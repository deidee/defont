#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
tools/generate-fonts.py

Generates:
  dist/fonts/defont.ttf
  dist/fonts/defont.woff
  dist/fonts/defont.woff2
  dist/fonts/defont.css  (copied from src/style/main.css)

Reads bitmap glyphs and ligatures from:
  data/chars.php ($c array)

Now also supports OpenType ligatures:
  - Numeric keys remain normal Unicode glyphs
  - String keys with len(key) > 1 are treated as ligatures
  - Ligatures are emitted as GSUB 'liga' substitutions

Key fixes:
  - Horizontal metrics: LSB MUST match glyph xMin (after bounds are calculated),
    including for COLR layer glyphs. This is critical for correct colored rendering.
  - Optional per-pixel "mt_rand(0,3)"-style block extension to the RIGHT and BOTTOM
    (deterministic per glyph+pixel+seed), similar to the PHP generator.

Notes:
  - Alpha transparency stored in CPAL as 0..255; default alpha=128 (~0.5)
  - Suppresses PHP warnings/notices and extracts JSON blob from stdout if needed
"""

from __future__ import annotations

import argparse
import json
import os
import time
import subprocess
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

    # “Pixel” cell size in font units
    cell: int = 85

    # Padding in cells
    # NOTE: default inter-letter gap is left_pad + right_pad
    left_pad: int = 1
    right_pad: int = 0  # <- 1-block default letter spacing (with left_pad=1)
    bottom_pad: int = 1
    top_pad: int = 1

    # Extra spacing (in cells) added after glyph width
    letterspacing: int = 0


def glyph_name_for_codepoint(cp: int) -> str:
    if cp == 32:
        return "space"
    if cp <= 0xFFFF:
        return f"uni{cp:04X}"
    return f"u{cp:06X}"


def glyph_name_for_ligature(token: str) -> str:
    parts: List[str] = []
    for ch in token:
        cp = ord(ch)
        if cp <= 0xFFFF:
            parts.append(f"u{cp:04X}")
        else:
            parts.append(f"u{cp:06X}")
    return "liga_" + "_".join(parts) if parts else "liga_empty"


def rect_to_pen(pen: TTGlyphPen, x0: int, y0: int, x1: int, y1: int) -> None:
    pen.moveTo((x0, y0))
    pen.lineTo((x1, y0))
    pen.lineTo((x1, y1))
    pen.lineTo((x0, y1))
    pen.closePath()


def find_upwards(start: Path, rel: str) -> Path | None:
    start = start.resolve()
    for parent in [start] + list(start.parents):
        cand = parent / rel
        if cand.exists():
            return cand
    return None


def find_project_file(rel: str) -> Path:
    here = Path(__file__).resolve().parent
    found = find_upwards(here, rel)
    return found if found is not None else Path(rel)


def run_php_chars_to_json(chars_php: Path, debug: bool = False) -> Tuple[Dict[int, List[int]], Dict[str, List[int]]]:
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

    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
    )
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
    ligatures: Dict[str, List[int]] = {}
    skipped: List[str] = []

    for k, v in raw.items():
        ks = str(k)
        if v is None:
            pixels: List[int] = []
        elif isinstance(v, list):
            pixels = [int(x) for x in v]
        else:
            raise TypeError(f"Unexpected value type for key {ks}: {type(v)}")

        if ks.isdigit():
            cmap[int(ks)] = pixels
            continue

        if ks == ".notdef":
            continue

        if len(ks) > 1:
            ligatures[ks] = pixels
        elif debug:
            skipped.append(ks)

    if debug:
        print(f"[run_php_chars_to_json] php exit={res.returncode}")
        print(f"[run_php_chars_to_json] stdout_len={len(stdout)} stderr_len={len(stderr)}")
        print(
            f"[run_php_chars_to_json] numeric_keys={len(cmap)} "
            f"ligature_keys={len(ligatures)} skipped_non_numeric={len(skipped)}"
        )
        if skipped:
            print(f"[run_php_chars_to_json] skipped keys (first 20): {skipped[:20]}")

    return cmap, ligatures


def mix32(x: int) -> int:
    x &= 0xFFFFFFFF
    x ^= (x >> 16)
    x = (x * 0x7FEB352D) & 0xFFFFFFFF
    x ^= (x >> 15)
    x = (x * 0x846CA68B) & 0xFFFFFFFF
    x ^= (x >> 16)
    return x & 0xFFFFFFFF


def stable_text_hash(text: str) -> int:
    h = 0x811C9DC5
    for ch in text:
        cp = ord(ch)
        h = mix32(h ^ cp ^ ((cp << 11) & 0xFFFFFFFF))
    return h & 0xFFFFFFFF


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


def attach_cpal(font: TTFont, palette_rgba: List[Tuple[int, int, int, int]]) -> None:
    """
    CPAL colors as C_P_A_L_.Color objects (constructor order BGRA).
    This matches older fontTools that expects .blue/.green/.red/.alpha.
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


