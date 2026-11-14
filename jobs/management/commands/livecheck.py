"""Exercise the real Blablador shim through our own client code.

    manage.py livecheck --discover      # GET /v1/models, free
    manage.py livecheck --queue         # queue action per model, free
    manage.py livecheck --submit SLUG   # submits a real job, costs GPU time
    manage.py livecheck --watch         # poll + fetch artifacts until all settle

Deliberately uses jobs.blablador and the real Job rows, so a pass here is
evidence about the portal, not just about the service.
"""

from __future__ import annotations

import json
import time
import urllib.request
import uuid

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from jobs import blablador, registry
from jobs.models import ApiCredential, ArtifactState, Job, Status, key_for

UBIQUITIN = (
    "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"
)


class Command(BaseCommand):
    help = "Run live checks against the Blablador molecular shim."

    def add_arguments(self, parser):
        parser.add_argument("--discover", action="store_true")
        parser.add_argument("--queue", action="store_true")
        parser.add_argument("--submit", metavar="SLUG")
        parser.add_argument("--watch", action="store_true")
        parser.add_argument("--timeout", type=int, default=600)
        parser.add_argument("--user", default="admin")
        parser.add_argument("--sequence-file",
                            help="FASTA file to use instead of the built-in sequence.")
        parser.add_argument("--label", default="")

    def _key(self, username: str) -> str:
        key = key_for(User.objects.get(username=username))
        if not key:
            raise SystemExit(
                f"user {username} has no stored API key; add one at /account/key/ "
                "or with manage.py setkey"
            )
        return key

    def handle(self, *args, **opts):
        self.api_key = self._key(opts["user"])
        if opts["discover"]:
            self.discover()
        if opts["queue"]:
            self.queue()
        if opts["submit"]:
            self.submit(
                opts["submit"], opts["user"],
                sequence_file=opts.get("sequence_file"),
                label=opts.get("label") or "",
            )
        if opts["watch"]:
            self.watch(opts["timeout"])

    # --- free, read-only --------------------------------------------------

    def discover(self):
        self.stdout.write(self.style.MIGRATE_HEADING("GET /v1/models"))
        req = urllib.request.Request(
            f"{settings.BLABLADOR_API_BASE.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
        ids = {m.get("id") for m in payload.get("data", [])}
        for spec in registry.all_specs():
            ok = spec.alias in ids
            self.stdout.write(
                f"  {'OK  ' if ok else 'MISS'} {spec.alias:32} ({spec.name})"
            )
        self.stdout.write(f"  {len(ids)} model ids advertised in total")

    def queue(self):
        self.stdout.write(self.style.MIGRATE_HEADING("queue action"))
        for spec in registry.all_specs():
            try:
                q = blablador.queue(self.api_key, spec.alias)
            except blablador.BlabladorError as exc:
                self.stdout.write(f"  FAIL {spec.alias}: {exc}")
                continue
            self.stdout.write(
                f"  {spec.alias:32} queued={q.get('queued')} running={q.get('running')} "
                f"ready={q.get('model_workers_ready')} gpus_free={q.get('gpus_free')} "
                f"global_queued={q.get('global_queued')} workers={q.get('workers_online')}"
            )

    # --- costs GPU time ---------------------------------------------------

    def submit(self, slug: str, username: str, sequence_file=None, label=""):
        spec = registry.get(slug)
        owner = User.objects.get(username=username)
        params = SMOKE_PARAMS[slug]()
        if sequence_file:
            from jobs.forms import clean_protein_sequence
            from pathlib import Path as _Path

            params["sequence"] = clean_protein_sequence(
                _Path(sequence_file).read_text()
            )

        job = Job.objects.create(
            owner=owner,
            model_slug=slug,
            label=label or f"live smoke {timezone.now():%H:%M}",
            request_id=uuid.uuid4(),
            parameters=params,
            status=Status.PENDING,
            notify_by_email=False,
        )
        self.stdout.write(f"  job {job.pk} ({spec.name}) request_id={job.request_id}")
        try:
            remote = blablador.submit(self.api_key, spec.alias, str(job.request_id), params)
        except blablador.BlabladorError as exc:
            job.status = Status.SUBMIT_FAILED
            job.error = str(exc)
            job.save(update_fields=["status", "error"])
            self.stdout.write(self.style.ERROR(f"  submit failed: {exc}"))
            return
        changed = job.apply_remote(remote)
        job.submitted_at = timezone.now()
        job.save(update_fields=sorted(set(changed + ["submitted_at"])))
        self.stdout.write(
            self.style.SUCCESS(
                f"  accepted: remote={job.remote_job_id} status={job.status} "
                f"queue_position={job.queue_position}"
            )
        )

    def watch(self, timeout: int):
        from jobs.management.commands.reconcile import Command as Reconcile

        reconcile = Reconcile()
        deadline = time.time() + timeout
        while time.time() < deadline:
            reconcile.retry_submissions()
            reconcile.poll_active()
            reconcile.download_artifacts()

            rows = Job.objects.order_by("pk")
            self.stdout.write(f"--- {timezone.now():%H:%M:%S}")
            for job in rows:
                extra = ""
                if job.queue_position:
                    extra = f" pos={job.queue_position}"
                if job.stage:
                    extra += f" stage={job.stage}"
                if job.artifact_state != ArtifactState.NONE:
                    extra += f" artifact={job.artifact_state}"
                if job.gpu_seconds is not None:
                    extra += f" gpu_s={job.gpu_seconds:.1f}"
                if job.consecutive_poll_errors:
                    extra += f" poll_errors={job.consecutive_poll_errors}"
                self.stdout.write(f"  #{job.pk} {job.model_slug:18} {job.status:10}{extra}")

            if not rows.filter(status__in=Status.active()).exists() and not rows.filter(
                status=Status.COMPLETED,
                artifact_state__in=[ArtifactState.PENDING, ArtifactState.DOWNLOADING],
            ).exists():
                self.stdout.write(self.style.SUCCESS("all jobs settled"))
                return
            time.sleep(15)
        self.stdout.write(self.style.WARNING("watch timed out (jobs continue running)"))


def _ubiquitin_pdb() -> str:
    """Fetch 1UBQ once for the target-based models."""
    from pathlib import Path

    cache = Path(settings.BASE_DIR) / "artifacts" / "1ubq.pdb"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen("https://files.rcsb.org/download/1UBQ.pdb", timeout=60) as r:
            cache.write_bytes(r.read())
    return cache.read_text()


# Smallest inputs that still exercise each real code path.
SMOKE_PARAMS = {
    # The documented example. Fewer loops/steps finishes quickly but
    # returns a structure that is not physically plausible.
    "esmfold2": lambda: {"sequence": UBIQUITIN, "num_loops": 3, "steps": 50},
    "bioemu2": lambda: {
        "sequence": UBIQUITIN, "samples": 1, "seed": 42, "msa_mode": "single_sequence",
    },
    "rfdiffusion3": lambda: {"input": {"api": {"length": "40-50"}}, "samples": 1},
    "bindcraft": lambda: {
        "target_pdb": _ubiquitin_pdb(),
        "chains": "A",
        "binder_length": [60, 100],
        "samples": 1,
        "max_trajectories": 1,
        "walltime_seconds": 3600,
    },
    "proteina-complexa": lambda: {
        "target_pdb": _ubiquitin_pdb(),
        "target_input": "A1-76",
        "binder_length": [60, 100],
        "samples": 1,
        "steps": 50,
        "seed": 42,
    },
}
