"""Client for the Blablador molecular shim.

Every action is a POST to /v1/responses whose `input` is a JSON *string*
containing the molecular action. The outer response completing says nothing
about the computation; read `molecular_job`.

Nothing here logs API keys, sequences or uploaded structures.
"""

from __future__ import annotations

import base64
import binascii
import http.client
import json
import logging
import random
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from django.conf import settings

log = logging.getLogger(__name__)

# Per API action, not job runtime.
ACTION_TIMEOUT = 60
MAX_CHUNK = 1_048_576
ARTIFACT_FILENAME = "result.zip"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects instead of following them.

    Every request here carries the user's API key in a header. urllib re-sends
    that header on a redirect - including when it rewrites a POST into a GET -
    so a 302 from the service, or from any proxy in front of it, would hand one
    user's credential to whatever host the Location named. A gateway that is
    misconfigured or compromised is exactly when that happens, and no call in
    this module needs a redirect, so the safe answer is to stop rather than
    follow.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BlabladorError(
            f"the service answered {code} redirecting elsewhere; refusing to "
            "follow it with the caller's API key attached",
            status=code,
        )


class _HTTPSOnly(urllib.request.HTTPSHandler):
    """Only ever speak TLS to the configured service.

    urllib will happily use plain HTTP if a later change of BLABLADOR_API_BASE
    says so. The key travels in a header either way, so the scheme is enforced
    here rather than trusted from configuration.
    """

    def https_open(self, req):
        return self.do_open(http.client.HTTPSConnection, req)

    def http_open(self, req):
        raise BlabladorError(
            "the service was addressed over plain HTTP; the API key is only "
            "ever sent over TLS"
        )


_opener = urllib.request.build_opener(_NoRedirect, _HTTPSOnly)


def _endpoint() -> str:
    """The one URL this module will ever POST to.

    Built from configuration only, never from anything in a request, and
    checked here so a misconfigured base fails loudly instead of quietly
    sending credentials to the wrong scheme.
    """
    base = settings.BLABLADOR_API_BASE.rstrip("/")
    if not base.startswith("https://"):
        raise BlabladorError(f"BLABLADOR_API_BASE must be an https:// URL, not {base!r}")
    return f"{base}/responses"


class BlabladorError(RuntimeError):
    """Any failure talking to the shim."""

    def __init__(self, message: str, *, status: int | None = None, transient: bool = False):
        super().__init__(message)
        self.status = status
        # Transient means "the computation is fine, our request wasn't".
        self.transient = transient


class MissingCredential(BlabladorError):
    """The user has not stored an API key yet."""


class InvalidCredential(BlabladorError):
    """401/403 - the user's key was rejected and must be replaced."""


class JobGone(BlabladorError):
    """404 — the job or its artifact has expired out of retention."""


class QueueFull(BlabladorError):
    """429 — the global queued-job cap is reached."""


class ModelNotReady(BlabladorError):
    """503 — no worker is currently validated for this model."""


def _post(api_key: str, model: str, action: dict[str, Any]) -> dict[str, Any]:
    if not api_key:
        raise MissingCredential("no Blablador API key is stored for this user")

    body = json.dumps(
        {"model": model, "stream": False, "input": json.dumps(action)}
    ).encode()
    req = urllib.request.Request(
        _endpoint(),
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        method="POST",
    )

    try:
        # This opener refuses redirects and plain HTTP, so the key can only
        # reach the host configured above. See _NoRedirect.
        with _opener.open(req, timeout=ACTION_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")[:500]
        raise _http_error(exc.code, raw, action.get("action", "?")) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Network-level failure: never evidence that the job itself failed.
        raise BlabladorError(f"could not reach Blablador: {exc}", transient=True) from exc

    # The router can return HTML gateway errors with a 200, so parse defensively.
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BlabladorError(
            f"non-JSON response from the router: {raw[:200]!r}", transient=True
        ) from exc

    job = envelope.get("molecular_job")
    if job is None:
        # Fall back to the compatibility copy before giving up.
        job = _job_from_output(envelope)
    if job is None:
        raise BlabladorError("response contained no molecular_job object")
    return job


def _job_from_output(envelope: dict) -> dict | None:
    """The same object is serialized into output[0].content[0].text."""
    try:
        text = envelope["output"][0]["content"][0]["text"]
        parsed = json.loads(text)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _http_error(code: int, body: str, action: str) -> BlabladorError:
    detail = body.strip()[:300]
    if code == 404:
        return JobGone(f"job or artifact no longer available ({detail})", status=404)
    if code == 429:
        return QueueFull(
            "the molecular queue is full; try again shortly", status=429, transient=True
        )
    if code == 503:
        return ModelNotReady(
            "no worker is ready for this model right now", status=503, transient=True
        )
    if code in (401, 403):
        return InvalidCredential("Blablador rejected this API key", status=code)
    if code == 400:
        return BlabladorError(f"rejected: {detail}", status=400)
    return BlabladorError(
        f"HTTP {code} on {action}: {detail}", status=code, transient=code >= 500
    )


# --- actions --------------------------------------------------------------


def submit(api_key: str, model: str, request_id: str, parameters: dict) -> dict:
    """Submit. Safe to retry with the same request_id and parameters.

    The server derives a stable job ID from (request_id, model), so an
    ambiguous network failure is recovered by repeating the identical call.
    """
    return _post(api_key, model, {
        "action": "submit",
        "request_id": request_id,
        "parameters": parameters,
    })


def status(api_key: str, model: str, job_id: str) -> dict:
    return _post(api_key, model, {"action": "status", "job_id": job_id})


def cancel(api_key: str, model: str, job_id: str) -> dict:
    return _post(api_key, model, {"action": "cancel", "job_id": job_id})


def queue(api_key: str, model: str) -> dict:
    return _post(api_key, model, {"action": "queue"})


def fetch_artifact(api_key: str, model: str, job_id: str, dest: Path) -> int:
    """Stream the result ZIP to `dest`, one chunk at a time.

    Written to a .part file and renamed only once total_bytes have arrived, so
    a partial download is never mistaken for a complete result.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    offset = 0
    total: int | None = None
    attempts = 0

    with open(part, "wb") as fh:
        while total is None or offset < total:
            try:
                chunk = _post(api_key, model, {
                    "action": "artifact",
                    "job_id": job_id,
                    "offset": offset,
                    "length": MAX_CHUNK,
                })
            except BlabladorError as exc:
                attempts += 1
                if not exc.transient or attempts > 5:
                    part.unlink(missing_ok=True)
                    raise
                # Retry the same offset, backing off with jitter.
                time.sleep(min(30, 2**attempts) * (0.5 + random.random()))
                continue

            attempts = 0
            if total is None:
                total = int(chunk["total_bytes"])
                if total > settings.MAX_ARTIFACT_BYTES:
                    part.unlink(missing_ok=True)
                    raise BlabladorError(
                        f"result is {total} bytes, over the "
                        f"{settings.MAX_ARTIFACT_BYTES} byte local limit"
                    )

            try:
                data = base64.b64decode(chunk["data_base64"], validate=True)
            except (KeyError, binascii.Error) as exc:
                part.unlink(missing_ok=True)
                raise BlabladorError(f"undecodable artifact chunk at {offset}") from exc

            if not data:
                part.unlink(missing_ok=True)
                raise BlabladorError(f"empty chunk at offset {offset} of {total}")

            fh.write(data)
            # Advance by decoded bytes, never by the base64 string length.
            offset += len(data)

    part.replace(dest)
    return offset
