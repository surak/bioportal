"""Tests against the documented shim protocol, with the network stubbed.

Everything mocks `jobs.blablador._post`, so these assert our handling of the
contract in the deployment handoff rather than the live service.
"""

from __future__ import annotations

import base64
import io
import json
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from jobs import blablador, notify
from jobs.management.commands.reconcile import Command
from jobs.models import ApiCredential, ArtifactState, Job, JobEvent, Status
from jobs.registry import all_specs, get as get_spec

SEQ = "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"
PDB = "ATOM      1  N   MET A   1      27.340  24.430   2.614  1.00  9.67           N\nEND\n"
JOB_ID = "a" * 32


def remote_job(**over):
    job = {
        "id": JOB_ID,
        "model": "alias-esmfold2",
        "status": "queued",
        "terminal": False,
        "poll_after_seconds": 10,
        "queue_position": 3,
        "model_queue_position": 1,
        "waiting_reason": "waiting_for_gpu_capacity",
    }
    job.update(over)
    return job


class Base(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        ov = override_settings(
            # Production uses WhiteNoise's hashed manifest, which requires a
            # collectstatic run; tests render templates without one.
            STORAGES={
                "default": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage"
                },
                "staticfiles": {
                    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
                },
            },
            ARTIFACT_ROOT=self._tmp.name,
            BLABLADOR_API_BASE="https://example.invalid/v1",
            CREDENTIAL_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        )
        ov.enable()
        self.addCleanup(ov.disable)
        self.user = User.objects.create_user("alice", "alice@fz-juelich.de", "pw")
        ApiCredential.store(self.user, "alice-personal-key-0123456789")
        self.client.force_login(self.user)

    def make_job(self, **over):
        defaults = dict(
            owner=self.user,
            model_slug="esmfold2",
            request_id=uuid.uuid4(),
            parameters={"sequence": SEQ},
            status=Status.QUEUED,
            remote_job_id=JOB_ID,
        )
        defaults.update(over)
        return Job.objects.create(**defaults)


# --- transport ------------------------------------------------------------


class TransportTests(Base):
    def test_input_is_a_json_string_and_stream_is_false(self):
        captured = {}

        class FakeResp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            captured["auth"] = req.headers["Authorization"]
            return FakeResp(json.dumps({"molecular_job": remote_job()}).encode())

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            blablador.submit("k", "alias-esmfold2", "uuid-1", {"sequence": SEQ})

        self.assertIs(captured["body"]["stream"], False)
        self.assertEqual(captured["body"]["model"], "alias-esmfold2")
        # input must be a STRING, not a nested object.
        self.assertIsInstance(captured["body"]["input"], str)
        action = json.loads(captured["body"]["input"])
        self.assertEqual(action["action"], "submit")
        self.assertEqual(action["request_id"], "uuid-1")
        self.assertEqual(captured["auth"], "Bearer k")

    def test_falls_back_to_compatibility_copy_in_output(self):
        envelope = {
            "output": [{"content": [{"text": json.dumps(remote_job(status="running"))}]}]
        }
        with mock.patch("jobs.blablador._post", wraps=blablador._post):
            with mock.patch("urllib.request.urlopen") as op:
                op.return_value.__enter__.return_value.read.return_value = json.dumps(
                    envelope
                ).encode()
                job = blablador.status("k", "alias-esmfold2", JOB_ID)
        self.assertEqual(job["status"], "running")

    def test_html_gateway_error_is_transient_not_a_job_failure(self):
        with mock.patch("urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.read.return_value = b"<html>502</html>"
            with self.assertRaises(blablador.BlabladorError) as ctx:
                blablador.status("k", "alias-esmfold2", JOB_ID)
        self.assertTrue(ctx.exception.transient)

    def test_status_codes_map_to_typed_errors(self):
        import urllib.error

        for code, exc_type in (
            (404, blablador.JobGone),
            (429, blablador.QueueFull),
            (503, blablador.ModelNotReady),
        ):
            with mock.patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.HTTPError(
                    "u", code, "e", {}, io.BytesIO(b"{}")
                ),
            ):
                with self.assertRaises(exc_type):
                    blablador.status("k", "alias-esmfold2", JOB_ID)


