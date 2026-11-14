from __future__ import annotations

import re

from django import forms

# One-letter amino acid codes, plus X for unknown.
AA_RE = re.compile(r"^[ACDEFGHIKLMNPQRSTVWYX]+$")
MAX_SEQUENCE_LEN = 4096

# The shim's limits, enforced here so a bad request never leaves the building.
MAX_PARAM_BYTES = 12_000_000
MAX_STRING_CHARS = 8_000_000


class BaseJobForm(forms.Form):
    """Every model's form carries a label and a notification preference."""

    label = forms.CharField(
        max_length=200,
        required=False,
        help_text="Optional name to find this run again later.",
    )
    notify_by_email = forms.BooleanField(
        required=False, initial=True, label="Email me when it finishes"
    )

    META_FIELDS = ("label", "notify_by_email")


def clean_protein_sequence(raw: str) -> str:
    """Accept FASTA or bare sequence; return a validated bare sequence.

    The shim's shared validator only checks for uppercase A-Z, so the real
    biological check has to happen here.
    """
    lines = [line.strip() for line in raw.strip().splitlines()]
    seq = "".join(line for line in lines if line and not line.startswith(">"))
    seq = seq.upper().replace(" ", "")
    if not seq:
        raise forms.ValidationError("No sequence found.")
    if not AA_RE.match(seq):
        bad = sorted(set(seq) - set("ACDEFGHIKLMNPQRSTVWYX"))
        raise forms.ValidationError(
            f"Not a protein sequence — unexpected characters: {', '.join(bad)}"
        )
    if len(seq) > MAX_SEQUENCE_LEN:
        raise forms.ValidationError(
            f"Sequence is {len(seq)} residues; this portal's limit is {MAX_SEQUENCE_LEN}."
        )
    return seq


class SequenceField(forms.CharField):
    widget = forms.Textarea(attrs={"rows": 6, "spellcheck": "false"})

    def clean(self, value):
        return clean_protein_sequence(super().clean(value))


class PDBTextField(forms.FileField):
    """A PDB upload that is delivered to the shim as text, not a file path."""

    def clean(self, data, initial=None):
        upload = super().clean(data, initial)
        if upload in (None, False):
            return upload
        if upload.size > MAX_STRING_CHARS:
            raise forms.ValidationError("That structure file is too large.")
        raw = upload.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise forms.ValidationError(
                "That file isn't text. Upload a PDB file, not a compressed archive."
            )
        if not _looks_like_pdb(text):
            raise forms.ValidationError(
                "That doesn't look like a PDB file — no ATOM or HETATM records found."
            )
        return text


def _looks_like_pdb(text: str) -> bool:
    return any(
        line.startswith(("ATOM  ", "HETATM")) for line in text.splitlines()[:5000]
    )


def check_parameter_size(parameters: dict) -> None:
    """Guard the shim's 12 MB serialized-parameter cap before submitting."""
    import json

    size = len(json.dumps(parameters).encode())
    if size > MAX_PARAM_BYTES:
        raise forms.ValidationError(
            f"These inputs serialize to {size / 1e6:.1f} MB; the service accepts "
            f"at most {MAX_PARAM_BYTES / 1e6:.0f} MB."
        )


class ResidueSelectionField(forms.CharField):
    """A chain/residue selection such as 'A1-76' or 'B10-54'."""

    SELECTION_RE = re.compile(r"^[A-Za-z0-9]+\d+-\d+$")

    def clean(self, value):
        value = (super().clean(value) or "").strip()
        if not value:
            raise forms.ValidationError("A target selection is required.")
        if not self.SELECTION_RE.match(value):
            raise forms.ValidationError(
                "Use a chain and residue range, for example A1-76."
            )
        return value


class ApiKeyForm(forms.Form):
    api_key = forms.CharField(
        label="Blablador API key",
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}),
        help_text=(
            "Your own key from the Blablador token page. It is encrypted before "
            "being stored and is never shown back to you."
        ),
    )

    def clean_api_key(self):
        key = (self.cleaned_data["api_key"] or "").strip()
        if len(key) < 16 or any(c.isspace() for c in key):
            raise forms.ValidationError("That does not look like an API key.")
        return key