def attach_colr_v0(font: TTFont, color_layers: Dict[str, List[Tuple[str, int]]]) -> None:
    """
    COLR v0 with LayerRecord objects (older fontTools requires .name).
    """
    from fontTools.ttLib.tables.C_O_L_R_ import LayerRecord  # type: ignore

    colr = newTable("COLR")
    colr.version = 0
    colr.ColorLayers = {}

    for base_glyph, layers in color_layers.items():
        layer_records: List[LayerRecord] = []
        for layer_glyph, color_id in layers:
            lr = LayerRecord()
            lr.name = str(layer_glyph)
            lr.colorID = int(color_id)
            if hasattr(lr, "glyphName"):
                lr.glyphName = str(layer_glyph)
            layer_records.append(lr)
        colr.ColorLayers[str(base_glyph)] = layer_records

    font["COLR"] = colr


def attach_ligature_features(
    font: TTFont,
    chars: Dict[int, List[int]],
    ligatures: Dict[str, List[int]],
    debug: bool = False,
) -> Tuple[int, int]:
    try:
        from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
    except Exception as e:
        if ligatures:
            print(f"[warn] could not import fontTools.feaLib.builder; skipping ligatures: {e}")
        return (0, len(ligatures))

    lines: List[str] = [
        "languagesystem DFLT dflt;",
        "languagesystem latn dflt;",
        "",
        "feature liga {",
    ]

    added = 0
    skipped = 0

    for token in sorted(ligatures.keys(), key=lambda s: (len(s), s)):
        cps = [ord(ch) for ch in token]
        component_names: List[str] = []
        missing: List[int] = []

        for cp in cps:
            if cp == 32:
                component_names.append("space")
                continue
            if cp not in chars:
                missing.append(cp)
                continue
            component_names.append(glyph_name_for_codepoint(cp))

        if missing:
            skipped += 1
            if debug:
                missing_hex = ", ".join(
                    f"U+{cp:04X}" if cp <= 0xFFFF else f"U+{cp:06X}"
                    for cp in missing
                )
                print(f"[warn] skipping ligature {token!r}: missing component glyph(s): {missing_hex}")
            continue

        if not component_names:
            skipped += 1
            continue

        lig_gname = glyph_name_for_ligature(token)
        lines.append(f"    sub {' '.join(component_names)} by {lig_gname};")
        added += 1

    lines.append("} liga;")

    if added == 0:
        return (0, skipped)

    feature_text = "\n".join(lines) + "\n"
    addOpenTypeFeaturesFromString(font, feature_text)

    if debug:
        print("[debug] GSUB feature text:")
        print(feature_text)

    return (added, skipped)


