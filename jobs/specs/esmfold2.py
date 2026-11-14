"""ESMFold2 — full structure prediction (not ESMFold2 Fast)."""

from __future__ import annotations

from django import forms

from jobs.forms import BaseJobForm, SequenceField
from jobs.registry import ModelSpec, register


class ESMFold2Form(BaseJobForm):
    sequence = SequenceField(
        label="Protein sequence",
        help_text="FASTA or bare one-letter sequence.",
    )
    num_loops = forms.IntegerField(
        min_value=1, max_value=8, initial=3, label="Loops",
        help_text="1-8. More loops, slower, generally better.",
    )
    steps = forms.IntegerField(
        min_value=1, max_value=1000, initial=50,
        help_text=(
            "Sampling steps. Low values finish quickly but can return a "
            "structure that is not physically foldable."
        ),
    )

    def clean(self):
        cleaned = super().clean()
        steps = cleaned.get("steps")
        loops = cleaned.get("num_loops")
        # Measured: 1 loop / 10 steps returned a backbone whose consecutive
        # CA atoms averaged 10 A apart, against the ~3.8 A that chemistry
        # allows. The service accepts these values; the result is unusable.
        if steps is not None and loops is not None and steps < 25 and loops < 2:
            self.add_error(
                "steps",
                "This combination of steps and loops has been observed to "
                "produce structures that are not physically foldable. Use at "
                "least 25 steps, or 2 loops.",
            )
        return cleaned


def build(cleaned: dict) -> dict:
    return {
        "sequence": cleaned["sequence"],
        "num_loops": cleaned["num_loops"],
        "steps": cleaned["steps"],
    }


SPEC = register(ModelSpec(
    slug="esmfold2",
    alias="alias-esmfold2",
    name="ESMFold2",
    blurb="Full structure prediction from a single protein sequence.",
    form_class=ESMFold2Form,
    params_builder=build,
    result_template="jobs/results/_structure.html",
    result_note="The result archive contains structure.pdb.",
    caveats=(
        "Produces exactly one prediction. The sequence path ignores sample "
        "count and seed.",
    ),
))
