#!/usr/bin/env python
"""Capture screenshots of the running portal for the README and docs site.

    python manage.py runserver 8731          # in another shell, with DEBUG=1
    python docs/screenshots.py

Shots are written to docs/img/. They are taken against a real database, so run
this against an instance with a few finished jobs in it rather than an empty
one.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8731"
USERNAME = "admin"
PASSWORD = "devpass"

OUT = Path(__file__).resolve().parent / "img"
OUT.mkdir(parents=True, exist_ok=True)

# path, filename, what to wait for, whether to capture the full page
SHOTS = [
    ("/", "home", None, True),
    ("/models/esmfold2/", "submit-esmfold2", "form.jobform", True),
    ("/jobs/", "job-list", "table.jobs", False),
    ("/jobs/6/", "job-detail", "#viewer canvas", False),
    ("/queue/", "queue", "table.jobs", False),
    ("/account/key/", "api-key", "form.jobform", False),
]


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for theme in ("light", "dark"):
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 860},
                device_scale_factor=2,
                color_scheme=theme,
            )
            page = ctx.new_page()

            # Log in once per context.
            page.goto(f"{BASE}/login/", wait_until="networkidle")
            page.click("details.localauth summary")
            page.fill("input[name=username]", USERNAME)
            page.fill("input[name=password]", PASSWORD)
            page.click("details.localauth button[type=submit]")
            page.wait_for_load_state("networkidle")

            for path, name, wait_for, full in SHOTS:
                page.goto(f"{BASE}{path}", wait_until="networkidle")
                if wait_for:
                    try:
                        page.wait_for_selector(wait_for, timeout=20000)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  ! {name} ({theme}): {wait_for} never appeared: {exc}")
                if "canvas" in (wait_for or ""):
                    # Give NGL a moment to finish its first render.
                    page.wait_for_timeout(2500)
                suffix = "" if theme == "light" else "-dark"
                target = OUT / f"{name}{suffix}.png"
                page.screenshot(path=str(target), full_page=full)
                print(f"  {target.relative_to(OUT.parent.parent)}")
            ctx.close()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
