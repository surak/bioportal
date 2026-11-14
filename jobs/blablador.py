"""Client for the Blablador molecular shim.

Every action is a POST to /v1/responses whose `input` is a JSON *string*
containing the molecular action. The outer response completing says nothing
about the computation; read `molecular_job`.

Nothing here logs API keys, sequences or uploaded structures.
"""

from __future__ import annotations

import base64
import binascii
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


class BlabladorError(RuntimeError):
    """Any failure talking to the shim."""

    def __init__(self, message: str, *, status: int | None = None, transient: bool = False):
        super().__init__(message)
        self.status = status
        # Transient means "the computation is fine, our request wasn't".
        self.transient = transient


class JobGone(BlabladorError):
    """404 — the job or its artifact has expired out of retention."""


class QueueFull(BlabladorError):
    """429 — the global queued-job cap is reached."""


class ModelNotReady(BlabladorError):
    """503 — no worker is currently validated for this model."""


def _post(model: str, action: dict[str, Any]) -> dict[str, Any]:
    if not settings.BLABLADOR_API_KEY:
        raise BlabladorError("BLABLADOR_API_KEY is not configured")

    body = json.dumps(
        {"model": model, "stream": False, "input": json.dumps(action)}
    ).encode()
    req = urllib.request.Request(
        f"{settings.BLABLADOR_API_BASE.rstrip('/')}/responses",
        data=body,
        headers={
            "Authorization": f"Bearer {settings.BLABLADOR_API_KEY}",
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=ACTION_TIMEOUT) as resp:
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
    if code == 401:
        return BlabladorError("Blablador rejected the API key", status=401)
    if code == 400:
        return BlabladorError(f"rejected: {detail}", status=400)
    return BlabladorError(
        f"HTTP {code} on {action}: {detail}", status=code, transient=code >= 500
    )


# --- actions --------------------------------------------------------------


def submit(model: str, request_id: str, parameters: dict) -> dict:
    """Submit. Safe to retry with the same request_id and parameters.

    The server derives a stable job ID from (request_id, model), so an
    ambiguous network failure is recovered by repeating the identical call.
    """
    return _post(model, {
        "action": "submit",
        "request_id": request_id,
        "parameters": parameters,
    })


def status(model: str, job_id: str) -> dict:
    return _post(model, {"action": "status", "job_id": job_id})


def cancel(model: str, job_id: str) -> dict:
    return _post(model, {"action": "cancel", "job_id": job_id})


def queue(model: str) -> dict:
    return _post(model, {"action": "queue"})


def fetch_artifact(model: str, job_id: str, dest: Path) -> int:
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
                chunk = _post(model, {
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
