"""Completion notifications.

Only terminal jobs with notify_by_email and no notified_at are picked up, and
notified_at is stamped in the same transaction, so a crash mid-send can at
worst drop one mail rather than spam the user on every tick.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from jobs.models import Actor, Job, JobEvent, Status

log = logging.getLogger(__name__)

SUBJECTS = {
    Status.COMPLETED: "[bioportal] {label} finished",
    Status.FAILED: "[bioportal] {label} failed",
    Status.CANCELLED: "[bioportal] {label} was cancelled",
    Status.SUBMIT_FAILED: "[bioportal] {label} could not be submitted",
}


def send_pending() -> int:
    pending = Job.objects.filter(
        status__in=Status.terminal(), notify_by_email=True, notified_at__isnull=True
    ).select_related("owner")

    sent = 0
    for job in pending:
        address = (job.owner.email or "").strip()
        if not address:
            # Nothing to send to; stamp it so we stop reconsidering the row.
            Job.objects.filter(pk=job.pk).update(notified_at=timezone.now())
            continue
        try:
            with transaction.atomic():
                locked = Job.objects.select_for_update().get(pk=job.pk)
                if locked.notified_at:
                    continue
                locked.notified_at = timezone.now()
                locked.save(update_fields=["notified_at"])
                send_mail(
                    subject=SUBJECTS[locked.status].format(label=locked.display_label),
                    message=_body(locked),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[address],
                    fail_silently=False,
                )
            JobEvent.objects.create(
                job=job, kind="notified", detail=address, actor=Actor.SYSTEM
            )
            sent += 1
        except Exception:
            log.exception("failed to notify job %s", job.pk)
    return sent


def _body(job: Job) -> str:
    url = f"{settings.SITE_BASE_URL.rstrip('/')}{job.get_absolute_url()}"
    lines = [
        f'Your {job.spec.name} job "{job.display_label}" is '
        f"{job.get_status_display().lower()}.",
        "",
    ]
    if job.submitted_at:
        lines.append(f"Submitted: {job.submitted_at:%Y-%m-%d %H:%M} UTC")
    if job.finished_at:
        lines.append(f"Finished:  {job.finished_at:%Y-%m-%d %H:%M} UTC")
    if job.gpu_seconds is not None:
        lines.append(f"GPU time:  {job.gpu_seconds:.1f} GPU-seconds")

    lines += ["", url]

    if job.status == Status.COMPLETED:
        if job.has_result:
            lines += ["", "The result archive is stored and ready to download."]
        else:
            lines += [
                "",
                "We are still fetching the result archive. Note that the "
                "service deletes results 48 hours after a job finishes.",
            ]
    elif job.error:
        lines += ["", "Reported error:", "", job.error[:2000]]

    # Deliberately never includes sequences, structures or parameters.
    return "\n".join(lines)
