"""Tests against the documented shim protocol, with the network stubbed.

Everything mocks `jobs.blablador._post`, so these assert our handling of the
contract in the deployment handoff rather than the live service.
"""

from __future__ import annotations

import base64
import io
import json
import re
import subprocess
import sys
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
from jobs.models import Actor, ApiCredential, ArtifactState, Job, JobEvent, Status
from jobs.registry import all_specs, get as get_spec

SEQ = "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"
PDB = "ATOM      1  N   MET A   1      27.340  24.430   2.614  1.00  9.67           N\nEND\n"
JOB_ID = "a" * 32


def _pdb(residues: int, chain: str = "A") -> str:
    """A PDB with one backbone atom per residue, for size checks."""
    return "".join(
        f"ATOM  {i:5d}  CA  ALA {chain}{i:4d}      "
        f"27.340  24.430   2.614  1.00  9.67           C\n"
        for i in range(1, residues + 1)
    ) + "END\n"


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

        with mock.patch.object(blablador._opener, "open", fake_urlopen):
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
            with mock.patch.object(blablador._opener, "open") as op:
                op.return_value.__enter__.return_value.read.return_value = json.dumps(
                    envelope
                ).encode()
                job = blablador.status("k", "alias-esmfold2", JOB_ID)
        self.assertEqual(job["status"], "running")

    def test_html_gateway_error_is_transient_not_a_job_failure(self):
        with mock.patch.object(blablador._opener, "open") as op:
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
            with mock.patch.object(
                blablador._opener,
                "open",
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

    def test_oversized_complex_is_refused_with_a_way_forward(self):
        """One design runs on one GPU, so too big is a refusal, not a wait."""
        spec = get_spec("bindcraft")
        form = spec.form_class(
            {"chains": "A", "binder_length_min": 60, "binder_length_max": 100,
             "samples": 1, "max_trajectories": 100, "walltime_hours": 1},
            {"target_pdb": self._upload(residues=600)},
        )
        self.assertFalse(form.is_valid())
        error = " ".join(form.errors["target_pdb"])
        self.assertIn("700 residues", error)   # 600 target + 100 binder
        self.assertIn("550", error)            # the budget
        self.assertIn("Trim", error)
        self.assertIn("hotspots", error)

    def test_only_named_chains_count_towards_the_budget(self):
        """Chains the run ignores must not push a workable target over."""
        spec = get_spec("bindcraft")
        pdb = _pdb(300, chain="A") + _pdb(300, chain="B")
        form = spec.form_class(
            {"chains": "A", "binder_length_min": 60, "binder_length_max": 100,
             "samples": 1, "max_trajectories": 100, "walltime_hours": 1},
            {"target_pdb": self._upload(text=pdb)},
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_a_chain_that_is_not_in_the_file_is_named(self):
        spec = get_spec("bindcraft")
        form = spec.form_class(
            {"chains": "Z", "binder_length_min": 60, "binder_length_max": 100,
             "samples": 1, "max_trajectories": 100, "walltime_hours": 1},
            {"target_pdb": self._upload(residues=50)},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Z", " ".join(form.errors["chains"]))

    @override_settings(BINDCRAFT_RESIDUE_BUDGET=950)
    def test_budget_follows_the_configured_card(self):
        """An 80 Gb card takes what a 32 Gb one cannot."""
        spec = get_spec("bindcraft")
        form = spec.form_class(
            {"chains": "A", "binder_length_min": 60, "binder_length_max": 100,
             "samples": 1, "max_trajectories": 100, "walltime_hours": 1},
            {"target_pdb": self._upload(residues=600)},
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_binder_length_stops_at_the_documented_ceiling(self):
        spec = get_spec("bindcraft")
        form = spec.form_class(
            {"chains": "A", "binder_length_min": 60, "binder_length_max": 400,
             "samples": 1, "max_trajectories": 100, "walltime_hours": 1},
            {"target_pdb": self._upload(residues=50)},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("binder_length_max", form.errors)

    def _upload(self, residues=1, text=None):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile("target.pdb", (text or _pdb(residues)).encode())


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

    # --- the home page's two gates -------------------------------------
    #
    # The page used to answer a locked gate with five identical buttons, each
    # of which led to the same place. It now says the reason once and lists the
    # models as rows: links when the gate is open, inert when it is not.

    def test_signed_out_home_has_one_action_and_a_quiet_list(self):
        self.client.logout()
        with override_settings(
            OIDC_RP_CLIENT_ID="bioportal", OIDC_RP_CLIENT_SECRET="secret", OIDC_AVAILABLE=True
        ):
            body = self.client.get(reverse("jobs:index")).content.decode()
            self.assertIn("Sign in to submit a job", body)
            # One in the header, one in the gate: never one per model.
            self.assertEqual(
                body.count('href="' + reverse("oidc_authentication_init") + '"'), 2
            )
            # No per-model call to action at all.
            self.assertNotIn(">Sign in to submit<", body)
            self.assertNotIn("<a class=\"model\"", body)
            self.assertEqual(body.count('class="model locked"'), len(all_specs()))

    def test_no_sign_in_link_is_offered_before_a_client_is_registered(self):
        """A dead sign-in link reads as the visitor's own mistake.

        Without a registered AAI client the authorize endpoint rejects the
        request with "Missing client_id parameter", so no template may render
        the link - not the header, not the gate, not the login page. A local
        account stands in for it.
        """
        self.client.logout()
        with override_settings(
            OIDC_RP_CLIENT_ID="",
            OIDC_RP_CLIENT_SECRET="",
            OIDC_AVAILABLE=False,
            ALLOW_SELF_SIGNUP=False,
        ):
            for url in (reverse("jobs:index"), reverse("login")):
                body = self.client.get(url).content.decode()
                self.assertNotIn(
                    reverse("oidc_authentication_init"),
                    body,
                    f"{url} still links to the OAuth endpoint",
                )
            home = self.client.get(reverse("jobs:index")).content.decode()
            self.assertIn("Sign in to submit a job", home)
            self.assertIn('href="' + reverse("login") + '"', home)
            login = self.client.get(reverse("login")).content.decode()
            self.assertIn("Sign in with the account you created here", login)
            self.assertIn('name="username"', login)
            self.assertIn('name="password"', login)

    def test_self_service_signup_is_offered_when_it_is_on(self):
        """Without an AAI client, the portal's own accounts are the way in."""
        self.client.logout()
        with override_settings(
            OIDC_AVAILABLE=False, ALLOW_SELF_SIGNUP=True
        ):
            home = self.client.get(reverse("jobs:index")).content.decode()
            self.assertIn("Create an account to submit a job", home)
            self.assertIn(
                'href="' + reverse("jobs:signup") + '"', home
            )
            self.assertNotIn("Submissions are not open yet", home)
            login = self.client.get(reverse("login")).content.decode()
            self.assertIn('href="' + reverse("jobs:signup") + '"', login)
            self.assertIn('name="password"', login)

    def test_signup_is_closed_beside_a_working_federated_login(self):
        self.client.logout()
        with override_settings(OIDC_AVAILABLE=True, ALLOW_SELF_SIGNUP=False):
            self.assertEqual(
                self.client.get(reverse("jobs:signup")).status_code, 404
            )
            body = self.client.get(reverse("jobs:index")).content.decode()
            self.assertNotIn(reverse("jobs:signup"), body)

    def test_a_self_made_account_can_submit_its_own_jobs(self):
        self.client.logout()
        with override_settings(OIDC_AVAILABLE=False, ALLOW_SELF_SIGNUP=True):
            response = self.client.post(
                reverse("jobs:signup"),
                {
                    "username": "newcomer",
                    "email": "newcomer@example.org",
                    "password1": "a-long-enough-passphrase",
                    "password2": "a-long-enough-passphrase",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("jobs:credential"))
        user = User.objects.get(username="newcomer")
        self.assertEqual(user.email, "newcomer@example.org")
        # Signed in by the same request, and there is no key yet: the gate now
        # asks for one instead of asking who they are.
        body = self.client.get(reverse("jobs:index")).content.decode()
        self.assertIn("Add your Blablador API key to submit a job", body)
        self.assertNotIn("Create an account to submit a job", body)

    def test_a_second_account_cannot_claim_an_email_address(self):
        User.objects.create_user(
            "early", email="someone@example.org", password="a-long-enough-passphrase"
        )
        self.client.logout()
        with override_settings(OIDC_AVAILABLE=False, ALLOW_SELF_SIGNUP=True):
            response = self.client.post(
                reverse("jobs:signup"),
                {
                    "username": "late",
                    "email": "Someone@Example.org",
                    "password1": "a-long-enough-passphrase",
                    "password2": "a-long-enough-passphrase",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.context["form"].errors.get("email")
        )
        self.assertFalse(User.objects.filter(username="late").exists())
        self.assertEqual(User.objects.get(username="early").email, "someone@example.org")

    def test_the_federated_link_returns_once_a_client_exists(self):
        self.client.logout()
        with override_settings(
            OIDC_RP_CLIENT_ID="bioportal", OIDC_RP_CLIENT_SECRET="secret", OIDC_AVAILABLE=True
        ):
            body = self.client.get(reverse("jobs:index")).content.decode()
            self.assertIn(reverse("oidc_authentication_init"), body)
            self.assertIn("Sign in to submit a job", body)
            self.assertNotIn("Sign-in not configured", body)

    def test_the_locked_list_still_says_what_the_models_are(self):
        """Inert is not empty: the alias and the blurb are the useful part."""
        self.client.logout()
        body = self.client.get(reverse("jobs:index")).content.decode()
        for spec in all_specs():
            self.assertIn(spec.name, body)
            self.assertIn(spec.alias, body)
            self.assertIn(spec.blurb, body)

    def test_a_user_with_a_key_can_click_straight_through(self):
        body = self.client.get(reverse("jobs:index")).content.decode()
        self.assertNotIn("class=\"gate", body)
        for spec in all_specs():
            self.assertIn(f'<a class="model" href="/models/{spec.slug}/">', body)

    def test_a_user_without_a_key_is_sent_to_the_key_page_once(self):
        """A key is the second gate; the models stay inert until it is closed."""
        ApiCredential.objects.all().delete()
        body = self.client.get(reverse("jobs:index")).content.decode()
        self.assertIn("Add your Blablador API key to submit a job", body)
        self.assertIn('class="gate warn"', body)
        self.assertNotIn("<a class=\"model\"", body)
        # The nav link plus the single gate button, and nothing per model.
        self.assertEqual(body.count('href="' + reverse("jobs:credential") + '"'), 2)
        self.assertEqual(body.count('class="model locked"'), len(all_specs()))

    def test_a_rejected_key_is_named_as_rejected(self):
        ApiCredential.objects.update(rejected_at="2026-01-01T00:00:00Z")
        body = self.client.get(reverse("jobs:index")).content.decode()
        self.assertIn("Your API key was rejected", body)
        self.assertIn("Replace my key", body)

    def test_the_gate_never_renders_a_template_comment(self):
        """{# ... #} is single-line only: a multi-line comment leaks as text."""
        for path in (reverse("jobs:index"), reverse("jobs:list")):
            body = self.client.get(path).content.decode()
            self.assertNotIn("{#", body)
            self.assertNotIn("#}", body)

    def test_no_markup_is_nested_inside_a_button(self):
        """The submit button was once spliced together around a copied block
        of markup, which left a whole <div> inside the <button> element."""
        for slug in ("esmfold2", "bindcraft"):
            body = self.client.get(reverse("jobs:submit", args=[slug])).content.decode()
            for button in re.findall(r"<button.*?</button>", body, re.S):
                self.assertNotIn("<div", button, slug)

    def test_quota_disables_the_submit_button(self):
        with override_settings(MAX_ACTIVE_JOBS_PER_USER=1):
            self.make_job(status=Status.QUEUED)
            self.make_job(status=Status.RUNNING)
            body = self.client.get(reverse("jobs:submit", args=["esmfold2"])).content.decode()
        self.assertRegex(body, r'<button type="submit"\s+disabled>')

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

    def test_diagnostics_show_remote_id_and_open_on_failure(self):
        job = self.make_job(status=Status.RUNNING, remote_job_id="abc123")
        body = self.client.get(reverse("jobs:status", args=[job.pk])).content.decode()
        self.assertIn("<code>abc123</code>", body)
        self.assertIn('<details class="diagnostics">', body)
        Job.objects.filter(pk=job.pk).update(status=Status.FAILED)
        body = self.client.get(reverse("jobs:status", args=[job.pk])).content.decode()
        self.assertIn('<details class="diagnostics" open>', body)

    def test_no_invented_progress_or_eta(self):
        """No percentage, progress bar or ETA: the service publishes none."""
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
        self.assertContains(resp, "did not recognise this key")

    def test_a_rejected_key_is_not_blamed_on_the_key_when_it_is_a_permission(self):
        """401 and 403 are different problems and must read differently."""
        ApiCredential.objects.filter(user=self.user).delete()
        with mock.patch(
            "jobs.blablador._post",
            side_effect=blablador.InvalidCredential("nope", status=403),
        ):
            resp = self.client.post(
                reverse("jobs:credential"), {"api_key": "a-bad-key-0123456789"}
            )
        self.assertFalse(ApiCredential.objects.filter(user=self.user).exists())
        self.assertContains(resp, "will not admit it")
        self.assertNotContains(resp, "did not recognise this key")

    def test_the_rejection_says_how_much_arrived_not_what_it_was(self):
        """A fingerprint is comparable; the key itself is never echoed."""
        ApiCredential.objects.filter(user=self.user).delete()
        with mock.patch(
            "jobs.blablador._post",
            side_effect=blablador.InvalidCredential("nope", status=401),
        ):
            resp = self.client.post(
                reverse("jobs:credential"), {"api_key": "a-bad-key-0123456789"}
            )
        body = resp.content.decode()
        self.assertIn("20 characters", body)
        self.assertNotIn("a-bad-key-0123456789", body)

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


class ApiKeyPasteTests(TestCase):
    """A key arrives wrapped in whatever it was copied from.

    The router answers 401 for the decorations as readily as for a wrong key,
    and the old message then sent the reader hunting for a mistake in a key
    that was correct all along. These are the shapes people actually paste.
    """

    KEY = "glpat-AbCdEfGhIjKlMnOpQrSt.01.zyxwvutsrqponmlk"

    def clean(self, raw):
        from jobs.forms import ApiKeyForm

        form = ApiKeyForm({"api_key": raw})
        self.assertTrue(form.is_valid(), form.errors)
        return form.cleaned_data["api_key"]

    def test_a_bare_key_is_kept_as_it_is(self):
        self.assertEqual(self.clean(self.KEY), self.KEY)

    def test_surrounding_whitespace_is_dropped(self):
        self.assertEqual(self.clean(f"  {self.KEY}\n"), self.KEY)

    def test_straight_quotes_are_dropped(self):
        self.assertEqual(self.clean(f'"{self.KEY}"'), self.KEY)
        self.assertEqual(self.clean(f"'{self.KEY}'"), self.KEY)

    def test_smart_quotes_are_dropped(self):
        """A key copied out of a word processor or a chat carries these."""
        self.assertEqual(self.clean(f"\u201c{self.KEY}\u201d"), self.KEY)
        self.assertEqual(self.clean(f"\u2018{self.KEY}\u2019"), self.KEY)

    def test_a_json_value_loses_its_quotes_and_comma(self):
        self.assertEqual(self.clean(f'"{self.KEY}",'), self.KEY)

    def test_backticks_are_dropped(self):
        self.assertEqual(self.clean(f"`{self.KEY}`"), self.KEY)

    def test_an_authorization_header_keeps_only_the_credential(self):
        """The whole header value is a common paste, and used to be rejected
        outright with a message that named no cause."""
        self.assertEqual(self.clean(f"Bearer {self.KEY}"), self.KEY)
        self.assertEqual(self.clean(f"bearer {self.KEY}"), self.KEY)
        self.assertEqual(self.clean(f"Authorization: Bearer {self.KEY}"), self.KEY)

    def test_an_env_assignment_keeps_only_the_credential(self):
        self.assertEqual(self.clean(f"BLABLADOR_API_KEY={self.KEY}"), self.KEY)
        self.assertEqual(self.clean(f"key={self.KEY}"), self.KEY)

    def test_a_key_with_a_space_inside_is_still_refused(self):
        from jobs.forms import ApiKeyForm

        form = ApiKeyForm({"api_key": "glpat-abcd efgh ijkl mnop qrst"})
        self.assertFalse(form.is_valid())
        self.assertIn("unbroken", " ".join(form.errors["api_key"]))

    def test_a_key_too_short_to_be_one_is_refused(self):
        from jobs.forms import ApiKeyForm

        form = ApiKeyForm({"api_key": "short"})
        self.assertFalse(form.is_valid())


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


class SirtfiTests(Base):
    """Checks behind the Sirtfi assertions that are code, not policy."""

    CLAIMS = {
        "sub": "alice-sub",
        "email": "alice@fz-juelich.de",
        "given_name": "Alice",
        "family_name": "Example",
    }

    def _backend(self):
        from jobs.auth import JscOIDCBackend

        return JscOIDCBackend()

    def test_os4_deactivated_user_cannot_sign_in_with_oidc(self):
        """[OS4] access rights can be terminated.

        mozilla-django-oidc's get_or_create_user returns a matched user
        without consulting user_can_authenticate(), so is_active must be
        enforced by us or deactivating an account would not lock anyone out.
        """
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        backend = self._backend()
        with mock.patch.object(backend, "get_userinfo", return_value=self.CLAIMS):
            with mock.patch.object(backend, "get_token", return_value={"access_token": "x", "id_token": "y"}):
                with mock.patch.object(backend, "verify_token", return_value=self.CLAIMS):
                    user = backend.get_or_create_user("tok", "idtok", self.CLAIMS)
        self.assertIsNone(user, "a deactivated user was allowed to authenticate")

    def test_os4_active_user_still_signs_in(self):
        backend = self._backend()
        with mock.patch.object(backend, "get_userinfo", return_value=self.CLAIMS):
            user = backend.get_or_create_user("tok", "idtok", self.CLAIMS)
        self.assertEqual(user, self.user)

    def test_a_federated_login_takes_the_password_off_the_account_it_matches(self):
        """An account made before the AAI client existed must not keep a way in.

        Accounts are matched by email address, and while sign-up is open anyone
        can claim an address. Once the real holder signs in through AAI, the
        local password must stop working - otherwise whoever registered it
        first keeps a way into an account that is not theirs.
        """
        self.assertTrue(self.user.has_usable_password())
        backend = self._backend()
        with mock.patch.object(backend, "get_userinfo", return_value=self.CLAIMS):
            user = backend.get_or_create_user("tok", "idtok", self.CLAIMS)
        self.assertEqual(user, self.user)
        self.assertFalse(User.objects.get(pk=self.user.pk).has_usable_password())
        self.client.logout()
        self.assertFalse(
            self.client.login(username="alice", password="pw"),
            "the local password still opened a federated account",
        )

    def test_the_staff_account_keeps_its_password(self):
        """The administrator's login is local on purpose; email is not its key."""
        staff = User.objects.create_user(
            "operator", "operator@fz-juelich.de", "pw", is_staff=True
        )
        backend = self._backend()
        claims = dict(self.CLAIMS, sub="operator-sub", email="operator@fz-juelich.de")
        with mock.patch.object(backend, "get_userinfo", return_value=claims):
            user = backend.get_or_create_user("tok", "idtok", claims)
        self.assertEqual(user, staff)
        self.assertTrue(User.objects.get(pk=staff.pk).has_usable_password())

    def test_tr1_successful_login_is_logged_with_actor_and_time(self):
        """[TR1] retain who did what, when."""
        with self.assertLogs("jobs.audit", level="INFO") as caught:
            self.client.force_login(self.user)
        joined = " ".join(caught.output)
        self.assertIn("login", joined)
        self.assertIn(self.user.username, joined)

    def test_tr1_failed_login_is_logged(self):
        with self.assertLogs("jobs.audit", level="WARNING") as caught:
            self.client.post(
                reverse("login"), {"username": "alice", "password": "wrong"}
            )
        self.assertIn("login-failed", " ".join(caught.output))

    def test_tr1_audit_log_never_contains_the_api_key(self):
        with self.assertLogs("jobs.audit", level="INFO") as caught:
            self.client.force_login(self.user)
        self.assertNotIn("alice-personal-key", " ".join(caught.output))

    def test_ur1_rules_of_use_are_published(self):
        """[UR1] the participant has defined rules and conditions of use."""
        resp = self.client.get(reverse("jobs:terms"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Acceptable use")


class HandoverTests(Base):
    """The operator's page: who may see it, and that its copies cannot drift."""

    def as_staff(self):
        self.user.is_staff = True
        self.user.save()
        return self.client

    def test_the_docs_site_is_not_stale(self):
        """A form, a caveat or the handover template cannot change alone."""
        root = Path(__file__).resolve().parent.parent
        proc = subprocess.run(
            [sys.executable, str(root / "docs" / "build.py"), "--check"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_it_is_hidden_while_the_service_is_unauthorised(self):
        """Not a security boundary - a page about open blockers is not shown
        to users who were just told the service works."""
        with override_settings(OIDC_RP_CLIENT_ID="", OIDC_ALLOWED_ENTITLEMENTS=[]):
            self.assertEqual(
                self.client.get(reverse("jobs:handover")).status_code, 404
            )
            self.assertNotContains(
                self.client.get(reverse("jobs:index")), "Notes for the SimLab"
            )

    def test_it_appears_once_an_aai_client_is_restricted(self):
        with override_settings(
            OIDC_RP_CLIENT_ID="bioportal",
            OIDC_ALLOWED_ENTITLEMENTS=["https://login.helmholtz.de/.../group#member"],
        ):
            self.assertContains(self.client.get(reverse("jobs:handover")), "For the SimLab")
            self.assertContains(
                self.client.get(reverse("jobs:index")), "Notes for the SimLab"
            )

    def test_one_unrestricted_client_does_not_publish_it(self):
        """A client with no entitlement list accepts any AAI login, so the
        service is not authorised yet and the page stays out of the way."""
        with override_settings(
            OIDC_RP_CLIENT_ID="bioportal", OIDC_ALLOWED_ENTITLEMENTS=[]
        ):
            self.assertEqual(
                self.client.get(reverse("jobs:handover")).status_code, 404
            )

    def test_staff_can_always_read_it(self):
        self.as_staff()
        with override_settings(OIDC_RP_CLIENT_ID="", OIDC_ALLOWED_ENTITLEMENTS=[]):
            self.assertEqual(
                self.client.get(reverse("jobs:handover")).status_code, 200
            )

    def test_it_names_the_blockers_and_the_decisions_not_to_undo(self):
        self.as_staff()
        body = self.client.get(reverse("jobs:handover")).content.decode()
        # Blocked on people, not on code.
        self.assertIn("OIDC_ALLOWED_ENTITLEMENTS", body)
        self.assertIn("PRIVACY_APPROVED", body)
        self.assertIn("TERMS_APPROVED", body)
        # And the traps a new operator would otherwise undo by accident.
        self.assertIn("No progress bar", body)
        self.assertIn("never logged", " ".join(body.split()))

    def test_both_stylesheets_style_the_shared_markup(self):
        """The page is written once but styled twice: the portal's app.css and
        the docs site's docs.css. A class defined in only one of them renders
        as an unstyled box on the other site - which is how the callout and the
        wide table shipped broken the first time."""
        root = Path(__file__).resolve().parent.parent
        templ = (root / "templates" / "jobs" / "handover.html").read_text()
        used = {c for group in re.findall(r'class="([^"]+)"', templ)
                for c in group.split()}
        app = (root / "static" / "app.css").read_text()
        docs = (root / "docs" / "docs.css").read_text()
        # The portal styles the table through a class; the docs site styles a
        # bare <table> instead, which is why .jobs may live in one sheet only.
        for name in sorted(used - {"jobs"}):
            self.assertIn("." + name, app, f"static/app.css does not style .{name}")
            self.assertIn("." + name, docs, f"docs/docs.css does not style .{name}")
        self.assertIn("table.jobs", app)
        self.assertIn("table {", docs)

    def test_it_matches_the_docs_site_copy(self):
        """docs/handover.html wraps the template; never a second version."""
        root = Path(__file__).resolve().parent.parent
        templ = (root / "templates" / "jobs" / "handover.html").read_text()
        body = templ[templ.index("<h1>"): templ.rindex("{% endblock %}")]
        page = (root / "docs" / "handover.html").read_text()
        self.assertIn(body.strip(), page)


# --- untrusted artifacts ---------------------------------------------------


class ArchiveBombTests(Base):
    """An artifact is untrusted input, parsed by the web process on demand.

    A ZIP can unpack to far more than it weighs, and a gzip member inside it
    can stack that again, so a small archive from the queue could otherwise
    exhaust a web worker's memory the moment a user opened the preview.
    """

    def _archive(self, name: str, payload: bytes) -> Path:
        import tempfile
        import zipfile

        path = Path(tempfile.mkdtemp()) / "result.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(name, payload)
        return path

    def test_zip_that_unpacks_hugely_is_refused(self):
        from jobs import structure

        archive = self._archive("big.pdb", b"A" * (200 * 1024 * 1024))
        # The archive is small on disk; that is the whole point of the bombe.
        self.assertLess(archive.stat().st_size, 1024 * 1024)
        self.assertIsNone(structure.extract_preview(archive))
        report = structure.check_archive(archive)
        self.assertEqual(report["verdict"], structure.Verdict.UNKNOWN)
        self.assertIn("preview limit", report["reason"])

    def test_gzip_member_that_expands_hugely_is_refused(self):
        import gzip

        from jobs import structure

        archive = self._archive(
            "s.pdb.gz", gzip.compress(b"B" * (200 * 1024 * 1024))
        )
        self.assertIsNone(structure.extract_preview(archive))
        report = structure.check_archive(archive)
        self.assertEqual(report["verdict"], structure.Verdict.UNKNOWN)
        self.assertIn("expands past", report["reason"])

    def test_a_real_structure_still_parses(self):
        from jobs import structure

        pdb = (
            "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00 50.00           C\n"
            "ATOM      2  CA  ALA A   2       3.800   0.000   0.000  1.00 50.00           C\n"
            "END\n"
        )
        archive = self._archive("ok.pdb", pdb.encode())
        name, text = structure.extract_preview(archive)
        self.assertEqual(name, "ok.pdb")
        self.assertIn("ATOM", text)


# --- the API key may only ever reach the configured host -------------------


class CredentialEgressTests(Base):
    """Every request carries a user's key, so redirects are an exfiltration path.

    urllib re-sends request headers when it follows a redirect - including when
    it rewrites a POST into a GET - so a 302 from the service, or from a proxy
    in front of it, would hand one user's credential to whatever host the
    Location named. Neither a redirect nor a downgrade to plain HTTP may happen.
    """

    def test_a_redirect_is_refused_rather_than_followed(self):
        import urllib.error

        redirect = urllib.error.HTTPError(
            "https://api.example/v1/responses", 302, "Found",
            {"Location": "https://collector.example/steal"}, io.BytesIO(b""),
        )
        with mock.patch.object(blablador._opener, "open", side_effect=redirect):
            with self.assertRaises(blablador.BlabladorError) as ctx:
                blablador.status("k", "alias-esmfold2", JOB_ID)
        # 302 is a server-side problem, not evidence the key is bad.
        self.assertNotIsInstance(ctx.exception, blablador.InvalidCredential)

    def test_a_plain_http_base_is_refused(self):
        with override_settings(BLABLADOR_API_BASE="http://api.example/v1"):
            with self.assertRaises(blablador.BlabladorError) as ctx:
                blablador.status("k", "alias-esmfold2", JOB_ID)
        self.assertIn("https", str(ctx.exception))

    def test_the_redirect_handler_does_not_forward_the_request(self):
        """The guard itself, independent of any patching."""
        import urllib.request

        req = urllib.request.Request(
            "https://api.example/v1/responses",
            data=b"{}",
            headers={"Authorization": "Bearer secret"},
        )
        with self.assertRaises(blablador.BlabladorError):
            blablador._NoRedirect().redirect_request(
                req, None, 302, "Found", {}, "https://collector.example/steal"
            )


# --- attribution -----------------------------------------------------------


class AttributionTests(Base):
    """The trail must say whether a person or the service acted.

    An unattended reconciler and a signed-in owner both change a job's state.
    "The job was cancelled" is a different incident depending on which of them
    did it, and a trail that cannot tell them apart is no use in a review.
    """

    def test_creating_a_job_is_recorded_as_the_user(self):
        job = self.make_job()
        JobEvent.objects.create(job=job, kind="created", actor=Actor.USER)
        self.assertEqual(job.events.get().actor, Actor.USER)

    def test_the_reconciler_marks_its_own_events(self):
        job = self.make_job(status=Status.RUNNING, started_at=None)
        with mock.patch(
            "jobs.blablador._post", return_value=remote_job(status="completed")
        ):
            Command().poll_active()
        actors = set(job.events.values_list("actor", flat=True))
        self.assertIn(Actor.RECONCILER, actors)
        self.assertNotIn(Actor.USER, actors)

    def test_a_status_change_leaves_one_event_naming_the_actor(self):
        job = self.make_job(status=Status.RUNNING)
        with mock.patch(
            "jobs.blablador._post", return_value=remote_job(status="completed")
        ):
            Command().poll_active()
        event = job.events.get(kind="status")
        self.assertEqual(event.actor, Actor.RECONCILER)
        self.assertIn("running -> completed", event.detail)

    def test_rejected_credential_is_recorded_as_the_reconciler(self):
        job = self.make_job(status=Status.RUNNING)
        with mock.patch(
            "jobs.blablador._post",
            side_effect=blablador.InvalidCredential("401", status=401),
        ):
            Command().poll_active()
        event = job.events.get(kind="credential-rejected")
        self.assertEqual(event.actor, Actor.RECONCILER)


# --- the credential lifecycle is auditable --------------------------------


class CredentialAuditTests(Base):
    def test_storing_a_key_is_audited_without_the_key(self):
        # Start with no key, so this is a first store rather than a replace.
        ApiCredential.objects.filter(user=self.user).delete()
        with self.assertLogs("jobs.audit", level="INFO") as logs:
            ApiCredential.store(self.user, "glpat-abcdefghijklmnopqrst")
        line = "\n".join(logs.output)
        self.assertIn("credential-stored", line)
        self.assertIn("user=alice", line)
        # A fingerprint, so a later incident can tell which key was in place.
        self.assertIn("fingerprint=", line)
        self.assertNotIn("glpat-abcde", line)

    def test_replacing_a_key_is_distinguishable_from_the_first(self):
        ApiCredential.store(self.user, "glpat-firstkey0000000000")
        with self.assertLogs("jobs.audit", level="INFO") as logs:
            ApiCredential.store(self.user, "glpat-secondkey000000000")
        self.assertIn("credential-replaced", "\n".join(logs.output))

    def test_removing_a_key_is_audited(self):
        cred = ApiCredential.store(self.user, "glpat-abcdefghijklmnopqrst")
        with self.assertLogs("jobs.audit", level="INFO") as logs:
            cred.delete()
        self.assertIn("credential-removed", "\n".join(logs.output))

    def test_a_successful_poll_stamps_last_used(self):
        """Otherwise a key that died months ago looks freshly used."""
        job = self.make_job(status=Status.RUNNING)
        cred = ApiCredential.store(self.user, "glpat-abcdefghijklmnopqrst")
        self.assertIsNone(cred.last_used_at)
        with mock.patch(
            "jobs.blablador._post", return_value=remote_job(status="running")
        ):
            Command().poll_active()
        cred.refresh_from_db()
        self.assertIsNotNone(cred.last_used_at)


# --- only one sweep downloads an archive ----------------------------------


class ArtifactClaimTests(Base):
    """A second sweep must not refetch an archive the first is already fetching.

    The download is metered against the owner's own quota, so a duplicate fetch
    is a duplicate charge, and it is what a pod restart or an overlapping sweep
    produces naturally.
    """

    def test_a_job_already_downloading_is_not_claimed_again(self):
        from jobs.models import ArtifactState

        self.make_job(status=Status.COMPLETED, artifact_state=ArtifactState.DOWNLOADING)
        with mock.patch("jobs.blablador.fetch_artifact") as fetch:
            Command().download_artifacts()
        fetch.assert_not_called()

    def test_a_pending_job_is_claimed_exactly_once(self):
        from jobs.models import ArtifactState

        job = self.make_job(status=Status.COMPLETED, artifact_state=ArtifactState.PENDING)
        calls = []

        def fake_fetch(key, model, remote, dest):
            calls.append(job.pk)
            # A competing sweep sees DOWNLOADING, not PENDING, so it skips.
            self.assertEqual(
                Job.objects.get(pk=job.pk).artifact_state,
                ArtifactState.DOWNLOADING,
            )
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"PK\x05\x06" + b"\0" * 18)
            return 22

        with mock.patch("jobs.blablador.fetch_artifact", side_effect=fake_fetch):
            Command().download_artifacts()
        self.assertEqual(calls, [job.pk])


class BusyQueueTests(Base):
    """Someone about to wait behind other people should be told so."""

    def _others(self, n_people, jobs_each=1, **over):
        for i in range(n_people):
            other = User.objects.create_user(f"other{i}", f"o{i}@x.de", "pw")
            for _ in range(jobs_each):
                self.make_job(owner=other, remote_job_id="", **over)

    def test_submit_page_quiet_when_alone(self):
        self.make_job()  # the user's own job does not count
        resp = self.client.get(reverse("jobs:submit", args=["esmfold2"]))
        self.assertNotContains(resp, "queue is busy")

    def test_submit_page_counts_people_and_jobs(self):
        self._others(2, jobs_each=2)
        resp = self.client.get(reverse("jobs:submit", args=["esmfold2"]))
        self.assertContains(resp, "queue is busy")
        self.assertContains(resp, "2 other people have")
        self.assertContains(resp, "4 ")

    def test_finished_jobs_do_not_count(self):
        self._others(1, status=Status.COMPLETED)
        resp = self.client.get(reverse("jobs:submit", args=["esmfold2"]))
        self.assertNotContains(resp, "queue is busy")

    def test_names_nobody(self):
        self._others(1)
        resp = self.client.get(reverse("jobs:submit", args=["esmfold2"]))
        self.assertContains(resp, "1 other person has")
        self.assertNotContains(resp, "other0")

    def test_queued_job_is_told(self):
        job = self.make_job()
        self._others(1)
        resp = self.client.get(reverse("jobs:status", args=[job.pk]))
        self.assertContains(resp, "queue is busy")

    def test_running_job_is_not_told(self):
        job = self.make_job(status=Status.RUNNING)
        self._others(1)
        resp = self.client.get(reverse("jobs:status", args=[job.pk]))
        self.assertNotContains(resp, "queue is busy")
