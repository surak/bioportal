from __future__ import annotations

import re

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

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


def count_residues(pdb_text: str, chains: list[str] | None = None) -> int:
    """Count distinct residues in ATOM records, optionally limited to chains.

    A residue is identified the way the PDB format does: chain, sequence
    number and insertion code together. Alternate locations and multi-atom
    records therefore count once. HETATM is ignored - waters, ions and ligands
    are not what drives the folding complex's size.
    """
    wanted = {c.upper() for c in chains} if chains else None
    seen = set()
    for line in pdb_text.splitlines():
        if not line.startswith("ATOM  "):
            continue
        chain = line[21:22].strip().upper()
        if wanted is not None and chain not in wanted:
            continue
        seen.add((chain, line[22:26].strip(), line[26:27]))
    return len(seen)


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

    # What surrounds a key in the places people copy it from.
    QUOTES = "\"'`\u201c\u201d\u2018\u2019"
    PREFIXES = ("bearer ", "authorization: bearer ")
    # NAME=value, as an env line or an export holds it. Deliberately narrow: a
    # key may itself contain '-', '.' and '=', so only a plain identifier to
    # the left of the first '=' counts as a variable name.
    ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\s*(.+)$", re.S)

    def clean_api_key(self):
        """Store the key itself, not the decorations around it.

        A key is nearly always pasted from somewhere that quotes it: a JSON
        body, an export line, a terminal echo, a chat message that helpfully
        wraps it in backticks. Sent as-is those characters reach the router and
        come back 401, which the service reports as a bad key - and the reader
        then hunts for a mistake they did not make.
        """
        key = (self.cleaned_data["api_key"] or "").strip()
        key = key.strip(self.QUOTES).strip()
        # A whole Authorization header is a common slip; keep the credential.
        lowered = key.lower()
        for prefix in self.PREFIXES:
            if lowered.startswith(prefix):
                key = key[len(prefix):].strip()
                break
        match = self.ASSIGNMENT_RE.match(key)
        if match:
            key = match.group(1).strip()
        # Quotes and separators from a JSON value or an env line.
        while True:
            before = key
            key = key.strip().rstrip(",;").strip().strip(self.QUOTES).strip()
            if key == before:
                break

        if len(key) < 16 or any(c.isspace() for c in key):
            raise forms.ValidationError(
                "That does not look like an API key: it should be one unbroken "
                "string with no spaces in it."
            )
        return key


class SignupForm(UserCreationForm):
    """A local account, for the time without a registered AAI client.

    The email address is required: it is where the completion notice for a job
    goes, and it is what a federated login will be matched to later.
    """

    email = forms.EmailField(
        label="Email address",
        help_text="Where the completion notice for your jobs is sent.",
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email")

    def clean_email(self):
        email = (self.cleaned_data["email"] or "").strip()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(
                "An account with this address already exists. Sign in instead."
            )
        return email
