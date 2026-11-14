"""Proteina-Complexa — single-pass binder generation."""

from __future__ import annotations

from django import forms

from jobs.forms import BaseJobForm, PDBTextField, ResidueSelectionField
from jobs.registry import ModelSpec, register

# The adapter's fallback selection is the ubiquitin canary. It is meaningless
# for any other target, so this form always requires an explicit selection.
CANARY_SELECTION = "A1-76"


class ProteinaComplexaForm(BaseJobForm):
    target_pdb = PDBTextField(label="Target structure (PDB)")
    target_input = ResidueSelectionField(
        label="Target selection",
        help_text="The chain and residue range to design against, e.g. B20-140.",
    )
    binder_length_min = forms.IntegerField(min_value=10, max_value=500, initial=60)
    binder_length_max = forms.IntegerField(min_value=10, max_value=500, initial=100)
    hotspots = forms.CharField(
        required=False,
        help_text="Optional, comma-separated, e.g. A30,A33.",
    )
    samples = forms.IntegerField(min_value=1, max_value=128, initial=1)
    steps = forms.IntegerField(min_value=1, max_value=1000, initial=400)
    seed = forms.IntegerField(required=False, initial=42)

    def clean(self):
        cleaned = super().clean()
        lo, hi = cleaned.get("binder_length_min"), cleaned.get("binder_length_max")
        if lo and hi and lo > hi:
            self.add_error("binder_length_max", "Maximum must be at least the minimum.")
        return cleaned


def build(cleaned: dict) -> dict:
    params = {
        "target_pdb": cleaned["target_pdb"],
        "target_input": cleaned["target_input"],
        "binder_length": [cleaned["binder_length_min"], cleaned["binder_length_max"]],
        "samples": cleaned["samples"],
        "steps": cleaned["steps"],
    }
    if cleaned.get("seed") is not None:
        params["seed"] = cleaned["seed"]
    if cleaned.get("hotspots"):
        # Complexa takes a list here, unlike BindCraft's hotspot string.
        params["hotspots"] = [h.strip() for h in cleaned["hotspots"].split(",") if h.strip()]
    return params


SPEC = register(ModelSpec(
    slug="proteina-complexa",
    alias="alias-proteina-complexa",
    name="Proteina-Complexa",
    blurb="NVIDIA Proteina-Complexa binder generation against a target.",
    form_class=ProteinaComplexaForm,
    params_builder=build,
    result_template="jobs/results/_designs.html",
    caveats=(
        "This deployment is single-pass binder generation with reward scoring "
        "disabled - not the full reward-guided search and refinement pipeline.",
    ),
))
