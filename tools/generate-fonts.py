#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
tools/generate-defont.py

Generates:
  - dist/fonts/defont.ttf
  - dist/fonts/defont.woff
  - dist/fonts/defont.woff2

Design:
  - 9-row bitmap glyphs from data/chars.php (same mapping your PHP uses)
  - each '1' pixel becomes a rectangle
  - COLR/CPAL color font layers using a "deJade" palette (RGBA with ~0.5 alpha)
  - monochrome fallback outlines are included in the base glyph shapes

Requires:
  - PHP available in PATH (to json_encode($c) from chars.php)
  - Python packages: fonttools, brotli
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Any

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

# Color font builders
from fontTools.colorLib.builder import buildCOLR, buildCPAL  # type: ignore
from fontTools.colorLib.errors import ColorLibError

ROWS = 9  # matches your PHP: const ROWS = 9


@dataclass(frozen=True)
class Metrics:
    upm: int = 1000
    ascent: int = 900
    descent: int = -100  # hhea descender is negative
    # Layout in font units: we emulate "HEIGHT_MULTIPLIER = 11" as 11 "cells"
    cell: int = 90  # 11 * 90 = 990 ~= fits within 1000; leaves a tiny safety margin
    left_pad_cells: int = 1
    right_pad_cells: int = 1
    letterspacing_cells: int = 0  # extra spacing beyond right pad (optional)

def run_php_chars_to_json(chars_php: Path, debug: bool = False) -> Dict[int, List[int]]:
    """
    Loads $c from the PHP file and returns {codepoint: [0/1, ...]}.

    Robust against:
      - mixed-key PHP arrays ($c['.notdef'] + $c[0x41])
      - PHP warnings/notices printed before JSON (e.g. Imagick/ImageMagick mismatch)

    Strategy:
      - run PHP with display_errors=0 / error_reporting=0 (best effort)
      - do NOT merge stderr into stdout
      - parse JSON from the first '{' ... last '}' slice of output if needed
      - ignore non-numeric keys like ".notdef"
    """
    chars_php = Path(chars_php)
    if not chars_php.exists():
        raise FileNotFoundError(f"chars.php not found: {chars_php}")

    php = "php.exe" if os.name == "nt" else "php"

    php_code = (
        # Best-effort suppression (may not stop extension init warnings on some setups)
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

    # Capture stdout and stderr separately so stderr warnings won't poison JSON
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise RuntimeError("PHP executable not found. Ensure `php` is in your PATH.") from e

    stdout = res.stdout or ""
    stderr = res.stderr or ""

    # Prefer stdout, but if JSON ended up on stderr somehow, consider that too.
    combined_for_fallback = stdout if stdout.strip() else (stderr if stderr.strip() else (stdout + stderr))

    def extract_json_blob(s: str) -> str:
        """
        Returns the substring from the first '{' to the last '}' (inclusive).
        This strips any PHP warnings/noise that may precede or follow the JSON.
        """
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

    # First try: parse stdout directly (ideal case)
    raw: Any
    try:
        raw = json.loads(stdout)
    except Exception:
        # Second try: strip warnings/junk and parse extracted JSON region
        json_blob = extract_json_blob(combined_for_fallback)
        try:
            raw = json.loads(json_blob)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                "Could not decode JSON from PHP even after stripping warnings.\n"
                f"Exit code: {res.returncode}\n"
                f"Extracted JSON (first 800 chars):\n{json_blob[:800]}\n\n"
                f"STDOUT (first 800 chars):\n{stdout[:800]}\n\n"
                f"STDERR (first 800 chars):\n{stderr[:800]}"
            ) from e

    if not isinstance(raw, dict):
        raise TypeError(f"Expected JSON object from PHP, got: {type(raw)}")

    cmap: Dict[int, List[int]] = {}
    skipped: List[str] = []

    for k, v in raw.items():
        ks = str(k)
        # Don't even call int() unless it's strictly numeric (skips ".notdef")
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




