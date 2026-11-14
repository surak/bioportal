from __future__ import annotations

import logging
import uuid
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from jobs import blablador, forms as jobforms, registry
from jobs.models import ArtifactState, Job, JobEvent, Status

log = logging.getLogger(__name__)


def index(request):
    return render(request, "jobs/index.html", {"specs": registry.all_specs()})


def _spec_or_404(slug):
    try:
        return registry.get(slug)
    except KeyError:
        raise Http404(f"unknown model {slug!r}")


@login_required
def submit(request, slug):
    spec = _spec_or_404(slug)
    active = Job.objects.filter(owner=request.user, status__in=Status.active()).count()
    at_quota = active >= settings.MAX_ACTIVE_JOBS_PER_USER

    if request.method == "POST":
        form = spec.form_class(request.POST, request.FILES)
        if at_quota:
            messages.error(
                request,
                f"You have {active} jobs in flight, which is this portal's limit "
                f"of {settings.MAX_ACTIVE_JOBS_PER_USER}. Wait for one to finish.",
            )
        elif form.is_valid():
            try:
                job = _create_and_submit(request, spec, form)
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            else:
                return redirect(job)
    else:
        form = spec.form_class()

    return render(
        request,
        "jobs/submit.html",
        {"spec": spec, "form": form, "at_quota": at_quota, "active": active},
    )


def _create_and_submit(request, spec, form) -> Job:
    """Persist the request, then send it. Never the other way round.

    The row (with its request_id and exact parameters) is committed before the
    HTTP call, so an ambiguous failure leaves something to retry rather than an
    orphaned job on the cluster that nobody can find.
    """
    parameters = spec.params_builder(form.cleaned_data)
    jobforms.check_parameter_size(parameters)

    with transaction.atomic():
        job = Job.objects.create(
            owner=request.user,
            model_slug=spec.slug,
            label=form.cleaned_data.get("label", ""),
            request_id=uuid.uuid4(),
            parameters=parameters,
            notify_by_email=form.cleaned_data.get("notify_by_email", True),
            status=Status.PENDING,
        )
        JobEvent.objects.create(job=job, kind="created")

    try:
        remote = blablador.submit(spec.alias, str(job.request_id), job.parameters)
    except blablador.QueueFull:
        # Leave it PENDING; the reconciler retries with the same request_id.
        job.poll_after = timezone.now()
        job.save(update_fields=["poll_after"])
        messages.warning(
            request,
            "The molecular queue is full right now. We've kept your request and "
            "will keep trying to submit it — you don't need to resend it.",
        )
        return job
    except blablador.ModelNotReady:
        job.poll_after = timezone.now()
        job.save(update_fields=["poll_after"])
        messages.warning(
            request,
            f"No {spec.name} worker is ready at the moment. We've kept your "
            "request and will submit it as soon as one is.",
        )
        return job
    except blablador.BlabladorError as exc:
        if exc.transient:
            job.poll_after = timezone.now()
            job.save(update_fields=["poll_after"])
            messages.warning(
                request,
                "We couldn't confirm the submission because of a connection "
                "problem. Your request is saved and will be retried — do not "
                "resubmit it.",
            )
        else:
            job.status = Status.SUBMIT_FAILED
            job.error = str(exc)
            job.save(update_fields=["status", "error"])
            JobEvent.objects.create(job=job, kind="submit-failed", detail=str(exc)[:500])
            messages.error(request, f"The service rejected this job: {exc}")
        log.warning("submit failed for job %s: %s", job.pk, exc)
        return job

    changed = job.apply_remote(remote)
    job.submitted_at = timezone.now()
    job.save(update_fields=sorted(set(changed + ["submitted_at"])))
    JobEvent.objects.create(job=job, kind="submitted", detail=job.remote_job_id)
    messages.success(
        request,
        "Submitted. You can close this page — "
        + (
            "we'll email you when it finishes."
            if job.notify_by_email
            else "your job list keeps track of it."
        ),
    )
    return job


@login_required
def job_list(request):
    jobs = Job.objects.filter(owner=request.user)
    status = request.GET.get("status")
    if status == "active":
        jobs = jobs.filter(status__in=Status.active())
    elif status in Status.values:
        jobs = jobs.filter(status=status)
    return render(request, "jobs/list.html", {"jobs": jobs, "status": status})


def _owned(request, pk) -> Job:
    job = get_object_or_404(Job, pk=pk)
    if job.owner_id != request.user.id and not request.user.is_staff:
        raise Http404
    return job


@login_required
def detail(request, pk):
    job = _owned(request, pk)
    return render(request, "jobs/detail.html", {"job": job, "spec": job.spec})


@login_required
def status_fragment(request, pk):
    """HTMX polls this while the job is active."""
    job = _owned(request, pk)
    resp = render(request, "jobs/_status.html", {"job": job, "spec": job.spec})
    if job.is_terminal:
        resp["HX-Trigger"] = "job-finished"
    return resp


@login_required
@require_POST
def cancel(request, pk):
    job = _owned(request, pk)
    if not job.is_active:
        messages.info(request, "That job has already finished.")
        return redirect(job)
    if not job.remote_job_id:
        job.status = Status.CANCELLED
        job.save(update_fields=["status"])
        messages.success(request, "Withdrawn before it reached the queue.")
        return redirect(job)

    try:
        remote = blablador.cancel(job.spec.alias, job.remote_job_id)
    except blablador.BlabladorError as exc:
        messages.error(request, f"Could not request cancellation: {exc}")
    else:
        job.apply_remote(remote)
        job.save()
        JobEvent.objects.create(job=job, kind="cancel-requested")
        messages.success(
            request,
            "Cancellation requested. A running job keeps going until its "
            "process actually stops, so the status may say “cancelling” for a "
            "while — and a job can still finish if it wins the race.",
        )
    return redirect(job)


@login_required
def download(request, pk):
    """Serve the stored result archive from local disk."""
    job = _owned(request, pk)
    if job.artifact_state != ArtifactState.STORED or not job.artifact_path:
        raise Http404("no stored result for this job")
    path = Path(job.artifact_path)
    if not path.is_file():
        log.error("artifact row for job %s points at a missing file", job.pk)
        raise Http404
    return FileResponse(
        path.open("rb"),
        as_attachment=True,
        filename=f"{job.spec.slug}-{job.pk}.zip",
        content_type="application/zip",
    )


@login_required
def queue_status(request):
    """Aggregate queue counts. Not a list of anyone's jobs — none is exposed."""
    snapshots = {}
    for spec in registry.all_specs():
        try:
            snapshots[spec] = blablador.queue(spec.alias)
        except blablador.BlabladorError as exc:
            snapshots[spec] = {"error": str(exc)}
    return render(request, "jobs/queue.html", {"snapshots": snapshots})
