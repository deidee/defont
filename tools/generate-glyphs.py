#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/generate-glyphs.py

Generate SVGs for every glyph defined in data/chars.php.

Outputs:
  src/svg/character-u{hex}.svg     (e.g. character-u0041.svg)
  src/svg/character-notdef.svg    (if '.notdef' exists)

Assumptions (matches your glyph designer + chars.php):
- Fixed ROWS = 9
- Variable width (cols = len(pixels) / ROWS)
- Pixels are 0/1 in row-major order
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


ROWS = 9


@dataclass(frozen=True)
class Glyph:
    key: str  # either "U+...." or ".notdef" etc
    codepoint: Optional[int]  # None for non-unicode keys
    pixels: List[int]
    cols: int


def project_root() -> Path:
    # tools/generate-glyphs.py -> project root is parent of tools/
    return Path(__file__).resolve().parents[1]


def strip_php_comments(src: str) -> str:
    # Remove /* ... */ first
    s = re.sub(r"/\*[\s\S]*?\*/", "", src)
    # Remove //... (line comments)
    s = re.sub(r"^\s*//.*$", "", s, flags=re.MULTILINE)
    s = re.sub(r"//.*$", "", s, flags=re.MULTILINE)
    return s


def parse_key(key_raw: str) -> Tuple[Optional[int], Optional[str]]:
    """
    Returns (codepoint, string_key)
    - If numeric: codepoint int, string_key None
    - If quoted string: codepoint None, string_key str
    - Otherwise: (None, None)
    """
    kr = key_raw.strip()

    # quoted string key
    m = re.match(r"""^(['"])(.*?)\1$""", kr)
    if m:
        return None, m.group(2)

    # numeric key
    if re.match(r"^0x[0-9a-fA-F]+$", kr):
        return int(kr, 16), None
    if re.match(r"^\d+$", kr):
        return int(kr, 10), None

    return None, None


def parse_pixels(body: str) -> List[int]:
    # Only 0/1 digits are meaningful for this format
    nums = re.findall(r"[01]", body)
    return [1 if ch == "1" else 0 for ch in nums]


def parse_glyphs_php(path: Path) -> Dict[str, Glyph]:
    """
    Parses glyphs from a PHP file that looks like:
      $c[0x41] = array( ... );
      $data->c[0x41] = array( ... );
      $c['.notdef'] = array( ... );

    Returns dict keyed by a stable glyph key:
      - "U+0041" for unicode glyphs
      - ".notdef" (or other string keys) for named glyphs
    """
    src = path.read_text(encoding="utf-8")
    s = strip_php_comments(src)

    # Accept both $c[...] and $data->c[...]
    lhs = r"(?:\$c|\$data\s*->\s*c)"

    # Match: $c[KEY] = array( BODY );
    # Non-greedy BODY with DOTALL.
    re_assign = re.compile(
        rf"{lhs}\s*\[\s*([^\]]+?)\s*\]\s*=\s*array\s*\(\s*([\s\S]*?)\s*\)\s*;",
        flags=re.MULTILINE,
    )

    found: Dict[str, Glyph] = {}

    for m in re_assign.finditer(s):
        key_raw = m.group(1)
        body = m.group(2)

        cp, sk = parse_key(key_raw)
        px = parse_pixels(body)

        if cp is None and sk is None:
            continue

        if cp is not None:
            # Empty array is allowed (space)
            if len(px) == 0:
                cols = 1
            else:
                if len(px) % ROWS != 0:
                    # skip invalid glyphs (but warn)
                    print(
                        f"[warn] Skipping U+{cp:04X}: pixel length {len(px)} not divisible by {ROWS}",
                        file=sys.stderr,
                    )
                    continue
                cols = max(1, len(px) // ROWS)

            key = f"U+{cp:04X}"
            found[key] = Glyph(key=key, codepoint=cp, pixels=px, cols=cols)
        else:
            # named glyph
            if len(px) == 0:
                cols = 1
            else:
                if len(px) % ROWS != 0:
                    print(
                        f"[warn] Skipping '{sk}': pixel length {len(px)} not divisible by {ROWS}",
                        file=sys.stderr,
                    )
                    continue
                cols = max(1, len(px) // ROWS)

            assert sk is not None
            found[sk] = Glyph(key=sk, codepoint=None, pixels=px, cols=cols)

    return found


def codepoint_filename(cp: int, uppercase: bool = False) -> str:
    # Use 4 hex digits for BMP, 6 for >FFFF (matches your designer behavior)
    width = 4 if cp <= 0xFFFF else 6
    hx = f"{cp:0{width}x}"
    if uppercase:
        hx = hx.upper()
    return f"character-u{hx}.svg"


def slugify_name(name: str) -> str:
    # For non-codepoint keys like ".notdef"
    s = name.strip()
    if s.startswith("."):
        s = s[1:]
    s = s.replace(" ", "-")
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if not s:
        s = "glyph"
    return s


def build_svg(
    glyph: Glyph,
    cell_px: int,
    fill: str,
    include_bg: bool,
    bg: str,
) -> str:
    cols = glyph.cols
    rows = ROWS

    # ViewBox is in "cell units" to keep it clean; width/height are for preview.
    width_px = cols * cell_px
    height_px = rows * cell_px

    rects: List[str] = []
    if glyph.pixels:
        # row-major
        for r in range(rows):
            for c in range(cols):
                i = r * cols + c
                if i >= len(glyph.pixels):
                    break
                if glyph.pixels[i] != 1:
                    continue
                rects.append(f'<rect x="{c}" y="{r}" width="1" height="1"/>')

    title = glyph.key if glyph.codepoint is None else f"{glyph.key} ({chr(glyph.codepoint)})"
    bg_rect = f'<rect x="0" y="0" width="{cols}" height="{rows}" fill="{bg}"/>' if include_bg else ""
    fg_group = f'<g fill="{fill}">\n    ' + "\n    ".join(rects) + "\n  </g>" if rects else f'<g fill="{fill}"></g>'

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width_px}" height="{height_px}" '
        f'viewBox="0 0 {cols} {rows}" '
        f'shape-rendering="crispEdges">\n'
        f'  <title>{escape_xml(title)}</title>\n'
        f'  {bg_rect}\n'
        f'  {fg_group}\n'
        f"</svg>\n"
    )


