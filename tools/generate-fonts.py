#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
tools/generate-fonts.py

Generates:
  - dist/fonts/defont.ttf
  - dist/fonts/defont.woff
  - dist/fonts/defont.woff2

Key fix vs previous versions:
  - Build CPAL + COLR *manually* (COLR v0) instead of using colorLib.builder,
    because some fontTools builds produce broken COLR layer references in browsers
    (symptom: glyphs render as single vertical bars).

Deps:
  pip install fonttools brotli
  PHP must be available on PATH

Usage:
  python tools/generate-fonts.py
  python tools/generate-fonts.py --debug
"""

import argparse
import json
import os
import random
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable

# Manual COLR/CPAL table classes
from fontTools.ttLib.tables.C_O_L_R_ import LayerRecord  # type: ignore
from fontTools.ttLib.tables.C_P_A_L_ import Color  # type: ignore


ROWS = 9  # bitmap height in chars.php


@dataclass(frozen=True)
class Metrics:
    upm: int = 1000
    ascent: int = 900
    descent: int = -100

    cell: int = 85  # font units per bitmap cell

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


def de_jade(rng: random.Random) -> Tuple[int, int, int, int]:
    # Matches PHP deJade(): r=[0..127], g=[127..255], b=[0..191], a=0.5
    r = rng.randint(0, 127)
    g = rng.randint(127, 255)
    b = rng.randint(0, 191)
    a = 128
    return (r, g, b, a)


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
    Robust against PHP warnings printed before JSON.
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


def attach_cpal_manual(font: TTFont, palette_rgba: List[Tuple[int, int, int, int]]) -> None:
    """
    CPAL uses BGRA order internally (Color(blue, green, red, alpha)).
    """
    cpal = newTable("CPAL")
    cpal.version = 0
    cpal.numPaletteEntries = len(palette_rgba)

    # one palette
    cpal.palettes = [[Color(b, g, r, a) for (r, g, b, a) in palette_rgba]]

    # optional metadata arrays (safe defaults)
    try:
        default_type = getattr(cpal, "DEFAULT_PALETTE_TYPE", 0)
        cpal.paletteTypes = [default_type]
    except Exception:
        cpal.paletteTypes = [0]
    cpal.paletteLabels = []
    cpal.paletteEntryLabels = []

    font["CPAL"] = cpal


def attach_colr_manual(font: TTFont, color_layers: Dict[str, List[Tuple[str, int]]]) -> None:
    """
    Build COLR v0 manually: base glyph -> list of LayerRecord(name, colorID).
    """
    colr = newTable("COLR")
    colr.version = 0
    colr.ColorLayers = {}
    for base_glyph, layers in color_layers.items():
        colr.ColorLayers[base_glyph] = [LayerRecord(name=gn, colorID=int(pi)) for (gn, pi) in layers]
    font["COLR"] = colr


def build_font(
    chars: Dict[int, List[int]],
    out_base: Path,
    family_name: str,
    style_name: str,
    vendor_id: str,
    seed: int,
    jitter_px_max: int,
    metrics: Metrics,
    debug: bool = False,
) -> None:
    rng = random.Random(seed)
    out_base = Path(out_base)
    out_base.parent.mkdir(parents=True, exist_ok=True)

    # Filter glyphs: allow empty or divisible by ROWS
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

    # Palette size like PHP: ones + 1
    total_ones = sum(sum(p) for p in filtered.values())
    palette_size = total_ones + 1
    palette: List[Tuple[int, int, int, int]] = [de_jade(rng) for _ in range(palette_size)]

    # October special like PHP
    if datetime.now().month == 10 and palette:
        palette[0] = (255, 68, 136, 128)
        rng.shuffle(palette)

    glyph_order: List[str] = [".notdef", "space"]
    glyphs: Dict[str, Any] = {}
    advance_widths: Dict[str, int] = {}
    left_sidebearings: Dict[str, int] = {}
    color_layers: Dict[str, List[Tuple[str, int]]] = {}

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

    next_color_index = 0

    # Build glyphs + per-pixel layer glyphs
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

        base_pen = TTGlyphPen(None)
        layers_for_glyph: List[Tuple[str, int]] = []

        if pixels:
            for idx, bit in enumerate(pixels):
                if int(bit) != 1:
                    continue

                # Row-major mapping (same as your PHP logic)
                row = idx // cols  # 0..ROWS-1 top->bottom in bitmap
                col = idx % cols

                x0 = int((metrics.left_pad + col) * cell)
                y0 = int((metrics.bottom_pad + (ROWS - 1 - row)) * cell)

                w = cell
                h = cell
                if jitter_px_max > 0:
                    j = rng.randint(0, jitter_px_max)
                    extra = int(round(cell * (j / 24.0)))
                    w += extra
                    h += extra

                x1 = x0 + int(w)
                y1 = y0 + int(h)

                # base outline (fallback)
                rect_to_pen(base_pen, x0, y0, x1, y1)

                # layer glyph (one rect, one palette index)
                color_index = next_color_index
                next_color_index += 1
                if color_index >= palette_size:
                    color_index = palette_size - 1

                layer_name = f"{gname}.c{color_index}"
                glyph_order.append(layer_name)

                lp = TTGlyphPen(None)
                rect_to_pen(lp, x0, y0, x1, y1)
                glyphs[layer_name] = lp.glyph()
                advance_widths[layer_name] = aw
                left_sidebearings[layer_name] = 0

                layers_for_glyph.append((layer_name, color_index))

        glyphs[gname] = base_pen.glyph()
        if layers_for_glyph:
            color_layers[gname] = layers_for_glyph

    # cmap (base glyphs only)
    cmap: Dict[int, str] = {}
    for cp in sorted(filtered.keys()):
        if cp == 32:
            cmap[cp] = "space"
        else:
            cmap[cp] = glyph_name_for_codepoint(cp)

    fb = FontBuilder(metrics.upm, isTTF=True)
    fb.setupGlyphOrder(glyph_order)

    # hmtx for all glyphs
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
            "familyName": family_name,
            "styleName": style_name,
            "uniqueFontIdentifier": f"{family_name} {style_name}",
            "fullName": f"{family_name} {style_name}",
            "psName": f"{family_name.replace(' ', '')}-{style_name.replace(' ', '')}",
            "version": "Version 1.000",
            "manufacturer": vendor_id,
            "designer": vendor_id,
        }
    )
    fb.setupPost(keepGlyphNames=True)

    # Recalc bounds for all glyphs (helps some toolchains)
    glyf = fb.font["glyf"]
    for gn in fb.font.getGlyphOrder():
        try:
            glyf[gn].recalcBounds(glyf)
        except Exception:
            pass
    # Tell fontTools to recalc head bbox on save (in many versions this is a bool flag)
    try:
        fb.font.recalcBBoxes = True
    except Exception:
        pass

    # Attach color tables MANUALLY (the main fix)
    attach_cpal_manual(fb.font, palette)
    attach_colr_manual(fb.font, color_layers)

    # Save TTF
    ttf_path = out_base.with_suffix(".ttf")
    fb.save(ttf_path)

    # Debug sanity: confirm COLR v0 + layer counts + some bboxes
    if debug:
        t = TTFont(ttf_path)
        print(f"[colr] version={t['COLR'].version} layers(A)={len(t['COLR'].ColorLayers.get('uni0041', []))}")
        for cp in (0x41, 0x40, 0x4D):
            gn = glyph_name_for_codepoint(cp)
            g = t["glyf"][gn]
            g.recalcBounds(t["glyf"])
            print(f"[bbox] {hex(cp)} {gn}: xMin={g.xMin} xMax={g.xMax} yMin={g.yMin} yMax={g.yMax}")

    # Save WOFF/WOFF2 using fresh TTFont objects
    woff_path = out_base.with_suffix(".woff")
    f1 = TTFont(ttf_path)
    f1.flavor = "woff"
    f1.save(woff_path)

    woff2_path = out_base.with_suffix(".woff2")
    f2 = TTFont(ttf_path)
    f2.flavor = "woff2"
    try:
        f2.save(woff2_path)
    except Exception as e:
        raise RuntimeError(
            "WOFF2 save failed. You likely need `brotli` installed.\n"
            "Try: pip install brotli\n"
            f"Original error: {e}"
        ) from e

    print("Wrote:")
    print(f"  {ttf_path}")
    print(f"  {woff_path}")
    print(f"  {woff2_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate defont (TTF/WOFF/WOFF2) from data/chars.php")
    ap.add_argument("--chars", type=str, default=str(find_project_chars()), help="Path to data/chars.php")
    ap.add_argument("--out", type=str, default="dist/fonts/defont", help="Output base path (no extension)")
    ap.add_argument("--family", type=str, default="defont", help="Font family name")
    ap.add_argument("--style", type=str, default="Regular", help="Font style name")
    ap.add_argument("--vendor", type=str, default="deidee", help="Vendor/manufacturer string")
    ap.add_argument("--seed", type=int, default=0, help="Random seed (0 => YYYYMMDD)")
    ap.add_argument("--jitter", type=int, default=3, help="Max jitter like PHP (0..3 recommended)")
    ap.add_argument("--debug", action="store_true", help="Print debug info")
    args = ap.parse_args()

    if args.seed == 0:
        args.seed = int(datetime.now().strftime("%Y%m%d"))

    chars = run_php_chars_to_json(Path(args.chars), debug=args.debug)

    build_font(
        chars=chars,
        out_base=Path(args.out),
        family_name=args.family,
        style_name=args.style,
        vendor_id=args.vendor,
        seed=args.seed,
        jitter_px_max=max(0, int(args.jitter)),
        metrics=Metrics(),
        debug=args.debug,
    )


if __name__ == "__main__":
    main()