def glyph_name_for_codepoint(cp: int) -> str:
    if cp == 32:
        return "space"
    if cp <= 0xFFFF:
        return f"uni{cp:04X}"
    return f"u{cp:06X}"


def de_jade(rng: random.Random) -> Tuple[int, int, int, int]:
    # PHP:
    #   r = mt_rand(0,127)
    #   g = mt_rand(127,255)
    #   b = mt_rand(0,191)
    #   a = .5
    r = rng.randint(0, 127)
    g = rng.randint(127, 255)
    b = rng.randint(0, 191)
    a = 128  # 0.5 alpha
    return (r, g, b, a)


def rect_to_pen(pen: TTGlyphPen, x0: int, y0: int, x1: int, y1: int) -> None:
    # A simple rectangle contour. All points on-curve is fine for glyf.
    pen.moveTo((x0, y0))
    pen.lineTo((x1, y0))
    pen.lineTo((x1, y1))
    pen.lineTo((x0, y1))
    pen.closePath()

def palette_rgba255_to_unit(pal):
    """Convert [(R,G,B,A) 0..255] -> [(r,g,b,a) 0..1]."""
    out = []
    for c in pal:
        if len(c) == 3:
            r, g, b = c
            a = 255
        else:
            r, g, b, a = c
        out.append((r / 255.0, g / 255.0, b / 255.0, a / 255.0))
    return out

