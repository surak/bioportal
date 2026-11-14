#!/usr/bin/env python
"""Generate the GitHub Pages documentation from the model registry.

Field names, limits and caveats are read out of the real forms and ModelSpecs,
so the published docs cannot drift from what the portal actually accepts.

    python docs/build.py        # writes docs/*.html
"""

from __future__ import annotations

import html
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bioportal.settings")

import django  # noqa: E402

django.setup()

from django import forms  # noqa: E402

from jobs.registry import all_specs  # noqa: E402

E = html.escape


def nav(current: str, specs) -> str:
    links = [("index.html", "Overview")]
    links += [(f"{s.slug}.html", s.name) for s in specs]
    links += [("operating.html", "Running the portal")]
    items = "".join(
        f'<a href="{href}"{" class=\'on\'" if href == current else ""}>{E(text)}</a>'
        for href, text in links
    )
    return f"<nav class='sitenav'>{items}</nav>"


def page(current: str, specs, title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{E(title)} · BioPortal</title>
<link rel="stylesheet" href="docs.css">
</head>
<body>
<header class="masthead">
  <a class="brand" href="index.html">BioPortal</a>
  <span class="tagline">Molecular models on Blablador</span>
</header>
{nav(current, specs)}
<main>
{body}
</main>
<footer>
  <p>Jülich Supercomputing Centre. Generated from the model registry by
  <code>docs/build.py</code>.</p>
</footer>
</body>
</html>
"""


def describe_field(name: str, field: forms.Field) -> dict:
    kind = type(field).__name__.replace("Field", "")
    limits = []
    for attr, label in (("min_value", "min"), ("max_value", "max")):
        value = getattr(field, attr, None)
        if value is not None:
            limits.append(f"{label} {value}")
    if isinstance(field, forms.ChoiceField):
        limits.append("one of: " + ", ".join(c[0] for c in field.choices))
    if getattr(field, "max_length", None):
        limits.append(f"max length {field.max_length}")
    return {
        "name": name,
        "label": str(field.label or name.replace("_", " ")).strip(),
        "kind": kind,
        "required": field.required,
        "initial": field.initial,
        "limits": ", ".join(limits),
        "help": str(field.help_text or ""),
    }


def field_table(spec) -> str:
    form = spec.form_class()
    rows = []
    for name, field in form.fields.items():
        if name in ("label", "notify_by_email"):
            continue
        f = describe_field(name, field)
        rows.append(
            "<tr>"
            f"<td><code>{E(f['name'])}</code></td>"
            f"<td>{E(f['label'])}</td>"
            f"<td>{'yes' if f['required'] else 'no'}</td>"
            f"<td>{E(str(f['initial']) if f['initial'] is not None else '—')}</td>"
            f"<td>{E(f['limits']) or '—'}</td>"
            f"<td>{E(f['help'])}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Field</th><th>Shown as</th><th>Required</th>"
        "<th>Default</th><th>Accepted</th><th>Notes</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def model_page(spec, specs) -> str:
    caveats = ""
    if spec.caveats:
        items = "".join(f"<li>{E(c)}</li>" for c in spec.caveats)
        caveats = (
            "<div class='callout warn'><h2>Before you run this</h2>"
            f"<ul>{items}</ul></div>"
        )
    note = f"<p>{E(spec.result_note)}</p>" if spec.result_note else ""
    return page(
        f"{spec.slug}.html",
        specs,
        spec.name,
        f"""
<h1>{E(spec.name)}</h1>
<p class="lede">{E(spec.blurb)}</p>
<p class="meta">API model id <code>{E(spec.alias)}</code></p>

{caveats}

<h2>Inputs</h2>
{field_table(spec)}

<h2>Results</h2>
{note}
<p>
  Results arrive as a single <code>result.zip</code>. The archive may contain
  several files — reports, scores and more than one structure — so do not
  assume a single PDB. BioPortal copies the archive to its own storage as soon
  as the job finishes, which is what makes it available after the service's
  48-hour window closes.
</p>
""",
    )


def index_page(specs) -> str:
    cards = "".join(
        f"""<article class="card">
  <h3><a href="{s.slug}.html">{E(s.name)}</a></h3>
  <p>{E(s.blurb)}</p>
  <p class="meta"><code>{E(s.alias)}</code></p>
</article>"""
        for s in specs
    )
    return page(
        "index.html",
        specs,
        "Overview",
        f"""
<h1>Submit a job, close the tab, come back later</h1>
<p class="lede">
  BioPortal is a web frontend for five molecular models running on shared GPUs
  at the Jülich Supercomputing Centre. Sign in with your JSC account, fill in a
  form, and collect your results when the job is done.
</p>

<div class="cards">{cards}</div>

<h2>How a job works</h2>
<ol class="steps">
  <li><strong>Submit once.</strong> Your request goes into a queue shared with
      every other user of the service.</li>
  <li><strong>Wait.</strong> Jobs can take minutes or hours. Closing the page,
      losing your network connection or reloading does <em>not</em> cancel a
      submitted job.</li>
  <li><strong>Come back.</strong> Your job list keeps every job you have
      submitted. If you asked for an email, you get one when the job finishes.</li>
  <li><strong>Download.</strong> Results are a ZIP archive, kept by BioPortal
      after the service itself has deleted its copy.</li>
</ol>

<h2>What the waiting screen can and cannot tell you</h2>
<p>
  While a job is queued you are shown your position in the queue and why it is
  waiting — for a free GPU, or for a worker for that model. While it runs you
  are shown whatever stage the service reports.
</p>
<div class="callout">
  <p>
    <strong>There is no progress percentage and no estimated finish time.</strong>
    The service does not calculate them, so BioPortal does not invent them.
    Queue position tells you the order requests were accepted; it is not a
    promise about when yours will start, because a job needing fewer GPUs can
    legitimately start before one ahead of it.
  </p>
</div>

<h2>Your own API key</h2>
<p>
  Before you can submit anything you must store your own Blablador API key on
  the <em>API key</em> page. Jobs run against your key, so the GPU time is
  metered to your account rather than a shared one.
</p>
<div class="callout">
  <p>
    The key is encrypted before being stored and is never shown back to you —
    the portal displays only a short fingerprint so you can tell which key is
    in use. It has to be stored rather than kept in your browser session,
    because the portal needs it to follow your job and download your results
    hours later, after you have closed the page. You can remove it at any time;
    removing it does not stop jobs already running, but the portal can no
    longer track them or fetch their results until you add a working key again.
  </p>
</div>

<h2>Results and the 48-hour window</h2>
<p>
  The molecular service deletes results 48 hours after a job reaches a final
  state. BioPortal fetches your archive automatically as soon as your job
  finishes, so you do not have to race that deadline. If an archive was lost
  before it could be copied, the job page says so plainly rather than showing a
  broken link — the job would have to be re-run.
</p>

<h2>Cancelling</h2>
<p>
  You can cancel a job from its page. A queued job is normally dropped straight
  away. A running job keeps going until its process actually stops, so its
  status reads <em>cancelling</em> for a while — and a job can still finish if
  it beats the cancellation. Closing your browser tab is not a cancellation.
</p>

<h2>If something looks wrong</h2>
<ul>
  <li><strong>“We're having trouble reaching the service.”</strong> A connection
      problem on the portal's side. Your job is unaffected and still running;
      the portal keeps retrying.</li>
  <li><strong>A job running far longer than expected.</strong> The job page will
      flag it. The service does not automatically restart jobs whose worker was
      lost, so contact the operators with the job ID shown on the page.</li>
  <li><strong>Limits.</strong> Each account may have a few jobs in flight at a
      time, and the whole service accepts roughly 100 queued jobs. If the queue
      is full your request is kept and submitted automatically — do not resend it.</li>
</ul>

<div class="callout">
  <p>
    Inputs and results are stored on shared institute servers while a job is
    processed and during the retention window. This is not a
    nothing-touches-the-server service; do not submit material that may not be
    stored there.
  </p>
</div>
""",
    )


def operating_page(specs) -> str:
    return page(
        "operating.html",
        specs,
        "Running the portal",
        """
<h1>Running the portal</h1>
<p class="lede">For whoever operates this deployment.</p>

<h2>Two processes</h2>
<p>
  A web process (<code>gunicorn bioportal.wsgi</code>) and exactly one
  reconciler (<code>manage.py reconcile --loop</code>). The web process never
  waits on the molecular service: it records the job and returns. The
  reconciler retries ambiguous submissions, polls each job on the schedule the
  service asks for, downloads finished archives, and sends completion mail.
</p>

<h2>Configuration</h2>
<table>
<thead><tr><th>Setting</th><th>Why it matters</th></tr></thead>
<tbody>
<tr><td><code>CREDENTIAL_ENCRYPTION_KEY</code></td><td>Fernet key used to encrypt each user's stored Blablador key. Lives in the environment, never in the database, and must be identical for the web and reconciler processes. Rotating it makes every stored credential unreadable.</td></tr>
<tr><td><code>ARTIFACT_ROOT</code></td><td>Where result archives are copied. This is what makes results outlive the service's 48-hour retention — size it for your throughput.</td></tr>
<tr><td><code>MAX_ACTIVE_JOBS_PER_USER</code></td><td>Per-user fairness. The whole service accepts about 100 queued jobs and has a limited number of GPU slots, so set this deliberately before opening the portal up.</td></tr>
<tr><td><code>STALE_RUNNING_SECONDS</code></td><td>How long a job may sit in <em>running</em> before it is flagged. Flagged jobs are never auto-failed: a lost worker cannot be distinguished from a slow job.</td></tr>
<tr><td><code>OIDC_ALLOWED_ENTITLEMENTS</code></td><td>Restricts sign-in to your group. Empty accepts any successful Helmholtz AAI login — fine for a pilot, tighten before announcing.</td></tr>
</tbody>
</table>

<h2>The admin</h2>
<p>
  <code>/admin/</code> lists every job with its event trail. The
  <em>Trouble</em> column flags jobs that are stale or that the portal cannot
  reach. Two actions are available: re-send a completion email, and retry an
  archive download — the latter only helps inside the 48-hour window.
</p>

<h2>Adding a model</h2>
<p>
  Add a module under <code>jobs/specs/</code> with a form and a
  <code>build(cleaned)</code> that returns the service's
  <code>parameters</code> object, register a <code>ModelSpec</code> with the
  alias from <code>GET /v1/models</code>, and list the module in
  <code>load_specs()</code>. The homepage, job list, queue view and these docs
  all pick it up automatically.
</p>
""",
    )


def main() -> None:
    specs = all_specs()
    (HERE / "index.html").write_text(index_page(specs))
    (HERE / "operating.html").write_text(operating_page(specs))
    for spec in specs:
        (HERE / f"{spec.slug}.html").write_text(model_page(spec, specs))
    # Tell GitHub Pages to serve these files as-is.
    (HERE / ".nojekyll").write_text("")
    print(f"wrote {2 + len(specs)} pages to {HERE}")


if __name__ == "__main__":
    main()
