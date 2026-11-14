"""RFdiffusion3 — molecular design from an official RFD3 input object."""

from __future__ import annotations

import json

from django import forms

from jobs.forms import BaseJobForm, PDBTextField
from jobs.registry import ModelSpec, register

LENGTH_RE = r"^\d+(-\d+)?$"


class RFdiffusion3Form(BaseJobForm):
    MODES = [
        ("unconditional", "Unconditional design (validated)"),
        ("advanced", "Advanced: supply an RFD3 input object"),
    ]
    mode = forms.ChoiceField(choices=MODES, initial="unconditional")
    length = forms.RegexField(
        regex=LENGTH_RE,
        initial="40-50",
        required=False,
        label="Length",
        help_text="Residues, e.g. 45 or 40-50. Used in unconditional mode.",
    )
    rfd3_input = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 10, "spellcheck": "false"}),
        required=False,
        label="RFD3 input object",
        help_text=(
            "A JSON object in the official RFdiffusion3 input schema - not an "
            "English prompt. Refer to an attached file by its filename."
        ),
    )
    attachment = PDBTextField(
        required=False,
        label="Attachment (optional)",
        help_text="A PDB referred to by name from the input object above.",
    )
    attachment_name = forms.RegexField(
        regex=r"^[A-Za-z0-9._-]+$",
        required=False,
        initial="target.pdb",
        help_text="The filename your input object refers to.",
    )
    samples = forms.IntegerField(min_value=1, max_value=128, initial=1)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("mode") == "unconditional":
            if not cleaned.get("length"):
                self.add_error("length", "A length is required for this mode.")
        else:
            raw = (cleaned.get("rfd3_input") or "").strip()
            if not raw:
                self.add_error("rfd3_input", "An RFD3 input object is required.")
            else:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    self.add_error("rfd3_input", f"Not valid JSON: {exc.msg}")
                else:
                    if not isinstance(parsed, dict):
                        self.add_error("rfd3_input", "Must be a JSON object.")
                    else:
                        cleaned["rfd3_input_parsed"] = parsed
        if cleaned.get("attachment") and not cleaned.get("attachment_name"):
            self.add_error("attachment_name", "Name the attachment so the input can refer to it.")
        return cleaned


def build(cleaned: dict) -> dict:
    if cleaned["mode"] == "unconditional":
        params = {"input": {"api": {"length": cleaned["length"]}}}
    else:
        params = {"input": cleaned["rfd3_input_parsed"]}
    params["samples"] = cleaned["samples"]
    if cleaned.get("attachment"):
        # Filenames only; the adapter rejects server paths and traversal.
        params["files"] = {cleaned["attachment_name"]: cleaned["attachment"]}
    return params


SPEC = register(ModelSpec(
    slug="rfdiffusion3",
    alias="alias-rfdiffusion3",
    name="RFdiffusion3",
    blurb="Generates molecular designs from an official RFD3 input object.",
    form_class=RFdiffusion3Form,
    params_builder=build,
    result_template="jobs/results/_designs.html",
    caveats=(
        "Unconditional 40-50 residue design is the validated path. Other input "
        "fields are passed through to RFD3 but were not covered by deployment "
        "validation.",
    ),
))