def build_font(
    chars: Dict[int, List[int]],
    out_base: Path,
    family_name: str,
    style_name: str,
    vendor_id: str,
    seed: int,
    jitter_px_max: int,
    metrics: Metrics,
) -> None:
    rng = random.Random(seed)

    out_base.parent.mkdir(parents=True, exist_ok=True)

    # Filter/validate glyph data
    # Keep only codepoints whose pixel arrays are either empty (space) or divisible by ROWS.
    filtered: Dict[int, List[int]] = {}
    for cp, pixels in chars.items():
        if pixels is None:
            pixels = []
        if len(pixels) == 0:
            filtered[cp] = []
            continue
        if len(pixels) % ROWS != 0:
            # Skip invalid entries rather than exploding; you can tighten this if you prefer.
            continue
        filtered[cp] = pixels

    # Compute total "ones" across all glyphs (for palette sizing like PHP does).
    total_ones = sum(sum(p) for p in filtered.values())
    # PHP uses for($i=0; $i <= $ones; $i++), i.e. ones+1 entries
    palette_size = total_ones + 1

    # Build palette
    palette: List[Tuple[int, int, int, int]] = [de_jade(rng) for _ in range(palette_size)]

    # October special (mimic your PHP behavior)
    now = datetime.now()
    if now.month == 10 and palette:
        palette[0] = (255, 68, 136, 128)
        rng.shuffle(palette)

    # We'll assign palette indices sequentially per "on" pixel, across the whole font build.
    next_palette_index = 0

    glyph_order: List[str] = [".notdef", "space"]
    glyphs = {}
    advance_widths: Dict[str, int] = {}
    left_side_bearings: Dict[str, int] = {}

    # COLR layer mapping: baseGlyphName -> list[(layerGlyphName, paletteIndex)]
    color_layers: Dict[str, List[Tuple[str, int]]] = {}

    # .notdef glyph (simple box)
    pen_notdef = TTGlyphPen(None)
    # A visible notdef rectangle
    rect_to_pen(pen_notdef, 80, 80, 920, 920)
    glyphs[".notdef"] = pen_notdef.glyph()
    advance_widths[".notdef"] = metrics.upm
    left_side_bearings[".notdef"] = 0

    # space glyph (empty)
    pen_space = TTGlyphPen(None)
    glyphs["space"] = pen_space.glyph()
    advance_widths["space"] = 2 * metrics.cell
    left_side_bearings["space"] = 0

    # Sort by codepoint for stable output
    for cp in sorted(filtered.keys()):
        if cp == 32:
            # already have "space" glyph; still map cmap to it later
            continue

        pixels = filtered[cp]
        gname = glyph_name_for_codepoint(cp)
        if gname not in glyph_order:
            glyph_order.append(gname)

        # Determine columns & advance width
        if len(pixels) == 0:
            cols = 0
        else:
            cols = len(pixels) // ROWS

        aw = (metrics.left_pad_cells + cols + metrics.right_pad_cells + metrics.letterspacing_cells) * metrics.cell
        if aw <= 0:
            aw = 2 * metrics.cell

        advance_widths[gname] = aw
        left_side_bearings[gname] = 0

        # Base glyph pen (monochrome fallback outlines)
        base_pen = TTGlyphPen(None)

        layers_for_glyph: List[Tuple[str, int]] = []

        if cols > 0:
            # Iterate pixels in row-major order: index -> (row, col)
            for i, bit in enumerate(pixels):
                if bit != 1:
                    continue

                row = i // cols  # 0..ROWS-1, top->bottom in your PHP
                col = i % cols

                # Map to font coords:
                # - x starts after a left pad cell
                x = (metrics.left_pad_cells + col) * metrics.cell

                # y_top mimics SVG top-left anchoring, then rect extends "down"
                # Top of row r:
                y_top = (ROWS - row) * metrics.cell  # r=0 -> 9*cell, r=8 -> 1*cell
                # Base rect size:
                w = metrics.cell
                h = metrics.cell

                # Jitter: PHP adds 0..3 pixels to width/height where size=24.
                # We scale that to font units.
                if jitter_px_max > 0:
                    j = rng.randint(0, jitter_px_max)
                    extra = int(round(metrics.cell * (j / 24.0)))
                    w += extra
                    h += extra

                x0 = int(x)
                x1 = int(x + w)
                y1 = int(y_top)
                y0 = int(y_top - h)

                # Add to base outline
                rect_to_pen(base_pen, x0, y0, x1, y1)

                # Create a layer glyph containing just this rectangle
                layer_name = f"{gname}.p{next_palette_index}"
                glyph_order.append(layer_name)

                layer_pen = TTGlyphPen(None)
                rect_to_pen(layer_pen, x0, y0, x1, y1)
                glyphs[layer_name] = layer_pen.glyph()

                advance_widths[layer_name] = aw
                left_side_bearings[layer_name] = 0

                # Assign a unique palette index per pixel (like sequential palette[$one])
                pal_index = next_palette_index
                layers_for_glyph.append((layer_name, pal_index))

                next_palette_index += 1
                # Safety: don't exceed palette; keep it predictable if something is off
                if next_palette_index >= palette_size:
                    next_palette_index = palette_size - 1

        glyphs[gname] = base_pen.glyph()
        if layers_for_glyph:
            color_layers[gname] = layers_for_glyph

    # Build cmap (character map) for base glyphs
    cmap: Dict[int, str] = {}
    for cp in sorted(filtered.keys()):
        cmap[cp] = "space" if cp == 32 else glyph_name_for_codepoint(cp)

    # FontBuilder
    fb = FontBuilder(metrics.upm, isTTF=True)
    fb.setupGlyphOrder(glyph_order)

    # Horizontal metrics
    hmtx = {}
    for gn in glyph_order:
        aw = advance_widths.get(gn, 2 * metrics.cell)
        lsb = left_side_bearings.get(gn, 0)
        hmtx[gn] = (int(aw), int(lsb))
    fb.setupHorizontalMetrics(hmtx)

    # Tables
    fb.setupCharacterMap(cmap)
    fb.setupGlyf(glyphs)
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
            "familyName": family_name,
            "styleName": style_name,
            "uniqueFontIdentifier": f"{family_name} {style_name}",
            "fullName": f"{family_name} {style_name}",
            "psName": f"{family_name.replace(' ', '')}-{style_name.replace(' ', '')}",
            "version": "Version 1.000",
            "manufacturer": vendor_id,
            "designer": vendor_id,
            "vendorURL": "https://deidee.nl/",
        }
    )
    fb.setupPost()

    # --- Build color tables (COLR/CPAL) ---

    # --- Build CPAL ---
    try:
        # Some fontTools variants support this style (font passed in)
        buildCPAL(fb.font, palettes=[palette])
    except TypeError:
        # Other variants return the CPAL table; assign it manually.
        try:
            fb.font["CPAL"] = buildCPAL([palette])
        except ColorLibError:
            # This fontTools expects colors in 0..1 floats, not 0..255 ints.
            fb.font["CPAL"] = buildCPAL([palette_rgba255_to_unit(palette)])

    # COLR: mapping baseGlyphName -> [(layerGlyphName, paletteIndex), ...]
    try:
        buildCOLR(fb.font, colorLayers=color_layers)
    except TypeError:
        # Older/alternate style: buildCOLR(colorLayers) -> COLR table
        fb.font["COLR"] = buildCOLR(color_layers)


    # Save TTF
    ttf_path = out_base.with_suffix(".ttf")
    fb.save(ttf_path)

    # Save WOFF + WOFF2
    from fontTools.ttLib import TTFont

    font = TTFont(ttf_path)

    # WOFF
    woff_path = out_base.with_suffix(".woff")
    font.flavor = "woff"
    font.save(woff_path)

    # WOFF2
    woff2_path = out_base.with_suffix(".woff2")
    font.flavor = "woff2"
    try:
        font.save(woff2_path)
    except Exception as e:
        raise RuntimeError(
            "WOFF2 save failed. You likely need `brotli` installed.\n"
            "Try: pip install brotli\n"
            f"Original error: {e}"
        ) from e

    # Reset flavor (good hygiene)
    font.flavor = None

    print("Wrote:")
    print(f"  {ttf_path}")
    print(f"  {woff_path}")
    print(f"  {woff2_path}")


