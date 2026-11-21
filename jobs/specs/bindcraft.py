"""BindCraft — binder design against an uploaded target."""

from __future__ import annotations

from django import forms
from django.conf import settings

from jobs.forms import BaseJobForm, PDBTextField, count_residues
from jobs.registry import ModelSpec, register


class BindCraftForm(BaseJobForm):
    target_pdb = PDBTextField(label="Target structure (PDB)")
    chains = forms.RegexField(
        regex=r"^[A-Za-z0-9]+(,[A-Za-z0-9]+)*$",
        initial="A",
        label="Target chains",
        help_text="One or more chain IDs, e.g. A or A,B.",
    )
    # Upstream: "The maximum size of reliably generated binders is currently
    # 250 amino acids", with 60-180 called the optimal range.
    binder_length_min = forms.IntegerField(min_value=10, max_value=250, initial=60)
    binder_length_max = forms.IntegerField(
        min_value=10, max_value=250, initial=100,
        help_text="60-180 is the range upstream calls optimal; 250 is its ceiling.",
    )
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
        self._check_fits_on_one_gpu(cleaned, hi)
        return cleaned

    def _check_fits_on_one_gpu(self, cleaned: dict, binder_max: int | None) -> None:
        """Refuse complexes too big for a single card, while it is still cheap.

        A trajectory cannot be split across GPUs, so this is not a queueing
        problem that waiting solves: the only ways through are a smaller
        target or a smaller binder. Saying so now beats an out-of-memory
        death after an hour in the queue.
        """
        pdb, chains = cleaned.get("target_pdb"), cleaned.get("chains")
        if not pdb or not chains or not binder_max:
            return
        wanted = [c.strip() for c in chains.split(",") if c.strip()]
        target = count_residues(pdb, wanted)
        if not target:
            self.add_error("chains", (
                f"No ATOM records for chain{'s' if len(wanted) > 1 else ''} "
                f"{', '.join(wanted)} in that file. Check the chain IDs."
            ))
            return
        budget = settings.BINDCRAFT_RESIDUE_BUDGET
        total = target + binder_max
        if total > budget:
            self.add_error("target_pdb", (
                f"Chain{'s' if len(wanted) > 1 else ''} {', '.join(wanted)} "
                f"hold {target} residues, and with a binder of up to "
                f"{binder_max} that is {total} residues in the folded complex. "
                f"This portal's limit is {budget}, the most that fits in one "
                f"GPU's memory — and BindCraft cannot spread one design across "
                f"several GPUs. Trim the target to the part that matters for "
                f"binding (upstream recommends this for speed regardless), "
                f"name only the chains you need, and use hotspots to point the "
                f"search at the interface. A shorter maximum binder length "
                f"also buys room."
            ))


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
        "One design runs on one GPU and cannot be split across several, so the "
        "target plus the binder must fit in a single card's memory. Trim the "
        "target to the part that matters for binding before uploading it.",
    ),
))