# --- artifacts ------------------------------------------------------------


class ArtifactTests(Base):
    def test_chunks_are_concatenated_by_decoded_byte_offset(self):
        payload = bytes(range(256)) * 400  # 102_400 bytes
        half = len(payload) // 2
        chunks = [
            {"total_bytes": len(payload),
             "data_base64": base64.b64encode(payload[:half]).decode()},
            {"total_bytes": len(payload),
             "data_base64": base64.b64encode(payload[half:]).decode()},
        ]
        seen_offsets = []

        def fake_post(api_key, model, action):
            seen_offsets.append(action["offset"])
            return chunks[len(seen_offsets) - 1]

        dest = Path(self._tmp.name) / "r.zip"
        with mock.patch("jobs.blablador._post", fake_post):
            size = blablador.fetch_artifact("k", "alias-esmfold2", JOB_ID, dest)

        self.assertEqual(size, len(payload))
        self.assertEqual(dest.read_bytes(), payload)
        # Second request must start at the decoded length, not the base64 length.
        self.assertEqual(seen_offsets, [0, half])

    def test_partial_download_leaves_no_usable_file(self):
        def fake_post(api_key, model, action):
            raise blablador.BlabladorError("boom", transient=False)

        dest = Path(self._tmp.name) / "r.zip"
        with mock.patch("jobs.blablador._post", fake_post):
            with self.assertRaises(blablador.BlabladorError):
                blablador.fetch_artifact("k", "alias-esmfold2", JOB_ID, dest)
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_suffix(".zip.part").exists())

    @override_settings(MAX_ARTIFACT_BYTES=1000)
    def test_oversized_artifact_is_refused(self):
        def fake_post(api_key, model, action):
            return {"total_bytes": 5000, "data_base64": base64.b64encode(b"x").decode()}

        with mock.patch("jobs.blablador._post", fake_post):
            with self.assertRaises(blablador.BlabladorError):
                blablador.fetch_artifact(
                    "k", "alias-esmfold2", JOB_ID, Path(self._tmp.name) / "r.zip"
                )


# --- submission -----------------------------------------------------------