def find_project_chars(default_rel: str = "data/chars.php") -> Path:
    """
    Tries to locate data/chars.php by searching upward from this script.
    """
    here = Path(__file__).resolve()
    for parent in [here.parent] + list(here.parents):
        candidate = parent / default_rel
        if candidate.exists():
            return candidate
    # fallback to cwd
    candidate = Path.cwd() / default_rel
    return candidate


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate defont (TTF/WOFF/WOFF2) from data/chars.php.")
    ap.add_argument("--chars", type=str, default=str(find_project_chars()), help="Path to data/chars.php")
    ap.add_argument("--out", type=str, default="dist/fonts/defont", help="Output base path without extension")
    ap.add_argument("--family", type=str, default="defont", help="Font family name")
    ap.add_argument("--style", type=str, default="Regular", help="Font style name")
    ap.add_argument("--vendor", type=str, default="deidee", help="Vendor/manufacturer string")
    ap.add_argument("--seed", type=int, default=0, help="Random seed (0 = derive from date)")
    ap.add_argument("--jitter", type=int, default=3, help="Max jitter in 'PHP pixels' (0..3 recommended)")
    args = ap.parse_args()

    chars_php = Path(args.chars).resolve()
    out_base = Path(args.out).resolve()

    # Deterministic-ish default seed (like "today's brand output")
    if args.seed == 0:
        # YYYYMMDD integer
        args.seed = int(datetime.now().strftime("%Y%m%d"))

    chars = run_php_chars_to_json(chars_php)

    metrics = Metrics()
    build_font(
        chars=chars,
        out_base=out_base,
        family_name=args.family,
        style_name=args.style,
        vendor_id=args.vendor,
        seed=args.seed,
        jitter_px_max=max(0, int(args.jitter)),
        metrics=metrics,
    )


if __name__ == "__main__":
    main()
