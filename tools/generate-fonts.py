#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
tools/generate-fonts.py (MONOCHROME)

Builds a simple black TTF/WOFF/WOFF2 from data/chars.php.

No COLR/CPAL tables. Each glyph is built as a set of rectangle contours.
This isolates geometry/metrics issues from color-font issues.

Deps:
  pip install fonttools brotli
  PHP must be on PATH

Usage:
  python tools/generate-fonts.py
  python tools/generate-fonts.py --debug
"""

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont


ROWS = 9  # bitmap height in chars.php


@dataclass(frozen=True)
class Metrics:
    upm: int = 1000
    ascent: int = 900
    descent: int = -100

    cell: int = 85
    left_pad: int = 1
    right_pad: int = 1
    bottom_pad: int = 1
    top_pad: int = 1

    letterspacing: int = 0


def glyph_name_for_codepoint(cp: int) -> str:
    if cp == 32:
        return "space"
    if cp <= 0xFFFF:
        return f"uni{cp:04X}"
    return f"u{cp:06X}"


def rect_to_pen(pen: TTGlyphPen, x0: int, y0: int, x1: int, y1: int) -> None:
    pen.moveTo((x0, y0))
    pen.lineTo((x1, y0))
    pen.lineTo((x1, y1))
    pen.lineTo((x0, y1))
    pen.closePath()


def find_project_chars(default_rel: str = "data/chars.php") -> Path:
    here = Path(__file__).resolve()
    for parent in [here.parent] + list(here.parents):
        cand = parent / default_rel
        if cand.exists():
            return cand
    return Path(default_rel)


def run_php_chars_to_json(chars_php: Path, debug: bool = False) -> Dict[int, List[int]]:
    """
    Loads $c from chars.php and returns {codepoint: [0/1, ...]}.
    Strips PHP warnings printed before JSON.
    """
    chars_php = Path(chars_php).resolve()
    if not chars_php.exists():
        raise FileNotFoundError(f"chars.php not found: {chars_php}")

    php = "php.exe" if os.name == "nt" else "php"
    php_code = (
        "ini_set('display_errors','0');"
        "ini_set('html_errors','0');"
        "error_reporting(0);"
        f"require {json.dumps(str(chars_php))};"
        "echo json_encode($c, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);"
    )
    cmd = [
        php,
        "-d", "display_errors=0",
        "-d", "html_errors=0",
        "-d", "error_reporting=0",
        "-r", php_code,
    ]

    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    stdout = res.stdout or ""
    stderr = res.stderr or ""

    def extract_json_blob(s: str) -> str:
        start = s.find("{")
        end = s.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise RuntimeError(
                "PHP output did not contain a JSON object.\n"
                f"Exit code: {res.returncode}\n"
                f"STDOUT (first 800 chars):\n{stdout[:800]}\n\n"
                f"STDERR (first 800 chars):\n{stderr[:800]}"
            )
        return s[start : end + 1]

    try:
        raw: Any = json.loads(stdout)
    except Exception:
        blob = extract_json_blob(stdout if stdout.strip() else (stdout + "\n" + stderr))
        raw = json.loads(blob)

    if not isinstance(raw, dict):
        raise TypeError(f"Expected JSON object from PHP, got: {type(raw)}")

    cmap: Dict[int, List[int]] = {}
    skipped: List[str] = []

    for k, v in raw.items():
        ks = str(k)
        if not ks.isdigit():
            if debug:
                skipped.append(ks)
            continue
        cp = int(ks)
        if v is None:
            pixels: List[int] = []
        elif isinstance(v, list):
            pixels = [int(x) for x in v]
        else:
            raise TypeError(f"Unexpected value type for key {ks} (cp={cp}): {type(v)}")
        cmap[cp] = pixels

    if debug:
        print(f"[run_php_chars_to_json] php exit={res.returncode}")
        print(f"[run_php_chars_to_json] stdout_len={len(stdout)} stderr_len={len(stderr)}")
        print(f"[run_php_chars_to_json] numeric_keys={len(cmap)} skipped_non_numeric={len(skipped)}")
        if skipped:
            print(f"[run_php_chars_to_json] skipped keys (first 20): {skipped[:20]}")

    return cmap


def build_font_mono(chars: Dict[int, List[int]], out_base: Path, family: str, style: str, vendor: str, metrics: Metrics, debug: bool) -> None:
    out_base = Path(out_base)
    out_base.parent.mkdir(parents=True, exist_ok=True)

    # Filter: empty or divisible by ROWS
    filtered: Dict[int, List[int]] = {}
    for cp, pixels in chars.items():
        pixels = pixels or []
        if len(pixels) == 0:
            filtered[cp] = []
        elif len(pixels) % ROWS == 0:
            filtered[cp] = pixels
        else:
            if debug:
                print(f"[warn] skipping {hex(cp)}: len={len(pixels)} not divisible by ROWS={ROWS}")

    glyph_order: List[str] = [".notdef", "space"]
    glyphs: Dict[str, Any] = {}
    advance_widths: Dict[str, int] = {}
    left_sidebearings: Dict[str, int] = {}

    cell = metrics.cell

    def aw_for_cols(cols: int) -> int:
        return int((metrics.left_pad + cols + metrics.right_pad + metrics.letterspacing) * cell)

    # .notdef
    pen_notdef = TTGlyphPen(None)
    rect_to_pen(pen_notdef, 80, 80, metrics.upm - 80, metrics.upm - 80)
    glyphs[".notdef"] = pen_notdef.glyph()
    advance_widths[".notdef"] = metrics.upm
    left_sidebearings[".notdef"] = 0

    # space
    pen_space = TTGlyphPen(None)
    glyphs["space"] = pen_space.glyph()
    advance_widths["space"] = aw_for_cols(1)
    left_sidebearings["space"] = 0

    # Build glyph outlines
    for cp in sorted(filtered.keys()):
        if cp == 32:
            continue

        pixels = filtered[cp]
        gname = glyph_name_for_codepoint(cp)

        if gname not in glyph_order:
            glyph_order.append(gname)

        cols = 1 if not pixels else (len(pixels) // ROWS)

        if debug and cp in (0x30, 0x40, 0x41, 0x45, 0x4D, 0x61):
            print(f"[debug] {hex(cp)} len(pixels)={len(pixels)} cols={cols}")

        aw = aw_for_cols(cols)
        advance_widths[gname] = aw
        left_sidebearings[gname] = 0

        pen = TTGlyphPen(None)

        if pixels:
            for idx, bit in enumerate(pixels):
                if int(bit) != 1:
                    continue

                row = idx // cols
                col = idx % cols

                x0 = int((metrics.left_pad + col) * cell)
                y0 = int((metrics.bottom_pad + (ROWS - 1 - row)) * cell)
                x1 = x0 + cell
                y1 = y0 + cell

                rect_to_pen(pen, x0, y0, x1, y1)

        glyphs[gname] = pen.glyph()

    # cmap
    cmap: Dict[int, str] = {}
    for cp in sorted(filtered.keys()):
        if cp == 32:
            cmap[cp] = "space"
        else:
            cmap[cp] = glyph_name_for_codepoint(cp)

    fb = FontBuilder(metrics.upm, isTTF=True)
    fb.setupGlyphOrder(glyph_order)

    # hmtx for every glyph
    hmtx = {}
    for gn in glyph_order:
        hmtx[gn] = (int(advance_widths.get(gn, aw_for_cols(3))), int(left_sidebearings.get(gn, 0)))
    fb.setupHorizontalMetrics(hmtx)

    fb.setupCharacterMap(cmap)
    fb.setupGlyf(glyphs)

    fb.setupHead()
    fb.setupMaxp()
    fb.setupHorizontalHeader(ascent=metrics.ascent, descent=metrics.descent)
    fb.setupOS2(
        sTypoAscender=metrics.ascent,
        sTypoDescender=metrics.descent,
        sTypoLineGap=0,
        usWinAscent=metrics.ascent,
        usWinDescent=abs(metrics.descent),
    )
    fb.setupNameTable(
        {
            "familyName": family,
            "styleName": style,
            "uniqueFontIdentifier": f"{family} {style}",
            "fullName": f"{family} {style}",
            "psName": f"{family.replace(' ', '')}-{style.replace(' ', '')}",
            "version": "Version 1.000",
            "manufacturer": vendor,
            "designer": vendor,
        }
    )
    fb.setupPost(keepGlyphNames=True)

    # Recalc bounds per glyph; and set flag for head bbox recalculation on save (older fontTools)
    glyf = fb.font["glyf"]
    for gn in fb.font.getGlyphOrder():
        try:
            glyf[gn].recalcBounds(glyf)
        except Exception:
            pass
    try:
        fb.font.recalcBBoxes = True
    except Exception:
        pass

    ttf_path = out_base.with_suffix(".ttf")
    fb.save(ttf_path)

    if debug:
        t = TTFont(ttf_path)
        for cp in (0x41, 0x40, 0x4D):
            gn = glyph_name_for_codepoint(cp)
            g = t["glyf"][gn]
            g.recalcBounds(t["glyf"])
            print(f"[bbox] {hex(cp)} {gn}: xMin={g.xMin} xMax={g.xMax} yMin={g.yMin} yMax={g.yMax}")

    # Save WOFF/WOFF2 fresh
    woff_path = out_base.with_suffix(".woff")
    f1 = TTFont(ttf_path)
    f1.flavor = "woff"
    f1.save(woff_path)

    woff2_path = out_base.with_suffix(".woff2")
    f2 = TTFont(ttf_path)
    f2.flavor = "woff2"
    f2.save(woff2_path)

    print("Wrote:")
    print(f"  {ttf_path}")
    print(f"  {woff_path}")
    print(f"  {woff2_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate monochrome defont (TTF/WOFF/WOFF2) from data/chars.php")
    ap.add_argument("--chars", type=str, default=str(find_project_chars()), help="Path to data/chars.php")
    ap.add_argument("--out", type=str, default="dist/fonts/defont", help="Output base path (no extension)")
    ap.add_argument("--family", type=str, default="defont", help="Font family name")
    ap.add_argument("--style", type=str, default="Regular", help="Font style name")
    ap.add_argument("--vendor", type=str, default="deidee", help="Vendor/manufacturer string")
    ap.add_argument("--debug", action="store_true", help="Print debug info")
    args = ap.parse_args()

    chars = run_php_chars_to_json(Path(args.chars), debug=args.debug)
    build_font_mono(
        chars=chars,
        out_base=Path(args.out),
        family=args.family,
        style=args.style,
        vendor=args.vendor,
        metrics=Metrics(),
        debug=args.debug,
    )


if __name__ == "__main__":
    main()
