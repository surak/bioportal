"""A cheap sanity check on returned structures.

A job can finish, report success and hand back a well-formed PDB that is
nevertheless physically impossible — that is what too few sampling steps
produces. The service cannot tell you this and neither can a file-format
check, so the portal measures the one thing that is unambiguous: the distance
between consecutive alpha carbons.

In a real polypeptide that distance is set by covalent geometry and sits very
close to 3.8 A (3.80 for trans peptide bonds; cis-proline gives about 2.9).
A chain whose consecutive CA atoms are 10 A apart is not a folded protein.

This is a plausibility check, not a validation suite: passing it says the
backbone is connected, not that the prediction is correct.
"""

from __future__ import annotations

import gzip
import io
import logging
import math
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

# Trans peptide bonds give ~3.80 A; cis-proline ~2.9. Anything outside this
# window means consecutive residues are not bonded to each other.
CA_MIN = 2.7
CA_MAX = 4.2
# Below this fraction of plausible bonds we tell the user something is wrong.
PLAUSIBLE_FRACTION = 0.9

STRUCTURE_SUFFIXES = (".pdb", ".cif", ".pdb.gz", ".cif.gz")

# An artifact is untrusted input: it is whatever the molecular service's worker
# produced, and it is parsed by the *web* process when a user opens the preview.
# A ZIP can unpack to far more than it weighs (a few KB can claim gigabytes),
# and gzip inside it can stack that again, so an archive taken at face value is
# a way to exhaust a web worker's memory from the queue. These caps are generous
# for real structures - the largest PDB entry is tens of MB - and are what makes
# the bombe harmless rather than fatal.
MAX_PREVIEW_BYTES = 64 * 1024 * 1024


class TooLarge(ValueError):
    """The archive unpacks to more than the portal is willing to parse."""


class Verdict:
    OK = "ok"
    IMPLAUSIBLE = "implausible"
    UNKNOWN = "unknown"


def _read_member(zf: zipfile.ZipFile, name: str) -> str:
    """One member's text, refusing to unpack more than MAX_PREVIEW_BYTES.

    The size is checked against the ZIP's own declaration *and* enforced while
    reading, because the declaration is attacker-controlled too: a header can
    claim 1 MB and still stream more. gzip is decompressed the same way, since
    a small .gz member can expand without bound.
    """
    info = zf.getinfo(name)
    if info.file_size > MAX_PREVIEW_BYTES:
        raise TooLarge(
            f"{name} declares {info.file_size} bytes, over the "
            f"{MAX_PREVIEW_BYTES} byte preview limit"
        )

    with zf.open(name) as fh:
        raw = fh.read(MAX_PREVIEW_BYTES + 1)
    if len(raw) > MAX_PREVIEW_BYTES:
        raise TooLarge(f"{name} is larger than its header claimed")

    if name.endswith(".gz"):
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
            raw = gz.read(MAX_PREVIEW_BYTES + 1)
        if len(raw) > MAX_PREVIEW_BYTES:
            raise TooLarge(f"{name} expands past the preview limit")
    return raw.decode("utf-8", errors="replace")


def find_structures(archive: Path) -> list[str]:
    """Names of structure files inside the archive, largest first."""
    try:
        with zipfile.ZipFile(archive) as zf:
            members = [
                info
                for info in zf.infolist()
                if not info.is_dir()
                and info.filename.lower().endswith(STRUCTURE_SUFFIXES)
            ]
    except (zipfile.BadZipFile, OSError):
        return []
    members.sort(key=lambda i: i.file_size, reverse=True)
    return [i.filename for i in members]


def ca_trace(text: str) -> dict[str, list[tuple[float, float, float]]]:
    """Alpha-carbon coordinates per chain, from PDB or mmCIF text."""
    chains: dict[str, list[tuple[float, float, float]]] = {}

    if "_atom_site." in text:
        return _ca_trace_cif(text, chains)

    for line in text.splitlines():
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        if line[12:16].strip() != "CA":
            continue
        try:
            xyz = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
        except ValueError:
            continue
        chains.setdefault(line[21].strip() or "A", []).append(xyz)
    return chains


def _ca_trace_cif(text: str, chains: dict) -> dict:
    """Minimal mmCIF reader: only the _atom_site loop, only CA rows."""
    headers: list[str] = []
    in_loop = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("_atom_site."):
            headers.append(stripped.split(".", 1)[1])
            in_loop = True
            continue
        if not in_loop:
            continue
        if stripped.startswith(("#", "loop_", "_")) or not stripped:
            if headers and stripped.startswith(("#", "loop_")):
                break
            continue
        fields = stripped.split()
        if len(fields) < len(headers):
            continue
        row = dict(zip(headers, fields))
        if row.get("label_atom_id") != "CA":
            continue
        try:
            xyz = (
                float(row["Cartn_x"]), float(row["Cartn_y"]), float(row["Cartn_z"])
            )
        except (KeyError, ValueError):
            continue
        chain = row.get("auth_asym_id") or row.get("label_asym_id") or "A"
        chains.setdefault(chain, []).append(xyz)
    return chains


