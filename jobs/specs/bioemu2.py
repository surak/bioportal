"""BioEmu2 — conformational sampling."""

from __future__ import annotations

from django import forms

from jobs.forms import BaseJobForm, SequenceField
from jobs.registry import ModelSpec, register


class BioEmu2Form(BaseJobForm):
    sequence = SequenceField(label="Protein sequence")
    samples = forms.IntegerField(
        min_value=1, max_value=128, initial=1,
        help_text="Conformations to sample (1-128).",
    )
    seed = forms.IntegerField(required=False, initial=42,
        help_text="Optional. Leave blank for a random seed.")
    msa_mode = forms.ChoiceField(
        choices=[("single_sequence", "Single sequence (no MSA search)")],
        initial="single_sequence",
        label="MSA mode",
        help_text=(
            "Single-sequence mode is a deliberate scientific choice: no MSA is "
            "searched and ColabFold is not contacted. Accuracy differs from an "
            "MSA-based run."
        ),
    )


def build(cleaned: dict) -> dict:
    params = {
        "sequence": cleaned["sequence"],
        "samples": cleaned["samples"],
        # Explicit, never defaulted silently - the user chose this above.
        "msa_mode": cleaned["msa_mode"],
    }
    if cleaned.get("seed") is not None:
        params["seed"] = cleaned["seed"]
    return params


SPEC = register(ModelSpec(
    slug="bioemu2",
    alias="alias-bioemu2",
    name="BioEmu2",
    blurb="Samples an ensemble of conformations rather than one structure.",
    form_class=BioEmu2Form,
    params_builder=build,
    result_template="jobs/results/_ensemble.html",
    result_note="The archive may contain trajectory and structure files.",
    caveats=(
        "The sequence shortcut creates protein chain A.",
        "Runs on one GPU. The multi-GPU path was not validated in this rollout.",
    ),
))
