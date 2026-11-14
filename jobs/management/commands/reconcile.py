"""Poll the Blablador shim and bring local job rows up to date.

    manage.py reconcile --loop

Runs 24/7 under systemd. Responsibilities, in order:

1. Retry submissions that failed ambiguously, reusing the same request_id.
2. Poll active jobs when their own poll_after has come round.
3. Download artifacts of completed jobs, well before retention expires.
4. Send one completion email per job.

A failed HTTP request is never treated as a failed computation.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import signal
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from jobs import blablador, notify
from jobs.models import ArtifactState, Job, JobEvent, Status

log = logging.getLogger(__name__)

# The shim suggests 10s; never poll faster than this regardless.
MIN_POLL_SECONDS = 10
MAX_BACKOFF_SECONDS = 900


class Command(BaseCommand):
    help = "Sync jobs with the Blablador molecular shim."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--interval", type=int, default=5,
                            help="Seconds between sweeps; each job has its own schedule.")

    def handle(self, *args, **opts):
        self._stop = False
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._request_stop)

        while True:
            try:
                self.tick()
            except Exception:
                log.exception("reconcile sweep failed")

            if not opts["loop"] or self._stop:
                break
            for _ in range(opts["interval"]):
                if self._stop:
                    break
                time.sleep(1)

    def _request_stop(self, *_):
        self._stop = True

    def tick(self) -> None:
        self.retry_submissions()
        self.poll_active()
        self.download_artifacts()
        self.flag_stale()
        notify.send_pending()

    # --- 1. submissions that never got an answer -------------------------

    def retry_submissions(self) -> None:
        """A PENDING row means the submit call didn't come back cleanly.

        The shim derives a stable job ID from (request_id, model), so sending
        the identical request again is safe and returns the original job.
        """
        cutoff = timezone.now() - dt.timedelta(seconds=30)
        stuck = Job.objects.filter(
            status=Status.PENDING, remote_job_id="", created_at__lt=cutoff
        )[:20]
        for job in stuck:
            if job.poll_after and job.poll_after > timezone.now():
                continue
            try:
                remote = blablador.submit(
                    job.spec.alias, str(job.request_id), job.parameters
                )
            except blablador.BlabladorError as exc:
                self._record_poll_error(job, exc, context="resubmit")
                continue
            self._apply(job, remote, event="submitted-on-retry")

    # --- 2. polling -------------------------------------------------------

    def poll_active(self) -> None:
        now = timezone.now()
        due = Job.objects.filter(status__in=Status.active()).exclude(remote_job_id="")
        due = due.filter(poll_after__isnull=True) | due.filter(poll_after__lte=now)

        for job in due.distinct()[:200]:
            try:
                remote = blablador.status(job.spec.alias, job.remote_job_id)
            except blablador.JobGone as exc:
                # Past retention, or never existed. Not a computation failure,
                # but there is nothing left to wait for.
                self._mark_gone(job, str(exc))
            except blablador.BlabladorError as exc:
                self._record_poll_error(job, exc, context="poll")
            else:
                self._apply(job, remote)

    def _apply(self, job: Job, remote: dict, event: str = "status") -> None:
        with transaction.atomic():
            job = Job.objects.select_for_update().get(pk=job.pk)
            previous = job.status
            changed = job.apply_remote(remote)

            if previous == Status.PENDING and job.remote_job_id:
                job.submitted_at = job.submitted_at or timezone.now()
                changed.append("submitted_at")

            # A good answer clears any accumulated connection trouble.
            if job.consecutive_poll_errors or job.last_poll_error or job.stale_since:
                job.consecutive_poll_errors = 0
                job.last_poll_error = ""
                job.stale_since = None
                changed += ["consecutive_poll_errors", "last_poll_error", "stale_since"]

            job.poll_after = self._next_poll(job, remote)
            changed.append("poll_after")

            job.save(update_fields=sorted(set(changed)))

            if previous != job.status:
                JobEvent.objects.create(
                    job=job, kind=event, detail=f"{previous} -> {job.status}"
                )

    def _next_poll(self, job: Job, remote: dict) -> dt.datetime | None:
        if job.is_terminal:
            return None
        suggested = remote.get("poll_after_seconds")
        seconds = max(MIN_POLL_SECONDS, int(suggested or MIN_POLL_SECONDS))
        return timezone.now() + dt.timedelta(seconds=seconds)

    def _record_poll_error(self, job: Job, exc: Exception, *, context: str) -> None:
        """Back off with jitter. The job's status is deliberately untouched."""
        attempts = job.consecutive_poll_errors + 1
        delay = min(MAX_BACKOFF_SECONDS, MIN_POLL_SECONDS * 2**min(attempts, 7))
        delay *= 0.5 + random.random()

        fatal = isinstance(exc, blablador.BlabladorError) and not exc.transient
        updates = {
            "consecutive_poll_errors": attempts,
            "last_poll_error": str(exc)[:500],
            "poll_after": timezone.now() + dt.timedelta(seconds=delay),
        }
        if context == "resubmit" and fatal:
            # A 400 here means the request itself is bad; retrying won't help.
            updates["status"] = Status.SUBMIT_FAILED
            updates["error"] = str(exc)[:4000]
            JobEvent.objects.create(job=job, kind="submit-failed", detail=str(exc)[:500])
        Job.objects.filter(pk=job.pk).update(**updates)
        log.warning("%s error on job %s (attempt %d): %s", context, job.pk, attempts, exc)

    def _mark_gone(self, job: Job, detail: str) -> None:
        Job.objects.filter(pk=job.pk).update(
            status=Status.FAILED if not job.is_terminal else job.status,
            artifact_state=ArtifactState.EXPIRED,
            error=detail[:4000],
            poll_after=None,
            finished_at=job.finished_at or timezone.now(),
        )
        JobEvent.objects.create(job=job, kind="gone", detail=detail[:500])

    # --- 3. artifacts -----------------------------------------------------

    def download_artifacts(self) -> None:
        """Fetch results promptly: upstream deletes them 48 hours after finishing."""
        pending = Job.objects.filter(
            status=Status.COMPLETED, artifact_state=ArtifactState.PENDING
        ).exclude(remote_job_id="")[:5]

        for job in pending:
            dest = Path(settings.ARTIFACT_ROOT) / str(job.pk) / blablador.ARTIFACT_FILENAME
            Job.objects.filter(pk=job.pk).update(artifact_state=ArtifactState.DOWNLOADING)
            try:
                size = blablador.fetch_artifact(job.spec.alias, job.remote_job_id, dest)
            except blablador.JobGone:
                Job.objects.filter(pk=job.pk).update(artifact_state=ArtifactState.EXPIRED)
                JobEvent.objects.create(job=job, kind="artifact-expired")
                log.warning("artifact for job %s expired before download", job.pk)
            except blablador.BlabladorError as exc:
                state = (
                    ArtifactState.TOO_LARGE
                    if "over the" in str(exc)
                    else ArtifactState.PENDING  # try again next sweep
                )
                Job.objects.filter(pk=job.pk).update(
                    artifact_state=state, last_poll_error=str(exc)[:500]
                )
                log.warning("artifact download failed for job %s: %s", job.pk, exc)
            else:
                Job.objects.filter(pk=job.pk).update(
                    artifact_state=ArtifactState.STORED,
                    artifact_path=str(dest),
                    artifact_bytes=size,
                )
                JobEvent.objects.create(
                    job=job, kind="artifact-stored", detail=f"{size} bytes"
                )

    # --- 4. stale running jobs -------------------------------------------

    def flag_stale(self) -> None:
        """A hard worker failure can leave a job 'running' forever.

        There is no lease-based recovery upstream and no per-job liveness API,
        so the most we can honestly do is flag it for operations.
        """
        cutoff = timezone.now() - dt.timedelta(seconds=settings.STALE_RUNNING_SECONDS)
        stale = Job.objects.filter(
            status=Status.RUNNING, started_at__lt=cutoff, stale_since__isnull=True
        )
        for job in stale:
            Job.objects.filter(pk=job.pk).update(stale_since=timezone.now())
            JobEvent.objects.create(
                job=job,
                kind="stale",
                detail=f"still running after {settings.STALE_RUNNING_SECONDS}s",
            )
            log.error(
                "job %s (remote %s) has been running for over %ds - escalate to operations",
                job.pk, job.remote_job_id, settings.STALE_RUNNING_SECONDS,
            )
