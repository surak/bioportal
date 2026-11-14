from django.contrib import admin

from jobs.models import ArtifactState, Job, JobEvent, Status


class JobEventInline(admin.TabularInline):
    model = JobEvent
    extra = 0
    readonly_fields = ("at", "kind", "detail")
    can_delete = False


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = (
        "id", "model_slug", "owner", "status", "remote_job_id",
        "artifact_state", "trouble", "created_at",
    )
    list_filter = ("status", "artifact_state", "model_slug", "created_at")
    search_fields = (
        "id", "label", "remote_job_id", "request_id",
        "owner__username", "owner__email",
    )
    readonly_fields = (
        "request_id", "remote_job_id", "created_at", "submitted_at", "started_at",
        "finished_at", "retention_until", "artifact_path", "artifact_bytes",
        "usage", "stale_since", "consecutive_poll_errors", "last_poll_error",
    )
    inlines = [JobEventInline]
    actions = ["requeue_notification", "refetch_artifact"]

    @admin.display(description="Trouble", boolean=True)
    def trouble(self, obj) -> bool:
        """Jobs needing a human: stale, or we can't reach the service."""
        return bool(obj.stale_since) or obj.consecutive_poll_errors >= 3

    @admin.action(description="Re-send completion email")
    def requeue_notification(self, request, queryset):
        n = queryset.filter(status__in=Status.terminal()).update(notified_at=None)
        self.message_user(request, f"{n} job(s) will be re-notified on the next sweep.")

    @admin.action(description="Try fetching the result archive again")
    def refetch_artifact(self, request, queryset):
        n = queryset.filter(status=Status.COMPLETED).exclude(
            artifact_state=ArtifactState.STORED
        ).update(artifact_state=ArtifactState.PENDING)
        self.message_user(
            request,
            f"{n} job(s) queued for download. Note that the service deletes "
            "results 48 hours after a job finishes; expired ones cannot be recovered.",
        )


@admin.register(JobEvent)
class JobEventAdmin(admin.ModelAdmin):
    list_display = ("job", "at", "kind", "detail")
    list_filter = ("kind",)
    search_fields = ("job__id", "detail")