class SubmitTests(Base):
    FORM = {"sequence": SEQ, "num_loops": 3, "steps": 50, "notify_by_email": "on"}

    @mock.patch("jobs.blablador._post", return_value=remote_job())
    def test_submit_persists_then_sends(self, post):
        resp = self.client.post(reverse("jobs:submit", args=["esmfold2"]), self.FORM)
        job = Job.objects.get()
        self.assertRedirects(resp, job.get_absolute_url())
        self.assertEqual(job.status, Status.QUEUED)
        self.assertEqual(job.remote_job_id, JOB_ID)
        self.assertEqual(job.queue_position, 3)
        self.assertEqual(job.waiting_reason, "waiting_for_gpu_capacity")
        # The exact parameters sent are retained for idempotent retries.
        self.assertEqual(job.parameters["sequence"], SEQ)

    @mock.patch("jobs.blablador._post", side_effect=blablador.QueueFull("full", status=429))
    def test_queue_full_keeps_the_request_for_retry(self, post):
        self.client.post(reverse("jobs:submit", args=["esmfold2"]), self.FORM)
        job = Job.objects.get()
        self.assertEqual(job.status, Status.PENDING)
        self.assertIsNotNone(job.poll_after)

    @mock.patch(
        "jobs.blablador._post",
        side_effect=blablador.BlabladorError("timeout", transient=True),
    )
    def test_ambiguous_network_failure_does_not_lose_the_job(self, post):
        self.client.post(reverse("jobs:submit", args=["esmfold2"]), self.FORM)
        job = Job.objects.get()
        self.assertEqual(job.status, Status.PENDING)
        self.assertIsNotNone(job.request_id)

    @mock.patch(
        "jobs.blablador._post",
        side_effect=blablador.BlabladorError("bad parameters", status=400),
    )
    def test_hard_rejection_is_marked_not_submitted(self, post):
        self.client.post(reverse("jobs:submit", args=["esmfold2"]), self.FORM)
        self.assertEqual(Job.objects.get().status, Status.SUBMIT_FAILED)

    def test_retry_reuses_the_same_request_id_and_parameters(self):
        job = self.make_job(status=Status.PENDING, remote_job_id="")
        Job.objects.filter(pk=job.pk).update(
            created_at=job.created_at.replace(year=2020)
        )
        calls = []

        def fake_post(api_key, model, action):
            calls.append(action)
            return remote_job()

        with mock.patch("jobs.blablador._post", fake_post):
            Command().retry_submissions()

        self.assertEqual(calls[0]["request_id"], str(job.request_id))
        self.assertEqual(calls[0]["parameters"], job.parameters)
        job.refresh_from_db()
        self.assertEqual(job.status, Status.QUEUED)

    def test_bad_sequence_never_reaches_the_api(self):
        with mock.patch("jobs.blablador._post") as post:
            resp = self.client.post(
                reverse("jobs:submit", args=["esmfold2"]),
                {**self.FORM, "sequence": "not a protein!!"},
            )
        self.assertEqual(resp.status_code, 200)
        post.assert_not_called()
        self.assertEqual(Job.objects.count(), 0)


# --- parameter building ---------------------------------------------------


