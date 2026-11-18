from __future__ import annotations

import datetime as dt
import logging

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

log = logging.getLogger(__name__)


class Status(models.TextChoices):
    # Local-only: the row exists but the submit call has not returned yet.
    PENDING = "pending", "Submitting"
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    CANCELLING = "cancelling", "Cancelling"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    # Local-only: the submit call itself never succeeded.
    SUBMIT_FAILED = "submit_failed", "Not submitted"

    @classmethod
    def terminal(cls) -> set[str]:
        return {cls.COMPLETED, cls.FAILED, cls.CANCELLED, cls.SUBMIT_FAILED}

    @classmethod
    def active(cls) -> set[str]:
        return {cls.PENDING, cls.QUEUED, cls.RUNNING, cls.CANCELLING}


class ArtifactState(models.TextChoices):
    NONE = "none", "No result"
    PENDING = "pending", "Waiting to download"
    DOWNLOADING = "downloading", "Downloading"
    STORED = "stored", "Stored"
    EXPIRED = "expired", "Expired upstream"
    FAILED = "failed", "Download failed"
    TOO_LARGE = "too_large", "Too large to store"


class Job(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="jobs"
    )
    model_slug = models.CharField(max_length=64, db_index=True)
    label = models.CharField(max_length=200, blank=True)

    # Our idempotency key. Generated and saved before the submit call, and
    # reused verbatim on every retry so the shim returns the same job.
    request_id = models.UUIDField(unique=True)
    # The shim's 32-char hex job id, once we have it.
    remote_job_id = models.CharField(max_length=64, blank=True, db_index=True)

    parameters = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )

    # Straight from the shim; displayed, never invented.
    stage = models.CharField(max_length=120, blank=True)
    message = models.TextField(blank=True)
    queue_position = models.IntegerField(null=True, blank=True)
    model_queue_position = models.IntegerField(null=True, blank=True)
    waiting_reason = models.CharField(max_length=64, blank=True)
    usage = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True)

    # When our own polling is having trouble, as distinct from the job failing.
    poll_after = models.DateTimeField(null=True, blank=True, db_index=True)
    consecutive_poll_errors = models.IntegerField(default=0)
    last_poll_error = models.TextField(blank=True)
    stale_since = models.DateTimeField(null=True, blank=True)

    artifact_state = models.CharField(
        max_length=16, choices=ArtifactState.choices, default=ArtifactState.NONE
    )
    artifact_path = models.CharField(max_length=512, blank=True)
    artifact_bytes = models.BigIntegerField(null=True, blank=True)
    # Backbone plausibility of the returned structure; see jobs/structure.py.
    structure_check = models.JSONField(null=True, blank=True)
    # Upstream deletes results 48h after a terminal state.
    retention_until = models.DateTimeField(null=True, blank=True)

    notify_by_email = models.BooleanField(default=True)
    notified_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["status", "poll_after"])]

    def __str__(self) -> str:
        return f"#{self.pk} {self.model_slug} ({self.status})"

    def get_absolute_url(self) -> str:
        return reverse("jobs:detail", args=[self.pk])

    @property
    def spec(self):
        from jobs import registry

        return registry.get(self.model_slug)

    @property
    def is_terminal(self) -> bool:
        return self.status in Status.terminal()

    @property
    def is_active(self) -> bool:
        return self.status in Status.active()

    @property
    def display_label(self) -> str:
        return self.label or f"{self.spec.name} #{self.pk}"

    @property
    def has_result(self) -> bool:
        return self.artifact_state == ArtifactState.STORED

    @property
    def result_expires_in(self) -> dt.timedelta | None:
        """Time left before the upstream copy is deleted, if still relevant."""
        if not self.retention_until or self.has_result:
            return None
        remaining = self.retention_until - timezone.now()
        return remaining if remaining.total_seconds() > 0 else dt.timedelta(0)

    @property
    def structure_warning(self) -> str:
        """A plain-language warning when the returned backbone is impossible."""
        from jobs import structure

        check = self.structure_check or {}
        if check.get("verdict") != structure.Verdict.IMPLAUSIBLE:
            return ""
        return (
            f"Only {check.get('plausible_bonds', 0)} of {check.get('bonds', 0)} "
            f"consecutive backbone bonds are a physically possible length "
            f"(mean {check.get('mean_ca_ca')} \u00c5, expected about 3.8). "
            "The job finished, but this is not a folded structure."
        )

    @property
    def gpu_seconds(self) -> float | None:
        if isinstance(self.usage, dict):
            value = self.usage.get("gpu_seconds")
            if isinstance(value, (int, float)):
                return float(value)
        return None

    def apply_remote(self, job: dict) -> list[str]:
        """Copy shim fields onto this row. Returns the changed field names."""
        changed: list[str] = []

        def put(field: str, value) -> None:
            if value is not None and getattr(self, field) != value:
                setattr(self, field, value)
                changed.append(field)

        put("remote_job_id", job.get("id") or None)
        put("stage", job.get("stage") or "")
        put("message", job.get("message") or "")
        put("usage", job.get("usage") or None)

        status = job.get("status")
        if status in Status.values and status != self.status:
            self.status = status
            changed.append("status")

        # Queue positions are only meaningful while queued.
        if self.status == Status.QUEUED:
            put("queue_position", job.get("queue_position"))
            put("model_queue_position", job.get("model_queue_position"))
            put("waiting_reason", job.get("waiting_reason") or "")
        elif self.queue_position is not None:
            self.queue_position = None
            self.model_queue_position = None
            self.waiting_reason = ""
            changed += ["queue_position", "model_queue_position", "waiting_reason"]

        for field, key in (("started_at", "started_at"), ("finished_at", "completed_at")):
            ts = job.get(key)
            if ts and not getattr(self, field):
                setattr(self, field, dt.datetime.fromtimestamp(ts, tz=dt.UTC))
                changed.append(field)

        if self.is_terminal and not self.finished_at:
            self.finished_at = timezone.now()
            changed.append("finished_at")

        if self.is_terminal and not self.retention_until:
            seconds = job.get("retention_seconds") or settings.ARTIFACT_RETENTION_SECONDS
            self.retention_until = (self.finished_at or timezone.now()) + dt.timedelta(
                seconds=int(seconds)
            )
            changed.append("retention_until")

        if job.get("error"):
            put("error", str(job["error"])[:4000])

        if self.status == Status.COMPLETED and self.artifact_state == ArtifactState.NONE:
            self.artifact_state = ArtifactState.PENDING
            changed.append("artifact_state")

        return changed