def copy_main_css_to_dist(out_css: Path, debug: bool = False) -> None:
    src_css = find_project_file("src/style/main.css").resolve()
    out_css = Path(out_css).resolve()
    out_css.parent.mkdir(parents=True, exist_ok=True)

    if not src_css.exists():
        raise FileNotFoundError(f"Could not find CSS source file: {src_css}")

    data = src_css.read_text(encoding="utf-8")
    out_css.write_text(data, encoding="utf-8")

    if debug:
        print(f"[css] wrote {out_css} from {src_css} ({len(data)} bytes)")


def build_font(
    chars: Dict[int, List[int]],
    ligatures: Dict[str, List[int]],
    out_base: Path,
    family_name: str,
    style_name: str,
    vendor_id: str,
    seed: int,
    metrics: Metrics,
    palette_size: int,
    alpha: int,
    mono: bool,
    jitter_px: int,
    php_size: int,
    debug: bool = False,
) -> None:
    out_base = Path(out_base)
    out_base.parent.mkdir(parents=True, exist_ok=True)

    # Filter glyphs: allow empty, or lengths divisible by ROWS.
    filtered_chars: Dict[int, List[int]] = {}
    for cp, pixels in chars.items():
        pixels = pixels or []
        if len(pixels) == 0:
            filtered_chars[cp] = []
        elif len(pixels) % ROWS == 0:
            filtered_chars[cp] = pixels
        else:
            if debug:
                print(f"[warn] skipping {hex(cp)}: len={len(pixels)} not divisible by ROWS={ROWS}")

    filtered_ligatures: Dict[str, List[int]] = {}
    for token, pixels in ligatures.items():
        pixels = pixels or []
        if len(pixels) == 0:
            filtered_ligatures[token] = []
        elif len(pixels) % ROWS == 0:
            filtered_ligatures[token] = pixels
        else:
            if debug:
                print(f"[warn] skipping ligature {token!r}: len={len(pixels)} not divisible by ROWS={ROWS}")

    cell = metrics.cell

    def aw_for_cols(cols: int) -> int:
        return int((metrics.left_pad + cols + metrics.right_pad + metrics.letterspacing) * cell)

    # Palette (if not mono)
    palette_size = max(1, min(int(palette_size), 256))
    alpha = max(0, min(int(alpha), 255))
    palette = make_palette_dejade(palette_size=palette_size, seed=seed, alpha=alpha)

    # === mt_rand-style jitter (right & bottom extension) ===
    jitter_px = max(0, int(jitter_px))
    php_size = max(1, int(php_size))
    # scale PHP jitter in "pixels" to font units
    jitter_units = int(round(cell * (jitter_px / php_size)))

    def jitter_xy(key: int) -> Tuple[int, int]:
        if jitter_units <= 0:
            return (0, 0)
        jx = int(mix32(key ^ 0xA5A5A5A5) % (jitter_units + 1))
        jy = int(mix32(key ^ 0x5A5A5A5A) % (jitter_units + 1))
        return (jx, jy)

    glyph_order: List[str] = [".notdef", "space"]
    glyphs: Dict[str, Any] = {}
    advance_widths: Dict[str, int] = {}

    # base glyph -> list of (layerGlyphName, colorID)
    color_layers: Dict[str, List[Tuple[str, int]]] = {}

    # .notdef
    pen_notdef = TTGlyphPen(None)
    rect_to_pen(pen_notdef, 80, 80, metrics.upm - 80, metrics.upm - 80)
    glyphs[".notdef"] = pen_notdef.glyph()
    advance_widths[".notdef"] = metrics.upm

    # space
    glyphs["space"] = TTGlyphPen(None).glyph()
    advance_widths["space"] = aw_for_cols(1)

    def add_bitmap_glyph(gname: str, pixels: List[int], glyph_hash: int) -> None:
        if gname not in glyph_order:
            glyph_order.append(gname)

        cols = 1 if not pixels else (len(pixels) // ROWS)
        aw = aw_for_cols(cols)
        advance_widths[gname] = aw

        if not pixels:
            glyphs[gname] = TTGlyphPen(None).glyph()
            continue_flag = False
            if continue_flag:
                pass
            return

        if mono:
            pen = TTGlyphPen(None)
            for idx, bit in enumerate(pixels):
                if int(bit) != 1:
                    continue
                row = idx // cols
                col = idx % cols

                x0 = int((metrics.left_pad + col) * cell)
                y0 = int((metrics.bottom_pad + (ROWS - 1 - row)) * cell)

                key = (seed * 131071) + (glyph_hash * 4099) + idx
                jx, jy = jitter_xy(key)

                x1 = x0 + cell + jx           # extend RIGHT
                y1 = y0 + cell                # keep TOP fixed
                y0j = y0 - jy                 # extend BOTTOM (downwards)

                rect_to_pen(pen, x0, y0j, x1, y1)

            glyphs[gname] = pen.glyph()
            return

        # Color build: keep base empty; layers draw pixels
        glyphs[gname] = TTGlyphPen(None).glyph()
        layers: Dict[int, TTGlyphPen] = {}

        for idx, bit in enumerate(pixels):
            if int(bit) != 1:
                continue

            row = idx // cols
            col = idx % cols

            x0 = int((metrics.left_pad + col) * cell)
            y0 = int((metrics.bottom_pad + (ROWS - 1 - row)) * cell)

            key = (seed * 131071) + (glyph_hash * 4099) + idx

            # color selection
            color_id = int(mix32(key) % palette_size)

            # jitter
            jx, jy = jitter_xy(key)

            x1 = x0 + cell + jx
            y1 = y0 + cell
            y0j = y0 - jy

            pen = layers.get(color_id)
            if pen is None:
                pen = TTGlyphPen(None)
                layers[color_id] = pen

            rect_to_pen(pen, x0, y0j, x1, y1)

        color_layers[gname] = []
        for color_id in sorted(layers.keys()):
            layer_name = f"{gname}.c{color_id:03d}"
            glyph_order.append(layer_name)
            glyphs[layer_name] = layers[color_id].glyph()
            advance_widths[layer_name] = aw
            color_layers[gname].append((layer_name, color_id))

    for cp in sorted(filtered_chars.keys()):
        if cp == 32:
            continue

        pixels = filtered_chars[cp]
        gname = glyph_name_for_codepoint(cp)
        if debug and cp in (0x30, 0x40, 0x41, 0x45, 0x4D, 0x61):
            cols_dbg = 1 if not pixels else (len(pixels) // ROWS)
            print(f"[debug] {hex(cp)} len(pixels)={len(pixels)} cols={cols_dbg}")

        add_bitmap_glyph(gname, pixels, cp)

    for token in sorted(filtered_ligatures.keys(), key=lambda s: (len(s), s)):
        pixels = filtered_ligatures[token]
        gname = glyph_name_for_ligature(token)
        add_bitmap_glyph(gname, pixels, stable_text_hash(token))

    # cmap (base Unicode glyphs only)
    cmap: Dict[int, str] = {}
    for cp in sorted(filtered_chars.keys()):
        cmap[cp] = "space" if cp == 32 else glyph_name_for_codepoint(cp)

    fb = FontBuilder(metrics.upm, isTTF=True)
    fb.setupGlyphOrder(glyph_order)
    fb.setupCharacterMap(cmap)

    # Build glyf before hmtx, so we can compute xMin and set LSB correctly
    fb.setupGlyf(glyphs)

    # === CRITICAL FIX: LSB MUST MATCH xMin (after recalcBounds) ===
    glyf_table = fb.font["glyf"]
    hmtx: Dict[str, Tuple[int, int]] = {}
    for gn in glyph_order:
        g = glyf_table[gn]
        try:
            g.recalcBounds(glyf_table)
            x_min = int(getattr(g, "xMin", 0) or 0)
        except Exception:
            x_min = 0
        aw = int(advance_widths.get(gn, metrics.upm))
        hmtx[gn] = (aw, x_min)
    fb.setupHorizontalMetrics(hmtx)

    # Remaining required tables
    fb.setupHorizontalHeader(ascent=metrics.ascent, descent=metrics.descent)
    fb.setupMaxp()
    fb.setupHead()

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

    # Recalc bounds (hygiene)
    for gn in glyph_order:
        try:
            glyf_table[gn].recalcBounds(glyf_table)
        except Exception:
            pass

    # Color tables (with alpha)
    if not mono:
        attach_cpal(fb.font, palette)
        attach_colr_v0(fb.font, color_layers)

    # OpenType ligatures
    added_liga, skipped_liga = attach_ligature_features(
        font=fb.font,
        chars=filtered_chars,
        ligatures=filtered_ligatures,
        debug=debug,
    )

    # Save TTF
    ttf_path = out_base.with_suffix(".ttf")
    fb.save(ttf_path)

    # Save WOFF / WOFF2
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
    print(f"Ligatures added: {added_liga}")
    if skipped_liga:
        print(f"Ligatures skipped: {skipped_liga}")

    if debug:
        t = TTFont(ttf_path)
        if not mono and "COLR" in t and "CPAL" in t:
            layers_a = t["COLR"].ColorLayers.get("uni0041", [])
            pe = len(t["CPAL"].palettes[0])
            print(
                f"[debug] COLR layers(uni0041)={len(layers_a)}  CPAL entries={pe}  "
                f"alpha={alpha}  jitter_px={jitter_px} jitter_units={jitter_units}"
            )
        print(f"[debug] filtered glyphs={len(filtered_chars)} filtered ligatures={len(filtered_ligatures)}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate defont (TTF/WOFF/WOFF2) from data/chars.php, including ligatures"
    )
    ap.add_argument("--chars", type=str, default=str(find_project_file("data/chars.php")), help="Path to data/chars.php")
    ap.add_argument("--out", type=str, default="dist/fonts/defont", help="Output base path (no extension)")
    ap.add_argument("--family", type=str, default="defont", help="Font family name")
    ap.add_argument("--style", type=str, default="Regular", help="Font style name")
    ap.add_argument("--vendor", type=str, default="deidee", help="Vendor/manufacturer string")

    ap.add_argument("--seed", type=int, default=0, help="Seed (0 => YYYYMMDD)")
    ap.add_argument("--palette", type=int, default=128, help="Palette size (1..256)")
    ap.add_argument("--alpha", type=int, default=128, help="Alpha 0..255 (default 128 ≈ 0.5)")

    ap.add_argument(
        "--jitter-px",
        type=int,
        default=3,
        help="Max mt_rand-style jitter amount (0..3 like PHP). Set 0 to disable.",
    )
    ap.add_argument(
        "--php-size",
        type=int,
        default=24,
        help="Reference PHP block size for scaling jitter (default 24).",
    )

    ap.add_argument("--mono", action="store_true", help="Monochrome build (no CPAL/COLR)")
    ap.add_argument("--debug", action="store_true", help="Debug output")
    args = ap.parse_args()

    if args.seed == 0:
        args.seed = int(datetime.now().strftime("%Y%m%d"))

    chars, ligatures = run_php_chars_to_json(Path(args.chars), debug=args.debug)

    build_font(
        chars=chars,
        ligatures=ligatures,
        out_base=Path(args.out),
        family_name=args.family,
        style_name=args.style,
        vendor_id=args.vendor,
        seed=int(args.seed),
        metrics=Metrics(),
        palette_size=int(args.palette),
        alpha=int(args.alpha),
        mono=bool(args.mono),
        jitter_px=int(args.jitter_px),
        php_size=int(args.php_size),
        debug=bool(args.debug),
    )

    copy_main_css_to_dist(Path("dist/fonts/defont.css"), debug=args.debug)


if __name__ == "__main__":
    main()