class ParameterTests(Base):
    def test_aliases_match_the_handoff(self):
        self.assertEqual(
            {s.slug: s.alias for s in all_specs()},
            {
                "esmfold2": "alias-esmfold2",
                "bioemu2": "alias-bioemu2",
                "rfdiffusion3": "alias-rfdiffusion3",
                "bindcraft": "alias-bindcraft",
                "proteina-complexa": "alias-proteina-complexa",
            },
        )

    def test_bioemu2_always_sends_explicit_msa_mode(self):
        spec = get_spec("bioemu2")
        form = spec.form_class(
            {"sequence": SEQ, "samples": 1, "seed": 42, "msa_mode": "single_sequence"}
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(spec.params_builder(form.cleaned_data)["msa_mode"], "single_sequence")

    def test_rfdiffusion3_unconditional_builds_official_input_object(self):
        spec = get_spec("rfdiffusion3")
        form = spec.form_class(
            {"mode": "unconditional", "length": "40-50", "samples": 1,
             "attachment_name": "target.pdb"}
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            spec.params_builder(form.cleaned_data)["input"], {"api": {"length": "40-50"}}
        )

    def test_rfdiffusion3_rejects_an_english_prompt(self):
        spec = get_spec("rfdiffusion3")
        form = spec.form_class(
            {"mode": "advanced", "rfd3_input": "design me a nice binder", "samples": 1}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("rfd3_input", form.errors)

    def test_proteina_requires_an_explicit_target_selection(self):
        """The adapter's A1-76 fallback is a ubiquitin canary, never a default."""
        spec = get_spec("proteina-complexa")
        form = spec.form_class(
            {"binder_length_min": 60, "binder_length_max": 100, "samples": 1, "steps": 400},
            {"target_pdb": self._upload()},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("target_input", form.errors)

    def test_bindcraft_walltime_is_sent_in_seconds_within_bounds(self):
        spec = get_spec("bindcraft")
        form = spec.form_class(
            {"chains": "A", "binder_length_min": 60, "binder_length_max": 100,
             "samples": 1, "max_trajectories": 100, "walltime_hours": 24},
            {"target_pdb": self._upload()},
        )
        self.assertTrue(form.is_valid(), form.errors)
        params = spec.params_builder(form.cleaned_data)
        self.assertEqual(params["walltime_seconds"], 86400)
        self.assertEqual(params["binder_length"], [60, 100])
        self.assertLessEqual(params["walltime_seconds"], 86400)

    def test_non_pdb_upload_is_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        spec = get_spec("bindcraft")
        form = spec.form_class(
            {"chains": "A", "binder_length_min": 60, "binder_length_max": 100,
             "samples": 1, "max_trajectories": 100, "walltime_hours": 1},
            {"target_pdb": SimpleUploadedFile("t.pdb", b"this is not a structure")},
        )
        self.assertFalse(form.is_valid())

    def _upload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile("target.pdb", PDB.encode())


# --- reconciliation -------------------------------------------------------


class ReconcileTests(Base):
    def test_poll_interval_comes_from_the_server(self):
        job = self.make_job()
        with mock.patch(
            "jobs.blablador._post", return_value=remote_job(poll_after_seconds=10)
        ):
            Command().poll_active()
        job.refresh_from_db()
        self.assertIsNotNone(job.poll_after)
        from django.utils import timezone

        self.assertGreater((job.poll_after - timezone.now()).total_seconds(), 5)

    def test_transient_failure_leaves_status_alone_and_backs_off(self):
        job = self.make_job(status=Status.RUNNING)
        with mock.patch(
            "jobs.blablador._post",
            side_effect=blablador.BlabladorError("router down", transient=True),
        ):
            Command().poll_active()
        job.refresh_from_db()
        # The science is untouched; only our connection bookkeeping changes.
        self.assertEqual(job.status, Status.RUNNING)
        self.assertEqual(job.consecutive_poll_errors, 1)
        self.assertIsNotNone(job.poll_after)

    def test_recovery_clears_the_error_banner(self):
        job = self.make_job(status=Status.RUNNING, consecutive_poll_errors=4,
                            last_poll_error="router down")
        with mock.patch("jobs.blablador._post", return_value=remote_job(status="running")):
            Command().poll_active()
        job.refresh_from_db()
        self.assertEqual(job.consecutive_poll_errors, 0)
        self.assertEqual(job.last_poll_error, "")

    def test_completion_schedules_artifact_download(self):
        job = self.make_job()
        with mock.patch(
            "jobs.blablador._post",
            return_value=remote_job(
                status="completed", terminal=True, poll_after_seconds=0,
                completed_at=1763000000, result_bytes=1234,
                usage={"gpu_seconds": 31.0, "gpu_count": 1, "gpu_model": "A100"},
            ),
        ):
            Command().poll_active()
        job.refresh_from_db()
        self.assertEqual(job.status, Status.COMPLETED)
        self.assertEqual(job.artifact_state, ArtifactState.PENDING)
        self.assertIsNone(job.poll_after)
        self.assertIsNotNone(job.retention_until)
        self.assertEqual(job.gpu_seconds, 31.0)

    def test_artifact_is_downloaded_and_stored(self):
        job = self.make_job(status=Status.COMPLETED, artifact_state=ArtifactState.PENDING)
        payload = b"PK\x03\x04fake zip"
        with mock.patch(
            "jobs.blablador._post",
            return_value={"total_bytes": len(payload),
                          "data_base64": base64.b64encode(payload).decode()},
        ):
            Command().download_artifacts()
        job.refresh_from_db()
        self.assertEqual(job.artifact_state, ArtifactState.STORED)
        self.assertEqual(Path(job.artifact_path).read_bytes(), payload)

    def test_expired_artifact_is_reported_honestly(self):
        job = self.make_job(status=Status.COMPLETED, artifact_state=ArtifactState.PENDING)
        with mock.patch("jobs.blablador._post", side_effect=blablador.JobGone("gone", status=404)):
            Command().download_artifacts()
        job.refresh_from_db()
        self.assertEqual(job.artifact_state, ArtifactState.EXPIRED)

    def test_stale_running_job_is_flagged_for_operations(self):
        import datetime as dt

        from django.utils import timezone

        job = self.make_job(
            status=Status.RUNNING,
            started_at=timezone.now() - dt.timedelta(days=5),
        )
        Command().flag_stale()
        job.refresh_from_db()
        self.assertIsNotNone(job.stale_since)
        # Flagged, not silently failed - we cannot know the computation died.
        self.assertEqual(job.status, Status.RUNNING)
        self.assertTrue(JobEvent.objects.filter(job=job, kind="stale").exists())

    def test_notification_is_sent_exactly_once(self):
        job = self.make_job(status=Status.COMPLETED, artifact_state=ArtifactState.STORED)
        notify.send_pending()
        notify.send_pending()
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn(SEQ, mail.outbox[0].body)  # never leak sequences


# --- cancellation, access control, pages ---------------------------------


class CancelTests(Base):
    def test_cancel_reports_the_cancelling_state_truthfully(self):
        job = self.make_job(status=Status.RUNNING)
        with mock.patch(
            "jobs.blablador._post", return_value=remote_job(status="cancelling")
        ):
            resp = self.client.post(reverse("jobs:cancel", args=[job.pk]), follow=True)
        job.refresh_from_db()
        self.assertEqual(job.status, Status.CANCELLING)
        self.assertContains(resp, "wins the race")


class AccessTests(Base):
    def test_cannot_see_another_users_job(self):
        bob = User.objects.create_user("bob", "bob@x.de", "pw")
        ApiCredential.store(bob, "bob-personal-key-0123456789")
        job = self.make_job(owner=bob)
        self.assertEqual(self.client.get(job.get_absolute_url()).status_code, 404)

    def test_cannot_download_another_users_result(self):
        bob = User.objects.create_user("bob", "bob@x.de", "pw")
        ApiCredential.store(bob, "bob-personal-key-0123456789")
        job = self.make_job(owner=bob, status=Status.COMPLETED,
                            artifact_state=ArtifactState.STORED,
                            artifact_path="/etc/passwd")
        self.assertEqual(
            self.client.get(reverse("jobs:download", args=[job.pk])).status_code, 404
        )

    def test_download_404s_when_nothing_is_stored(self):
        job = self.make_job(status=Status.COMPLETED)
        self.assertEqual(
            self.client.get(reverse("jobs:download", args=[job.pk])).status_code, 404
        )

    def test_submit_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse("jobs:submit", args=["esmfold2"]))
        self.assertEqual(resp.status_code, 302)


class PageTests(Base):
    def test_every_model_page_renders(self):
        self.assertEqual(self.client.get(reverse("jobs:index")).status_code, 200)
        for spec in all_specs():
            resp = self.client.get(reverse("jobs:submit", args=[spec.slug]))
            self.assertEqual(resp.status_code, 200, spec.slug)

    def test_queue_page_degrades_when_the_service_is_down(self):
        with mock.patch(
            "jobs.blablador._post",
            side_effect=blablador.BlabladorError("unreachable", transient=True),
        ):
            resp = self.client.get(reverse("jobs:queue"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "unavailable")

    def test_status_fragment_stops_polling_once_terminal(self):
        job = self.make_job(status=Status.RUNNING)
        self.assertContains(
            self.client.get(reverse("jobs:status", args=[job.pk])), "hx-trigger"
        )
        Job.objects.filter(pk=job.pk).update(status=Status.COMPLETED)
        resp = self.client.get(reverse("jobs:status", args=[job.pk]))
        self.assertNotContains(resp, "hx-trigger")
        self.assertEqual(resp["HX-Trigger"], "job-finished")

    def test_no_invented_progress_or_eta(self):
        """No percentage, progress bar or ETA: the service publishes none."""
        import re

        job = self.make_job(status=Status.RUNNING)
        body = self.client.get(reverse("jobs:status", args=[job.pk])).content.decode()
        self.assertNotRegex(body, r"\d+\s*%")
        self.assertNotIn("<progress", body)
        # It says so plainly instead of leaving the user guessing.
        self.assertIn("no progress percentage", body)

    def test_queue_position_is_not_presented_as_a_start_time(self):
        job = self.make_job(status=Status.QUEUED, queue_position=7)
        body = self.client.get(reverse("jobs:status", args=[job.pk])).content.decode()
        self.assertIn("position 7", body)
        self.assertNotRegex(body, r"(?i)(starts|begins|ready) in")


class CredentialTests(Base):
    """Each user's own key: stored encrypted, used for their jobs only."""

    def test_key_is_encrypted_at_rest_and_never_stored_plaintext(self):
        cred = ApiCredential.objects.get(user=self.user)
        self.assertNotIn("alice-personal-key", cred.encrypted_key)
        self.assertEqual(cred.reveal(), "alice-personal-key-0123456789")

    def test_fingerprint_is_not_reversible_and_distinguishes_keys(self):
        from jobs import credentials

        a = credentials.fingerprint("alice-personal-key-0123456789")
        b = credentials.fingerprint("another-key-9876543210")
        self.assertNotEqual(a, b)
        self.assertNotIn("alice", a)

    def test_each_job_uses_its_own_owner_key(self):
        bob = User.objects.create_user("bob", "bob@x.de", "pw")
        ApiCredential.store(bob, "bob-personal-key-0123456789")
        self.make_job(owner=bob)
        seen = []

        def fake_post(api_key, model, action):
            seen.append(api_key)
            return remote_job(status="running")

        with mock.patch("jobs.blablador._post", fake_post):
            Command().poll_active()

        self.assertEqual(seen, ["bob-personal-key-0123456789"])

    def test_submitting_without_a_key_is_blocked_before_any_call(self):
        ApiCredential.objects.filter(user=self.user).delete()
        with mock.patch("jobs.blablador._post") as post:
            resp = self.client.post(
                reverse("jobs:submit", args=["esmfold2"]),
                {"sequence": SEQ, "num_loops": 3, "steps": 50},
            )
        post.assert_not_called()
        self.assertEqual(Job.objects.count(), 0)
        self.assertContains(resp, "need a Blablador API key")

    def test_rejected_key_is_flagged_and_job_is_left_running(self):
        job = self.make_job(status=Status.RUNNING)
        with mock.patch(
            "jobs.blablador._post",
            side_effect=blablador.InvalidCredential("nope", status=401),
        ):
            Command().poll_active()
        job.refresh_from_db()
        cred = ApiCredential.objects.get(user=self.user)
        self.assertTrue(cred.is_rejected)
        # The computation is untouched; we simply cannot watch it.
        self.assertEqual(job.status, Status.RUNNING)

    def test_key_is_verified_before_being_stored(self):
        ApiCredential.objects.filter(user=self.user).delete()
        with mock.patch(
            "jobs.blablador._post",
            side_effect=blablador.InvalidCredential("nope", status=401),
        ):
            resp = self.client.post(
                reverse("jobs:credential"), {"api_key": "a-bad-key-0123456789"}
            )
        self.assertFalse(ApiCredential.objects.filter(user=self.user).exists())
        self.assertContains(resp, "rejected this key")

    def test_stored_key_is_never_rendered_back(self):
        resp = self.client.get(reverse("jobs:credential"))
        self.assertNotContains(resp, "alice-personal-key-0123456789")
        self.assertContains(resp, ApiCredential.objects.get(user=self.user).fingerprint)

    def test_removing_a_key_does_not_touch_jobs(self):
        job = self.make_job(status=Status.RUNNING)
        self.client.post(reverse("jobs:credential"), {"remove": "1"})
        self.assertFalse(ApiCredential.objects.filter(user=self.user).exists())
        job.refresh_from_db()
        self.assertEqual(job.status, Status.RUNNING)


class StructureCheckTests(Base):
    """The portal must notice when a finished job returns an impossible chain."""

    def _pdb(self, spacing: float, n: int = 20, chain: str = "A") -> str:
        lines = []
        for i in range(n):
            lines.append(
                f"ATOM  {i+1:5d}  CA  ALA {chain}{i+1:4d}    "
                f"{i * spacing:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00 50.00           C"
            )
        return "\n".join(lines) + "\nEND\n"

    def test_real_backbone_spacing_passes(self):
        from jobs import structure

        report = structure.check_text(self._pdb(3.8))
        self.assertEqual(report["verdict"], structure.Verdict.OK)
        self.assertEqual(report["mean_ca_ca"], 3.8)
        self.assertEqual(report["residues"], 20)

    def test_shredded_chain_is_flagged(self):
        from jobs import structure

        report = structure.check_text(self._pdb(10.0))
        self.assertEqual(report["verdict"], structure.Verdict.IMPLAUSIBLE)
        self.assertEqual(report["plausible_bonds"], 0)

    def test_chains_are_measured_separately(self):
        """The gap between two chains is not a broken bond."""
        from jobs import structure

        report = structure.check_text(self._pdb(3.8, chain="A") + self._pdb(3.8, chain="B"))
        self.assertEqual(report["verdict"], structure.Verdict.OK)
        self.assertEqual(report["chains"], 2)

    def test_archive_without_structure_is_unknown_not_failed(self):
        from jobs import structure

        path = Path(self._tmp.name) / "r.zip"
        import zipfile

        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("report.csv", "a,b\n1,2\n")
        self.assertEqual(structure.check_archive(path)["verdict"], structure.Verdict.UNKNOWN)

    def test_gzipped_cif_is_read(self):
        """RFdiffusion3 returns compressed mmCIF."""
        import gzip
        import zipfile

        from jobs import structure

        cif = "\n".join([
            "loop_",
            "_atom_site.label_atom_id",
            "_atom_site.auth_asym_id",
            "_atom_site.Cartn_x",
            "_atom_site.Cartn_y",
            "_atom_site.Cartn_z",
        ] + [f"CA A {i * 3.8:.3f} 0.000 0.000" for i in range(10)])
        path = Path(self._tmp.name) / "r.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("designs/d.cif.gz", gzip.compress(cif.encode()))
        report = structure.check_archive(path)
        self.assertEqual(report["verdict"], structure.Verdict.OK)
        self.assertEqual(report["residues"], 10)

    def test_download_records_the_check_and_warns(self):
        import zipfile

        job = self.make_job(status=Status.COMPLETED, artifact_state=ArtifactState.PENDING)
        payload_path = Path(self._tmp.name) / "src.zip"
        with zipfile.ZipFile(payload_path, "w") as zf:
            zf.writestr("structure.pdb", self._pdb(10.0))
        payload = payload_path.read_bytes()

        with mock.patch(
            "jobs.blablador._post",
            return_value={"total_bytes": len(payload),
                          "data_base64": base64.b64encode(payload).decode()},
        ):
            Command().download_artifacts()

        job.refresh_from_db()
        self.assertEqual(job.artifact_state, ArtifactState.STORED)
        self.assertEqual(job.structure_check["verdict"], "implausible")
        self.assertIn("not a folded structure", job.structure_warning)
        self.assertTrue(
            JobEvent.objects.filter(job=job, kind="structure-implausible").exists()
        )

    def test_warning_is_shown_on_the_job_page(self):
        job = self.make_job(
            status=Status.COMPLETED,
            artifact_state=ArtifactState.STORED,
            artifact_path=str(Path(self._tmp.name) / "r.zip"),
            structure_check={"verdict": "implausible", "bonds": 75,
                             "plausible_bonds": 7, "mean_ca_ca": 10.0},
        )
        resp = self.client.get(reverse("jobs:status", args=[job.pk]))
        self.assertContains(resp, "does not look like a real structure")

    def test_low_sampling_settings_are_refused_before_submission(self):
        spec = get_spec("esmfold2")
        form = spec.form_class({"sequence": SEQ, "num_loops": 1, "steps": 10})
        self.assertFalse(form.is_valid())
        self.assertIn("steps", form.errors)

    def test_documented_defaults_are_accepted(self):
        spec = get_spec("esmfold2")
        form = spec.form_class({"sequence": SEQ, "num_loops": 3, "steps": 50})
        self.assertTrue(form.is_valid(), form.errors)


class PreviewTests(Base):
    """The in-page viewer is fed PDB prepared server-side."""

    def _archive(self, name: str, content: bytes) -> Path:
        import zipfile

        path = Path(self._tmp.name) / f"{name.replace('/', '_')}.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(name, content)
        return path

    def test_mmcif_is_converted_to_pdb(self):
        from jobs import structure

        cif = "\n".join([
            "loop_",
            "_atom_site.group_PDB",
            "_atom_site.label_atom_id",
            "_atom_site.label_comp_id",
            "_atom_site.auth_asym_id",
            "_atom_site.auth_seq_id",
            "_atom_site.Cartn_x",
            "_atom_site.Cartn_y",
            "_atom_site.Cartn_z",
            "_atom_site.type_symbol",
            "ATOM CA ALA A 1 1.000 2.000 3.000 C",
            "ATOM CA GLY A 2 4.800 2.000 3.000 C",
        ])
        pdb = structure.cif_to_pdb(cif)
        lines = [l for l in pdb.splitlines() if l.startswith("ATOM")]
        self.assertEqual(len(lines), 2)
        # Fixed-column PDB: coordinates must land in columns 31-54.
        self.assertEqual(lines[0][30:38].strip(), "1.000")
        self.assertEqual(lines[0][17:20], "ALA")
        self.assertEqual(lines[0][21], "A")
        # And it must round-trip through our own reader.
        self.assertEqual(structure.check_text(pdb)["residues"], 2)

    def test_gzipped_structure_is_decompressed(self):
        import gzip

        from jobs import structure

        pdb = "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\nEND\n"
        archive = self._archive("d.pdb.gz", gzip.compress(pdb.encode()))
        name, text = structure.extract_preview(archive)
        self.assertTrue(name.endswith(".gz"))
        self.assertIn("ATOM", text)

    def test_preview_endpoint_serves_pdb(self):
        pdb = b"ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\nEND\n"
        archive = self._archive("structure.pdb", pdb)
        job = self.make_job(
            status=Status.COMPLETED,
            artifact_state=ArtifactState.STORED,
            artifact_path=str(archive),
        )
        resp = self.client.get(reverse("jobs:preview", args=[job.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "chemical/x-pdb")
        self.assertIn(b"ATOM", resp.content)

    def test_preview_is_owner_only(self):
        bob = User.objects.create_user("bob", "bob@x.de", "pw")
        ApiCredential.store(bob, "bob-personal-key-0123456789")
        job = self.make_job(owner=bob, status=Status.COMPLETED,
                            artifact_state=ArtifactState.STORED,
                            artifact_path="/etc/passwd")
        self.assertEqual(
            self.client.get(reverse("jobs:preview", args=[job.pk])).status_code, 404
        )

    def test_preview_404s_when_archive_has_no_structure(self):
        archive = self._archive("report.csv", b"a,b\n1,2\n")
        job = self.make_job(status=Status.COMPLETED,
                            artifact_state=ArtifactState.STORED,
                            artifact_path=str(archive))
        self.assertEqual(
            self.client.get(reverse("jobs:preview", args=[job.pk])).status_code, 404
        )
