#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
tools/generate-fonts.py

Builds:
  dist/fonts/defont.ttf
  dist/fonts/defont.woff
  dist/fonts/defont.woff2

Reads bitmap glyphs from: data/chars.php ($c array)

Modes:
  --mono        : no CPAL/COLR (baseline; known-good)
  --flat-green  : CPAL/COLR with exactly one opaque green color and ONE layer per glyph
  default       : CPAL/COLR with small palette; rectangles grouped per colorID

Compatibility:
  - CPAL palettes use C_P_A_L_.Color objects (your fontTools requires .blue/.green/.red/.alpha)
  - COLR v0 layers use C_O_L_R_.LayerRecord objects (your fontTools requires .name)

Important debugging choice:
  - In ALL color modes, the BASE glyph is EMPTY and only layers draw pixels.
    This prevents “base + wrong layer” from looking like stray pixels.
"""

import argparse
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable

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


def mix32(x: int) -> int:
    x &= 0xFFFFFFFF
    x ^= (x >> 16)
    x = (x * 0x7FEB352D) & 0xFFFFFFFF
    x ^= (x >> 15)
    x = (x * 0x846CA68B) & 0xFFFFFFFF
    x ^= (x >> 16)
    return x & 0xFFFFFFFF


def make_palette_dejade(palette_size: int, seed: int, alpha: int) -> List[Tuple[int, int, int, int]]:
    import random
    rng = random.Random(seed)

    pal: List[Tuple[int, int, int, int]] = []
    for _ in range(palette_size):
        r = rng.randint(0, 127)
        g = rng.randint(127, 255)
        b = rng.randint(0, 191)
        pal.append((r, g, b, alpha))

    if datetime.now().month == 10 and pal:
        pal[0] = (255, 68, 136, alpha)
        rng.shuffle(pal)

    return pal


def attach_cpal_manual(font: TTFont, palette_rgba: List[Tuple[int, int, int, int]]) -> None:
    """
    Your fontTools expects CPAL colors as C_P_A_L_.Color objects with .blue/.green/.red/.alpha.
    Constructor order is BGRA.
    """
    from fontTools.ttLib.tables.C_P_A_L_ import Color  # type: ignore

    cpal = newTable("CPAL")
    cpal.version = 0
    cpal.numPaletteEntries = len(palette_rgba)
    cpal.palettes = [[Color(int(b), int(g), int(r), int(a)) for (r, g, b, a) in palette_rgba]]

    if hasattr(cpal, "paletteTypes"):
        cpal.paletteTypes = [0]
    if hasattr(cpal, "paletteLabels"):
        cpal.paletteLabels = []
    if hasattr(cpal, "paletteEntryLabels"):
        cpal.paletteEntryLabels = []

    font["CPAL"] = cpal


def make_layer_record(layer_glyph: str, color_id: int):
    """
    Your fontTools requires LayerRecord objects with .name.
    """
    from fontTools.ttLib.tables.C_O_L_R_ import LayerRecord  # type: ignore

    lr = LayerRecord()
    lr.name = str(layer_glyph)
    lr.colorID = int(color_id)
    # Some versions also have glyphName; set if present (harmless, but helps other builds)
    if hasattr(lr, "glyphName"):
        lr.glyphName = str(layer_glyph)
    return lr


def attach_colr_manual(font: TTFont, color_layers: Dict[str, List[Tuple[str, int]]]) -> None:
    """
    COLR v0 using LayerRecord objects (required by your fontTools).
    """
    colr = newTable("COLR")
    colr.version = 0
    colr.ColorLayers = {}
    for base_glyph, layers in color_layers.items():
        colr.ColorLayers[base_glyph] = [make_layer_record(gn, ci) for (gn, ci) in layers]
    font["COLR"] = colr


def build_font(
    chars: Dict[int, List[int]],
    out_base: Path,
    family_name: str,
    style_name: str,
    vendor_id: str,
    seed: int,
    metrics: Metrics,
    mono: bool,
    flat_green: bool,
    palette_size: int,
    alpha: int,
    debug: bool = False,
) -> None:
    out_base = Path(out_base)
    out_base.parent.mkdir(parents=True, exist_ok=True)

    # Filter glyphs: allow empty, or lengths divisible by ROWS.
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

    # Palette
    if flat_green:
        palette = [(0, 255, 0, 255)]
        palette_size = 1
        alpha = 255
    else:
        palette_size = max(1, min(int(palette_size), 256))
        alpha = max(0, min(int(alpha), 255))
        palette = make_palette_dejade(palette_size=palette_size, seed=seed, alpha=alpha)

    glyph_order: List[str] = [".notdef", "space"]
    glyphs: Dict[str, Any] = {}
    advance_widths: Dict[str, int] = {}
    left_sidebearings: Dict[str, int] = {}

    # base glyph -> list of (layerGlyphName, colorID)
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

        if not pixels:
            glyphs[gname] = TTGlyphPen(None).glyph()
            continue

        if mono:
            # Monochrome build: draw pixels directly in base glyph
            pen = TTGlyphPen(None)
            for idx, bit in enumerate(pixels):
                if int(bit) != 1:
                    continue
                row = idx // cols
                col = idx % cols
                x0 = int((metrics.left_pad + col) * cell)
                y0 = int((metrics.bottom_pad + (ROWS - 1 - row)) * cell)
                rect_to_pen(pen, x0, y0, x0 + cell, y0 + cell)
            glyphs[gname] = pen.glyph()
            continue

        # COLOR builds: keep BASE glyph EMPTY so we only see COLR output
        glyphs[gname] = TTGlyphPen(None).glyph()

        # Flat green sanity: one layer = entire glyph mask
        if flat_green:
            pen = TTGlyphPen(None)
            for idx, bit in enumerate(pixels):
                if int(bit) != 1:
                    continue
                row = idx // cols
                col = idx % cols
                x0 = int((metrics.left_pad + col) * cell)
                y0 = int((metrics.bottom_pad + (ROWS - 1 - row)) * cell)
                rect_to_pen(pen, x0, y0, x0 + cell, y0 + cell)

            layer_name = f"{gname}.fg"
            glyph_order.append(layer_name)
            glyphs[layer_name] = pen.glyph()
            advance_widths[layer_name] = aw
            left_sidebearings[layer_name] = 0

            color_layers[gname] = [(layer_name, 0)]
            continue

        # Normal palette: group rects by color id
        pens_by_color: Dict[int, TTGlyphPen] = {}
        for idx, bit in enumerate(pixels):
            if int(bit) != 1:
                continue
            row = idx // cols
            col = idx % cols
            x0 = int((metrics.left_pad + col) * cell)
            y0 = int((metrics.bottom_pad + (ROWS - 1 - row)) * cell)
            x1 = x0 + cell
            y1 = y0 + cell

            key = (seed * 131071) + (cp * 4099) + idx
            color_id = mix32(key) % palette_size

            pen = pens_by_color.get(color_id)
            if pen is None:
                pen = TTGlyphPen(None)
                pens_by_color[color_id] = pen
            rect_to_pen(pen, x0, y0, x1, y1)

        layers: List[Tuple[str, int]] = []
        for color_id in sorted(pens_by_color.keys()):
            layer_name = f"{gname}.c{color_id:03d}"
            glyph_order.append(layer_name)
            glyphs[layer_name] = pens_by_color[color_id].glyph()
            advance_widths[layer_name] = aw
            left_sidebearings[layer_name] = 0
            layers.append((layer_name, color_id))
        color_layers[gname] = layers

        if debug and cp in (0x41, 0x40):
            print(f"[layers] {gname}: {len(layers)} (palette_size={palette_size})")

    # cmap (base glyphs only)
    cmap: Dict[int, str] = {}
    for cp in sorted(filtered.keys()):
        cmap[cp] = "space" if cp == 32 else glyph_name_for_codepoint(cp)

    fb = FontBuilder(metrics.upm, isTTF=True)
    fb.setupGlyphOrder(glyph_order)
    fb.setupGlyf(glyphs)


    glyf_table = fb.font["glyf"]
    hmtx = {}
    for gn in glyph_order:
        glyph = glyf_table[gn]
        glyph.recalcBounds(glyf_table)
        # The LSB MUST match the xMin!
        actual_x_min = glyph.xMin  if hasattr(glyph, "xMin") else 0
        aw =  int(advance_widths.get(gn, metrics.upm))
        hmtx[gn] = (aw,  int(actual_x_min))
    fb.setupHorizontalMetrics(hmtx)

    fb.setupCharacterMap(cmap)

    fb.setupHead()
    fb.setupMaxp()
    fb.setupHorizontalHeader(ascent=metrics.ascent, descent=metrics.descent)

    # head timestamps (avoid “timestamp seems very low” warnings)
    tt_epoch_offset = 2082844800  # seconds 1904->1970
    now_tt = int(time.time()) + tt_epoch_offset
    fb.font["head"].created = now_tt
    fb.font["head"].modified = now_tt

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
    fb.setupPost()

    # bounds (not strictly required for display but good hygiene)
    glyf = fb.font["glyf"]
    for gn in fb.font.getGlyphOrder():
        try:
            glyf[gn].recalcBounds(glyf)
        except Exception:
            pass

    # color tables
    if not mono:
        attach_cpal_manual(fb.font, palette)
        attach_colr_manual(fb.font, color_layers)

    # save ttf
    ttf_path = out_base.with_suffix(".ttf")
    fb.save(ttf_path)

    if debug:
        t = TTFont(ttf_path)
        if not mono and "COLR" in t:
            la = t["COLR"].ColorLayers.get("uni0041", [])
            pe = len(t["CPAL"].palettes[0])
            print(f"[colr] version={t['COLR'].version} layers(A)={len(la)} palette_entries={pe}")

    # woff / woff2 from fresh TTFont instances
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
    ap = argparse.ArgumentParser(description="Generate defont (TTF/WOFF/WOFF2) from data/chars.php")
    ap.add_argument("--chars", type=str, default=str(find_project_chars()), help="Path to data/chars.php")
    ap.add_argument("--out", type=str, default="dist/fonts/defont", help="Output base path (no extension)")
    ap.add_argument("--family", type=str, default="defont", help="Font family name")
    ap.add_argument("--style", type=str, default="Regular", help="Font style name")
    ap.add_argument("--vendor", type=str, default="deidee", help="Vendor/manufacturer string")

    ap.add_argument("--seed", type=int, default=0, help="Seed (0 => YYYYMMDD)")
    ap.add_argument("--palette", type=int, default=10, help="Palette size (small is safer on Windows)")
    ap.add_argument("--alpha", type=int, default=255, help="Alpha 0..255 (default opaque)")

    ap.add_argument("--mono", action="store_true", help="Monochrome build (no CPAL/COLR)")
    ap.add_argument("--flat-green", action="store_true", help="Single opaque green, one COLR layer per glyph")
    ap.add_argument("--debug", action="store_true", help="Debug output")
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
        metrics=Metrics(),
        mono=bool(args.mono),
        flat_green=bool(args.flat_green),
        palette_size=int(args.palette),
        alpha=int(args.alpha),
        debug=bool(args.debug),
    )


if __name__ == "__main__":
    main()
