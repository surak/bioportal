# BioPortal

Web frontend for the five molecular models on Blablador: Proteina-Complexa,
BindCraft, RFdiffusion3, ESMFold2 and BioEmu2.

One Django app, one page per model. Users sign in with their JSC account,
submit a job, close the tab, and come back — or get an email — when it's done.

Built against `Blablador-Molecular-API-Agent-Handoff.md` (2026-11-14).

## How it fits together

```
browser ──POST──► Django ──POST /v1/responses──► Blablador shim ──► GPU workers
   ▲                 │          (key held here)
   │ HTMX poll       │ Job row
   └─────────────────┤
                     │
          reconcile ─┴── status / artifact ──► local artifact store
                     └──► completion email
```

The browser never talks to Blablador. CORS and direct-browser credential
handling weren't validated in that deployment, and the API key belongs to the
server, so every action is proxied.

Each action is `POST /v1/responses` with `stream: false` and `input` as a JSON
**string**. The outer response completing means the *API action* completed —
never the computation. Only `molecular_job` is believed.

### The four jobs of the reconciler

`manage.py reconcile --loop` runs 24/7 and is the only thing that advances a job:

1. **Retries ambiguous submissions.** A `pending` row with no job ID is resent
   with the *same* `request_id` and parameters; the shim derives a stable job ID
   from that pair, so a retry returns the original job instead of a duplicate.
2. **Polls** each job at its own `poll_after_seconds` (never faster than 10s).
3. **Downloads artifacts** as soon as a job completes — see retention, below.
4. **Flags stale jobs** for operations.

## Things the protocol forces, and how they're handled

**Results expire after 48 hours.** This is the sharpest edge. The reconciler
copies the archive to `ARTIFACT_ROOT` the moment a job completes rather than
waiting for a user to click download, so a result survives the user being on
holiday. If it expires first, the UI says so plainly instead of showing a dead
link. Admins can retry a fetch from the Django admin while the window is open.

**Artifacts are chunked base64, up to 2 GB, with no download URL.** Assembled
server-side into a `.part` file, offset advanced by *decoded* byte count (not
base64 string length), renamed only when `total_bytes` have arrived — a partial
download can never masquerade as a result. Transient chunk failures retry the
same offset with jittered backoff.

**A failed HTTP request is not a failed computation.** Transient errors only
touch `consecutive_poll_errors` and `poll_after`; job status is left alone and
the page says "we can't reach the service, your job is unaffected". 404 is the
one exception — the job is genuinely gone.

**No upstream job list exists.** Our database is the only record of who owns
which job ID, which is also why `/jobs/` is per-user and there is no global job
browser over shared storage.

**No progress percentage, no ETA.** `estimated_*` fields are documented as null,
so the UI shows queue position, `stage`/`message`, and `waiting_reason`, and
states outright that no ETA exists. A test asserts no percentage or progress bar
creeps in. Queue position is labelled as order, not a start guarantee.

**Cancellation is a race.** A running job goes to `cancelling` and may still
complete. The UI says that rather than implying the job stopped.

**Stale running jobs.** There's no lease-based recovery and no per-job liveness
API upstream, so a lost worker leaves a job `running` forever. After
`STALE_RUNNING_SECONDS` (default 30 h) the job is flagged, surfaced in the admin
"Trouble" column, and the user is told to contact operations with the job ID.
It is deliberately *not* auto-failed — we can't know the computation died.

**Model-specific traps**, encoded in the forms:

- Proteina-Complexa's `target_input` is **required**; the adapter's `A1-76`
  fallback is the ubiquitin canary and is never sent for an uploaded target.
  Its `hotspots` is a list, unlike BindCraft's hotspot string.
- BioEmu2's `msa_mode` is an explicit visible choice, since single-sequence mode
  means no MSA search and differs scientifically from an MSA run.
- RFdiffusion3's `input` is an official RFD3 object; the form validates it as
  JSON and rejects English prompts.
- BindCraft's filters can return fewer designs than requested, and its seed
  isn't wired into the CLI — both stated on the submit page.
- ESMFold2 produces one prediction and ignores `samples`/`seed`.

Validation bounds (`samples` 1–128, `steps` 1–1000, `num_loops` 1–8,
`max_trajectories` 1–10000, `walltime_seconds` ≤ 86400, 12 MB parameters) are
enforced client-side too, so a bad request never leaves the building. They're
bounds, not a promise that a combination fits in GPU memory.

**Logging.** Keys, sequences, structures and full request bodies are never
logged, and completion emails carry no parameters.

## Layout

| Path | What's in it |
|---|---|
| `jobs/blablador.py` | The shim client: actions, typed errors, chunked artifact fetch |
| `jobs/registry.py` | `ModelSpec` — the plugin contract |
| `jobs/specs/*.py` | One module per model: form + `parameters` builder |
| `jobs/management/commands/reconcile.py` | The 24/7 loop |
| `jobs/models.py` | `Job`, including `apply_remote()` |
| `jobs/auth.py` | Helmholtz AAI / JSC OIDC backend |
| `jobs/notify.py` | Completion email, exactly-once |

## Adding a sixth model

Write `jobs/specs/yourmodel.py` with a form and `build(cleaned) -> parameters`,
call `register(ModelSpec(...))` with the alias from `GET /v1/models`, and add the
module to `load_specs()`. It appears on the homepage with its own page, queue
entry and result view.

## Running it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env     # then fill it in
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
```

Production: `gunicorn bioportal.wsgi` behind nginx, plus

```ini
# /etc/systemd/system/bioportal-reconcile.service
[Unit]
Description=BioPortal job reconciler
After=network.target

[Service]
User=bioportal
WorkingDirectory=/srv/bioportal
Environment=BIOPORTAL_ENV_FILE=/srv/bioportal/.env
ExecStart=/srv/bioportal/.venv/bin/python manage.py reconcile --loop
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Run exactly one reconciler. `ARTIFACT_ROOT` needs room for 48 hours of results
and should be backed up or pruned on your own schedule.

## Authentication

- **Users** — `/oidc/authenticate/`, Helmholtz AAI. `OIDC_ALLOWED_ENTITLEMENTS`
  restricts access to your group; empty accepts any successful login, which is
  fine for a pilot but should be tightened before announcing the service.
- **You** — Django local password login at `/login/`, full admin at `/admin/`:
  every job, its events, the "Trouble" column, re-notify and re-fetch actions.

A federated login never grants staff rights; those are set locally.

## Tests

```bash
.venv/bin/python manage.py test jobs
```

38 tests, network stubbed. They cover the protocol details that are easy to get
wrong: `input` is a JSON string, the `output[0].content[0].text` fallback, HTML
gateway errors treated as transient, artifact offsets advanced by decoded bytes,
partial downloads discarded, idempotent resubmission reusing the request ID,
transient failures leaving job status untouched, expired artifacts reported
honestly, stale jobs flagged rather than failed, per-model parameter traps, and
that no invented progress or ETA appears in the UI.

## Not done

- **Not run against the live service.** Every test stubs the network; the shim's
  real responses may differ in detail from the handoff.
- **No billing integration.** Native `gpu_seconds` is displayed as reported;
  there's no currency conversion, because no rate was established.
- **Result previews** are a client-side NGL render of the first structure found
  in the archive. BioEmu2 trajectories and design sets show file listings only.
- **`GET /v1/models` is not consulted at runtime**; aliases are hardcoded in the
  specs. Worth wiring up a readiness check if aliases start moving.