def _atom_records_cif(text: str) -> list[dict]:
    """Every atom row of an mmCIF _atom_site loop."""
    headers: list[str] = []
    rows: list[dict] = []
    in_loop = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("_atom_site."):
            headers.append(stripped.split(".", 1)[1])
            in_loop = True
            continue
        if not in_loop:
            continue
        if not stripped or stripped.startswith(("#", "loop_", "_", "data_")):
            if headers:
                break
            continue
        fields = stripped.split()
        if len(fields) >= len(headers):
            rows.append(dict(zip(headers, fields)))
    return rows


def cif_to_pdb(text: str) -> str:
    """Convert mmCIF atom records to PDB.

    NGL's mmCIF parser rejects the minimal CIF these models emit, so the
    conversion happens here rather than in the browser. Only coordinates are
    carried over, which is all a preview needs.
    """
    out: list[str] = []
    for serial, row in enumerate(_atom_records_cif(text), start=1):
        if serial > 99999:
            break
        try:
            x = float(row["Cartn_x"]); y = float(row["Cartn_y"]); z = float(row["Cartn_z"])
        except (KeyError, ValueError):
            continue
        name = (row.get("label_atom_id") or "X").strip('"')
        resn = (row.get("label_comp_id") or "UNK")[:3]
        chain = (row.get("auth_asym_id") or row.get("label_asym_id") or "A")[:1]
        try:
            resi = int(float(row.get("auth_seq_id") or row.get("label_seq_id") or serial))
        except ValueError:
            resi = serial
        element = (row.get("type_symbol") or name[:1])[:2]
        try:
            bfac = float(row.get("B_iso_or_equiv") or 0.0)
        except ValueError:
            bfac = 0.0
        # Atom names shorter than four characters start one column in.
        atom = f"{name:<4}" if len(name) >= 4 else f" {name:<3}"
        out.append(
            f"ATOM  {serial:5d} {atom}{'':1}{resn:>3} {chain}{resi:4d}{'':4}"
            f"{x:8.3f}{y:8.3f}{z:8.3f}{1.0:6.2f}{bfac:6.2f}{'':10}{element:>2}"
        )
    out.append("END")
    return "\n".join(out) + "\n"


def extract_preview(archive: Path) -> tuple[str, str] | None:
    """The largest structure in the archive, as PDB text ready to render."""
    names = find_structures(archive)
    if not names:
        return None
    try:
        with zipfile.ZipFile(archive) as zf:
            name = names[0]
            text = _read_member(zf, name)
    except (zipfile.BadZipFile, OSError, gzip.BadGzipFile, TooLarge) as exc:
        log.warning("cannot preview %s: %s", archive.name, exc)
        return None
    if ".cif" in name.lower() or "_atom_site." in text:
        text = cif_to_pdb(text)
    return name, text


def check_text(text: str) -> dict:
    """Assess one structure's backbone connectivity."""
    chains = ca_trace(text)
    distances: list[float] = []
    for coords in chains.values():
        distances += [
            math.dist(coords[i], coords[i + 1]) for i in range(len(coords) - 1)
        ]

    residues = sum(len(c) for c in chains.values())
    if not distances:
        return {
            "verdict": Verdict.UNKNOWN,
            "residues": residues,
            "chains": len(chains),
        }

    plausible = sum(1 for d in distances if CA_MIN <= d <= CA_MAX)
    fraction = plausible / len(distances)
    return {
        "verdict": Verdict.OK if fraction >= PLAUSIBLE_FRACTION else Verdict.IMPLAUSIBLE,
        "residues": residues,
        "chains": len(chains),
        "bonds": len(distances),
        "plausible_bonds": plausible,
        "fraction": round(fraction, 3),
        "mean_ca_ca": round(sum(distances) / len(distances), 2),
    }


def check_archive(archive: Path) -> dict:
    """Check the largest structure in a result archive."""
    names = find_structures(archive)
    if not names:
        return {"verdict": Verdict.UNKNOWN, "reason": "no structure file in archive"}
    try:
        with zipfile.ZipFile(archive) as zf:
            report = check_text(_read_member(zf, names[0]))
    except (zipfile.BadZipFile, OSError, gzip.BadGzipFile, TooLarge) as exc:
        return {"verdict": Verdict.UNKNOWN, "reason": str(exc)[:200]}
    report["file"] = names[0]
    report["structure_files"] = len(names)
    return report
