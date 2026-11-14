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
        help_text="1-8. More loops, slower.",
    )
    steps = forms.IntegerField(min_value=1, max_value=1000, initial=50)


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
