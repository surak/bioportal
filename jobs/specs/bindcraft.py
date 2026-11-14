"""BindCraft — binder design against an uploaded target."""

from __future__ import annotations

from django import forms

from jobs.forms import BaseJobForm, PDBTextField
from jobs.registry import ModelSpec, register


class BindCraftForm(BaseJobForm):
    target_pdb = PDBTextField(label="Target structure (PDB)")
    chains = forms.RegexField(
        regex=r"^[A-Za-z0-9]+(,[A-Za-z0-9]+)*$",
        initial="A",
        label="Target chains",
        help_text="One or more chain IDs, e.g. A or A,B.",
    )
    binder_length_min = forms.IntegerField(min_value=10, max_value=500, initial=60)
    binder_length_max = forms.IntegerField(min_value=10, max_value=500, initial=100)
    hotspots = forms.CharField(
        required=False,
        label="Hotspots",
        help_text="Native BindCraft hotspot string. Optional.",
    )
    samples = forms.IntegerField(
        min_value=1, max_value=128, initial=1, label="Final designs wanted",
    )
    max_trajectories = forms.IntegerField(
        min_value=1, max_value=10000, initial=100,
        help_text="Upper bound on attempted trajectories (1-10000).",
    )
    walltime_hours = forms.IntegerField(
        min_value=1, max_value=24, initial=24,
        help_text="The job is stopped after this long (max 24 h).",
    )

    def clean(self):
        cleaned = super().clean()
        lo, hi = cleaned.get("binder_length_min"), cleaned.get("binder_length_max")
        if lo and hi and lo > hi:
            self.add_error("binder_length_max", "Maximum must be at least the minimum.")
        return cleaned


def build(cleaned: dict) -> dict:
    params = {
        "target_pdb": cleaned["target_pdb"],
        "chains": cleaned["chains"],
        "binder_length": [cleaned["binder_length_min"], cleaned["binder_length_max"]],
        "samples": cleaned["samples"],
        "max_trajectories": cleaned["max_trajectories"],
        "walltime_seconds": cleaned["walltime_hours"] * 3600,
    }
    if cleaned.get("hotspots"):
        params["hotspots"] = cleaned["hotspots"]
    return params


SPEC = register(ModelSpec(
    slug="bindcraft",
    alias="alias-bindcraft",
    name="BindCraft",
    blurb="Designs binders against a target structure and filters them.",
    form_class=BindCraftForm,
    params_builder=build,
    result_template="jobs/results/_designs.html",
    result_note="Examine the reports and accepted-design outputs in the archive.",
    caveats=(
        "Native quality filters can accept fewer designs than you asked for - "
        "a finished run is not a quality guarantee.",
        "Output is not deterministic; this build does not wire a seed into the CLI.",
    ),
))
