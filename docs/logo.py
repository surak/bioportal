#!/usr/bin/env python
"""Generate the BioPortal logo.

The mark keeps Blablador's speech bubble and face — the same rounded bubble,
the same two eyes and T-shaped snout, taken from blablador-ng-notext.svg — and
has it speaking an alpha helix instead of words.

The helix is a real 3-D helix projected to 2-D rather than a hand-drawn
squiggle, split into front and back segments so the coil passes over and under
itself correctly.

    python docs/logo.py        # writes docs/img/bioportal-{mark,logo}.svg
"""

from __future__ import annotations

import math
from pathlib import Path

OUT = Path(__file__).resolve().parent / "img"

# Blablador's mark, viewBox 0 0 627 648.
BUBBLE = """\
    <path d="M496.92,521.49H131.03c-61.28,0-111.13-49.85-111.13-111.13V131.03c0-61.28,\
49.85-111.13,111.13-111.13h364.93c61.28,0,111.13,49.85,111.13,111.13v474.13l-110.17-83.66Z" \
fill="var(--bubble-fill)"/>
    <path d="M495.96,0H131.03C58.66,0,0,58.66,0,131.03v279.33c0,72.36,58.66,131.03,131.03,\
131.03h359.19l136.76,103.85V131.03C626.98,58.66,568.32,0,495.96,0ZM587.2,409.52v155.55l-72.92\
-55.37-6.29-4.78V177.78h-39.79v323.82H131.03c-50.31,0-91.24-40.93-91.24-91.24V131.03c0-50.31,\
40.93-91.24,91.24-91.24h364.93c50.31,0,91.24,40.93,91.24,91.24v278.5Z" fill="var(--ink)"/>
    <circle cx="151.59" cy="178.57" r="50" fill="var(--ink)"/>
    <circle cx="363.49" cy="178.57" r="50" fill="var(--ink)"/>
    <polygon points="191.23 289.2 191.23 328.99 235.04 328.99 235.04 386 269.68 386 269.68 \
328.99 313.49 328.99 313.49 289.2 191.23 289.2" fill="var(--ink)"/>"""


def helix_segments(
    x0: float, y0: float, length: float, amplitude: float,
    turns: float, samples: int = 420,
) -> list[tuple[list[tuple[float, float]], float]]:
    """A helix along +x, as runs of points tagged with mean depth.

    z is the depth axis: z > 0 is the near side of the coil. Consecutive
    points are grouped into runs that stay on one side, so the near runs can
    be drawn after (over) the far ones.
    """
    pts: list[tuple[float, float, float]] = []
    for i in range(samples + 1):
        t = i / samples
        angle = t * turns * 2 * math.pi
        x = x0 + t * length
        # Slight tilt so the coil reads as receding rather than flat.
        y = y0 + amplitude * math.sin(angle) - t * amplitude * 0.18
        z = math.cos(angle)
        pts.append((x, y, z))

    runs: list[tuple[list[tuple[float, float]], float]] = []
    current: list[tuple[float, float]] = []
    sign = pts[0][2] >= 0
    depths: list[float] = []
    for x, y, z in pts:
        this_sign = z >= 0
        if this_sign != sign and current:
            runs.append((current, sum(depths) / len(depths)))
            # Overlap by one point so the runs join without a visible gap.
            current = [current[-1]]
            depths = []
            sign = this_sign
        current.append((x, y))
        depths.append(z)
    if current:
        runs.append((current, sum(depths) / len(depths)))
    return runs


def path_d(points: list[tuple[float, float]]) -> str:
    head = f"M{points[0][0]:.1f},{points[0][1]:.1f}"
    rest = "".join(f"L{x:.1f},{y:.1f}" for x, y in points[1:])
    return head + rest


def helix_svg(x0: float, y0: float, length: float, amplitude: float, turns: float) -> str:
    runs = helix_segments(x0, y0, length, amplitude, turns)
    back = [r for r in runs if r[1] < 0]
    front = [r for r in runs if r[1] >= 0]
    out = []
    # Far side first, lighter and thinner; near side over it.
    for points, _ in back:
        out.append(
            f'    <path d="{path_d(points)}" fill="none" stroke="var(--helix-back)" '
            'stroke-width="34" stroke-linecap="round" stroke-linejoin="round"/>'
        )
    for points, _ in front:
        out.append(
            f'    <path d="{path_d(points)}" fill="none" stroke="var(--helix)" '
            'stroke-width="46" stroke-linecap="round" stroke-linejoin="round"/>'
        )
    return "\n".join(out)


STYLE = """\
  <style>
    svg { --ink: #15140f; --bubble-fill: #ffffff;
          --helix: #0b6e4f; --helix-back: #7fc9ad; }
    @media (prefers-color-scheme: dark) {
      svg { --ink: #f2efe9; --bubble-fill: #17181a;
            --helix: #4fbf92; --helix-back: #1f6a52; }
    }
  </style>"""


def mark(width: int = 1140, height: int = 700) -> str:
    """Bubble plus helix, no wordmark."""
    # Ends short of the right edge: the stroke is 46 wide, so allow its radius.
    helix = helix_svg(x0=580, y0=290, length=480, amplitude=145, turns=3.0)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" \
role="img" aria-label="BioPortal">
  <title>BioPortal</title>
{STYLE}
  <g transform="translate(0,20)">
{helix}
  </g>
  <g transform="translate(0,20)">
{BUBBLE}
  </g>
</svg>
"""


def lockup(width: int = 1810, height: int = 500) -> str:
    """Horizontal logo with the wordmark, for 400x200 use."""
    helix = helix_svg(x0=580, y0=290, length=470, amplitude=145, turns=3.0)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" \
role="img" aria-label="BioPortal">
  <title>BioPortal</title>
{STYLE}
  <g transform="translate(40,30) scale(0.60)">
{helix}
  </g>
  <g transform="translate(40,30) scale(0.60)">
{BUBBLE}
  </g>
  <text x="780" y="270" font-family="Helvetica Neue, Helvetica, Arial, sans-serif" \
font-size="170" font-weight="700" fill="var(--ink)">BioPortal</text>
  <text x="786" y="360" font-family="Helvetica Neue, Helvetica, Arial, sans-serif" \
font-size="60" fill="var(--helix)">molecular models on Blablador</text>
</svg>
"""


def square(size: int = 720) -> str:
    """Square mark for favicons: the bubble, with the helix running out of
    its tail toward the lower right."""
    helix = helix_svg(x0=0, y0=0, length=330, amplitude=105, turns=2.25)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" \
role="img" aria-label="BioPortal">
  <title>BioPortal</title>
{STYLE}
  <g transform="translate(355,395) rotate(34)">
{helix}
  </g>
  <g transform="translate(30,20) scale(0.74)">
{BUBBLE}
  </g>
</svg>
"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "bioportal-mark.svg").write_text(mark())
    (OUT / "bioportal-logo.svg").write_text(lockup())
    (OUT / "bioportal-square.svg").write_text(square())
    print(f"wrote {OUT/'bioportal-mark.svg'}")
    print(f"wrote {OUT/'bioportal-logo.svg'}")
    print(f"wrote {OUT/'bioportal-square.svg'}")


if __name__ == "__main__":
    main()