class ApiCredential(models.Model):
    """A user's own Blablador API key, encrypted at rest.

    Stored rather than session-held because the reconciler needs it to poll and
    download results hours after the user has closed the browser.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="credential"
    )
    encrypted_key = models.TextField()
    # First 8 hex of a SHA-256 digest, so a user can recognise which key is
    # stored without the portal ever showing it back.
    fingerprint = models.CharField(max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    # Set when the service rejects the key, so we stop retrying and tell them.
    rejected_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.user} key {self.fingerprint}"

    @property
    def is_rejected(self) -> bool:
        return self.rejected_at is not None

    def reveal(self) -> str:
        from jobs import credentials

        return credentials.decrypt(self.encrypted_key)

    @classmethod
    def store(cls, user, api_key: str) -> "ApiCredential":
        """Replace this user's key, and say so in the security log.

        Storing a key is the one action that decides whose account every future
        job is metered to, so it leaves a record naming the person and the
        fingerprint - never the key. The fingerprint is enough to tell whether
        a later incident involves the key that was in place at the time.
        """
        from jobs import audit, credentials

        finger = credentials.fingerprint(api_key)
        obj, created = cls.objects.update_or_create(
            user=user,
            defaults={
                "encrypted_key": credentials.encrypt(api_key),
                "fingerprint": finger,
                "rejected_at": None,
            },
        )
        audit.audit.info(
            "credential-%s user=%s id=%s fingerprint=%s",
            "stored" if created else "replaced",
            user.get_username(),
            user.pk,
            finger,
        )
        return obj

    def delete(self, *args, **kwargs):
        """Removing a key is as security-relevant as storing one."""
        from jobs import audit

        audit.audit.info(
            "credential-removed user=%s id=%s fingerprint=%s",
            self.user.get_username(),
            self.user_id,
            self.fingerprint,
        )
        return super().delete(*args, **kwargs)

    def mark_used(self) -> None:
        """Record that the key worked, so a stale key is visible as stale.

        Updated with a direct queryset write: this runs on every reconcile
        sweep, and going through save() would rewrite the encrypted blob (and
        its updated_at) for nothing.
        """
        type(self).objects.filter(pk=self.pk).update(last_used_at=timezone.now())


def key_for(user) -> str | None:
    """The user's decrypted key, or None if absent or undecryptable."""
    from jobs import credentials

    cred = getattr(user, "credential", None)
    if cred is None:
        return None
    try:
        return cred.reveal()
    except credentials.CredentialError:
        log.error("could not decrypt stored credential for user %s", user.pk)
        return None


class Actor:
    """Who caused a JobEvent.

    An unattended service and a signed-in person both change a job's state, and
    a trail that does not say which is useless precisely when it is needed: "the
    job was cancelled" is a different incident depending on whether the owner
    did it or the reconciler did it on its own. Recording the actor costs one
    column and answers that without reading logs.
    """

    USER = "user"
    RECONCILER = "reconciler"
    SYSTEM = "system"


class JobEvent(models.Model):
    """Append-only audit trail."""

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField(auto_now_add=True)
    kind = models.CharField(max_length=32)
    detail = models.TextField(blank=True)
    # "user", "reconciler" or "system". Never empty: an event that cannot say
    # who caused it is a gap in the trail, so the default is the honest one for
    # the code that writes most events.
    actor = models.CharField(max_length=16, default=Actor.SYSTEM)

    class Meta:
        ordering = ("at",)

    def __str__(self) -> str:
        return f"{self.job_id} {self.kind} ({self.actor})"