def escape_xml(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def main() -> int:
    root = project_root()

    ap = argparse.ArgumentParser(description="Generate SVGs from data/chars.php")
    ap.add_argument(
        "--in",
        dest="in_path",
        default=str(root / "data" / "chars.php"),
        help="Input PHP file (default: data/chars.php)",
    )
    ap.add_argument(
        "--out",
        dest="out_dir",
        default=str(root / "src" / "svg"),
        help="Output directory (default: src/svg)",
    )
    ap.add_argument("--cell", dest="cell_px", type=int, default=24, help="Preview cell size in px (default: 24)")
    ap.add_argument("--fill", dest="fill", default="black", help="Fill color for 'on' cells (default: black)")
    ap.add_argument("--bg", dest="bg", default="white", help="Background color when --with-bg is set (default: white)")
    ap.add_argument("--with-bg", dest="with_bg", action="store_true", help="Include a background rect")
    ap.add_argument("--uppercase", dest="uppercase", action="store_true", help="Uppercase hex in filenames")
    ap.add_argument("--only", dest="only", default="", help="Optional filter: comma list of hex cps (e.g. 0041,05D0)")
    args = ap.parse_args()

    in_path = Path(args.in_path).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not in_path.exists():
        print(f"[error] Input file not found: {in_path}", file=sys.stderr)
        return 2

    glyphs = parse_glyphs_php(in_path)

    only_set: Optional[set[int]] = None
    if args.only.strip():
        only_set = set()
        for part in args.only.split(","):
            p = part.strip()
            if not p:
                continue
            try:
                only_set.add(int(p, 16))
            except ValueError:
                print(f"[warn] Ignoring invalid --only entry: {p}", file=sys.stderr)

    written = 0
    skipped = 0

    # Write unicode glyphs
    for g in sorted((g for g in glyphs.values() if g.codepoint is not None), key=lambda x: x.codepoint or 0):
        assert g.codepoint is not None
        if only_set is not None and g.codepoint not in only_set:
            skipped += 1
            continue

        filename = codepoint_filename(g.codepoint, uppercase=args.uppercase)
        svg = build_svg(g, cell_px=args.cell_px, fill=args.fill, include_bg=args.with_bg, bg=args.bg)
        (out_dir / filename).write_text(svg, encoding="utf-8")
        written += 1

    # Write .notdef (and other named glyphs) if present
    for g in (g for g in glyphs.values() if g.codepoint is None):
        # keep just .notdef by default; if you want more named glyphs later, remove this guard
        if g.key != ".notdef":
            continue
        name = "character-notdef.svg"
        svg = build_svg(g, cell_px=args.cell_px, fill=args.fill, include_bg=args.with_bg, bg=args.bg)
        (out_dir / name).write_text(svg, encoding="utf-8")
        written += 1

    print(f"[ok] Wrote {written} SVGs to {out_dir}")
    if skipped:
        print(f"[note] Skipped {skipped} (filtered by --only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())