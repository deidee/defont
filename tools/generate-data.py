#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/generate-data.py

Parse glyph data from data/chars.php and export it to:
- data/chars.json   (canonical, easy to consume from Python + JS)
- data/chars.py     (optional Python module)
- data/chars.mjs    (optional JS module)
- config/defont.php (runtime-friendly CakePHP config export)

Ligatures:
- Any *string key* (except ".notdef") with len(key) > 1 is treated as a ligature.
- meta.ligature_keys contains the list of such keys (strings).

Notes:
- The parser reads PHP source text; it does NOT execute PHP.
- It expects assignments like: $c[0x41] = array(...);
- Empty glyphs (e.g. space) have no dimensions in the PHP source, so width/height
  are exported as null.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ASSIGN_RE = re.compile(
    r"\$c\[(?P<key>.*?)\]\s*=\s*array\s*\((?P<body>.*?)\)\s*;",
    re.DOTALL,
)

INT_RE = re.compile(r"-?\d+")


def strip_php_comments(text: str) -> str:
    """Remove // line comments and /* ... */ block comments."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    return text


def parse_php_string_literal(raw: str) -> str:
    """
    Minimal PHP string literal parser for simple keys like '.notdef' or 'ΤΤΤ'.
    Supports single or double quotes and basic escaped quote/backslash.
    """
    raw = raw.strip()
    if len(raw) < 2 or raw[0] not in ("'", '"') or raw[-1] != raw[0]:
        raise ValueError(f"Invalid PHP string literal: {raw!r}")

    quote = raw[0]
    s = raw[1:-1]

    # Minimal escaping (enough for keys used here)
    s = s.replace("\\\\", "\\")
    if quote == "'":
        s = s.replace("\\'", "'")
    else:
        s = s.replace('\\"', '"')

    return s


def parse_key(raw: str) -> str | int:
    raw = raw.strip()

    if (raw.startswith("'") and raw.endswith("'")) or (raw.startswith('"') and raw.endswith('"')):
        return parse_php_string_literal(raw)

    if raw.lower().startswith("0x"):
        return int(raw, 16)

    return int(raw, 10)


def infer_dimensions(values: list[int]) -> tuple[int | None, int | None]:
    """
    Infer width/height from flat row-major glyph arrays.

    Heuristic (based on your data):
    - Prefer heights: 9, 7, 5, 3
    - Fallback to factor pair closest to square
    """
    n = len(values)
    if n == 0:
        return None, None

    preferred_heights = (9, 7, 5, 3)
    for h in preferred_heights:
        if n % h == 0:
            w = n // h
            if 1 <= w <= 64:
                return w, h

    # Fallback: choose factor pair closest to square
    best: tuple[int, int] | None = None
    best_score: int | None = None
    for h in range(1, n + 1):
        if n % h != 0:
            continue
        w = n // h
        score = abs(w - h)
        if best is None or score < (best_score if best_score is not None else 10**9):
            best = (w, h)
            best_score = score

    if best is None:
        return None, None

    return best


def reshape_rows(values: list[int], width: int | None, height: int | None) -> list[list[int]]:
    if not values or width is None or height is None:
        return []

    expected = width * height
    if expected != len(values):
        raise ValueError(f"Dimension mismatch: {width}x{height} != {len(values)}")

    return [values[r * width:(r + 1) * width] for r in range(height)]


def compute_bbox(rows: list[list[int]]) -> dict[str, int] | None:
    if not rows:
        return None

    xs: list[int] = []
    ys: list[int] = []

    for y, row in enumerate(rows):
        for x, v in enumerate(row):
            if v:
                xs.append(x)
                ys.append(y)

    if not xs or not ys:
        return None

    return {
        "x": min(xs),
        "y": min(ys),
        "width": max(xs) - min(xs) + 1,
        "height": max(ys) - min(ys) + 1,
    }


def normalize_glyph(key: str | int, values: list[int]) -> dict[str, Any]:
    width, height = infer_dimensions(values)
    rows = reshape_rows(values, width, height)

    entry: dict[str, Any] = {
        "key": key,
        "width": width,
        "height": height,
        "data": values,   # flat row-major
        "rows": rows,     # nested rows (convenient for rendering)
        "active_pixels": sum(1 for v in values if v != 0),
        "bbox": compute_bbox(rows),
    }

    # Codepoint glyphs (classic $c[0x....] keys)
    if isinstance(key, int):
        entry["codepoint"] = key
        entry["unicode"] = f"U+{key:04X}"
        try:
            entry["char"] = chr(key)
        except ValueError:
            entry["char"] = None
        entry["is_ligature"] = False

    # String glyphs (.notdef, ligatures, or any other named glyphs)
    else:
        entry["codepoint"] = None
        entry["unicode"] = None
        entry["char"] = None

        is_lig = (key != ".notdef") and (len(key) > 1)
        entry["is_ligature"] = is_lig

        if is_lig:
            entry["sequence"] = key
            entry["sequence_chars"] = list(key)
            entry["sequence_codepoints"] = [ord(ch) for ch in key]
            entry["sequence_unicode"] = [f"U+{ord(ch):04X}" for ch in key]
        else:
            entry["sequence"] = None
            entry["sequence_chars"] = None
            entry["sequence_codepoints"] = None
            entry["sequence_unicode"] = None

    return entry


def parse_php_chars(php_text: str) -> OrderedDict[str | int, dict[str, Any]]:
    glyphs: OrderedDict[str | int, dict[str, Any]] = OrderedDict()

    for m in ASSIGN_RE.finditer(php_text):
        key = parse_key(m.group("key"))
        body = strip_php_comments(m.group("body"))
        values = [int(s) for s in INT_RE.findall(body)]
        glyphs[key] = normalize_glyph(key, values)

    if not glyphs:
        raise ValueError("No glyph assignments found. Expected lines like $c[0x41] = array(...);")

    return glyphs


def _is_printable_char(ch: str) -> bool:
    return ch.isprintable() and ch not in {"\n", "\r", "\t", "\x0b", "\x0c"}


def _runtime_char_entry(key: str | int) -> tuple[str, int, str] | None:
    """
    Return (char, codepoint, key_string) for runtime-usable single-character glyphs.
    Supports:
    - int keys like 0x41
    - single-character string keys
    Excludes ligatures and .notdef.
    """
    if isinstance(key, int):
        try:
            ch = chr(key)
        except ValueError:
            return None
        if not _is_printable_char(ch):
            return None
        return ch, key, str(key)

    if key == ".notdef":
        return None

    if len(key) == 1 and _is_printable_char(key):
        return key, ord(key), key

    return None


def _group_runtime_chars(glyphs: OrderedDict[str | int, dict[str, Any]]) -> dict[str, Any]:
    entries: list[tuple[str, int, str]] = []

    for key in glyphs.keys():
        item = _runtime_char_entry(key)
        if item is not None:
            entries.append(item)

    # Stable and readable ordering by codepoint
    entries.sort(key=lambda t: t[1])

    upper: list[str] = []
    lower: list[str] = []
    digits: list[str] = []
    punct: list[str] = []
    other: list[str] = []

    codepoints: list[int] = []
    char_to_key: dict[str, str] = {}
    char_to_unicode: dict[str, str] = {}

    for ch, cp, key_str in entries:
        codepoints.append(cp)
        char_to_key[ch] = key_str
        char_to_unicode[ch] = f"U+{cp:04X}"

        if "A" <= ch <= "Z":
            upper.append(ch)
        elif "a" <= ch <= "z":
            lower.append(ch)
        elif "0" <= ch <= "9":
            digits.append(ch)
        elif ch.isprintable() and not ch.isalnum() and not ch.isspace():
            punct.append(ch)
        else:
            other.append(ch)

    all_chars = upper + lower + digits + punct + other

    return {
        "all": "".join(all_chars),
        "uppercase": "".join(upper),
        "lowercase": "".join(lower),
        "digits": "".join(digits),
        "punct": "".join(punct),
        "other": "".join(other),
        "codepoints": codepoints,
        "char_to_key": char_to_key,
        "char_to_unicode": char_to_unicode,
    }


def _codepoints_to_ranges(codepoints: list[int]) -> list[list[int]]:
    if not codepoints:
        return []

    cps = sorted(set(codepoints))
    ranges: list[list[int]] = []

    start = prev = cps[0]
    for cp in cps[1:]:
        if cp == prev + 1:
            prev = cp
            continue
        ranges.append([start, prev])
        start = prev = cp

    ranges.append([start, prev])
    return ranges


def _php_scalar(value: Any, indent: int = 0) -> str:
    pad = " " * indent
    next_pad = " " * (indent + 4)

    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace("\r", "\\r")
            .replace("\n", "\\n")
            .replace("\t", "\\t")
        )
        return f"'{escaped}'"

    if isinstance(value, list):
        if not value:
            return "[]"
        lines = ["["]
        for item in value:
            lines.append(f"{next_pad}{_php_scalar(item, indent + 4)},")
        lines.append(f"{pad}]")
        return "\n".join(lines)

    if isinstance(value, dict):
        if not value:
            return "[]"
        lines = ["["]
        for k, v in value.items():
            lines.append(f"{next_pad}{_php_scalar(str(k))} => {_php_scalar(v, indent + 4)},")
        lines.append(f"{pad}]")
        return "\n".join(lines)

    raise TypeError(f"Unsupported value for PHP export: {type(value)!r}")


def build_cake_config_payload(glyphs: OrderedDict[str | int, dict[str, Any]]) -> dict[str, Any]:
    runtime = _group_runtime_chars(glyphs)
    codepoints = runtime["codepoints"]
    unicode_ranges = _codepoints_to_ranges(codepoints)

    ligatures: OrderedDict[str, dict[str, Any]] = OrderedDict()
    named_glyph_keys: list[str] = []

    for key, glyph in glyphs.items():
        if isinstance(key, str):
            if glyph.get("is_ligature") is True:
                ligatures[key] = {
                    "sequence": key,
                    "chars": glyph.get("sequence_chars") or [],
                    "codepoints": glyph.get("sequence_codepoints") or [],
                    "unicode": glyph.get("sequence_unicode") or [],
                }
            elif key != ".notdef":
                named_glyph_keys.append(key)

    return {
        "Defont": {
            "glyphCount": len(glyphs),
            "chars": runtime["all"],
            "uppercase": runtime["uppercase"],
            "lowercase": runtime["lowercase"],
            "digits": runtime["digits"],
            "punct": runtime["punct"],
            "other": runtime["other"],
            "codepoints": codepoints,
            "unicodeRanges": unicode_ranges,
            "charToKey": runtime["char_to_key"],
            "charToUnicode": runtime["char_to_unicode"],
            "ligatureKeys": list(ligatures.keys()),
            "ligatures": ligatures,
            "namedGlyphKeys": named_glyph_keys,
            "hasUppercase": bool(runtime["uppercase"]),
            "hasLowercase": bool(runtime["lowercase"]),
            "hasDigits": bool(runtime["digits"]),
            "hasPunct": bool(runtime["punct"]),
        }
    }


def build_payload(glyphs: OrderedDict[str | int, dict[str, Any]], source_path: Path) -> dict[str, Any]:
    glyphs_by_key: OrderedDict[str, dict[str, Any]] = OrderedDict()
    keys_in_order: list[str] = []
    ligature_keys: list[str] = []

    for k, v in glyphs.items():
        sk = str(k) if isinstance(k, str) else str(int(k))
        glyphs_by_key[sk] = v
        keys_in_order.append(sk)
        if v.get("is_ligature") is True:
            ligature_keys.append(sk)

    widths = sorted({g["width"] for g in glyphs.values() if g["width"] is not None})
    heights = sorted({g["height"] for g in glyphs.values() if g["height"] is not None})

    return {
        "format": "deidee-chars",
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source_path.as_posix()),
        "meta": {
            "glyph_count": len(glyphs),
            "keys_in_order": keys_in_order,
            "ligature_keys": ligature_keys,
            "distinct_widths": widths,
            "distinct_heights": heights,
            "notes": [
                "glyph rows are row-major (top-to-bottom), matching the PHP arrays",
                "empty glyphs (e.g. space) have width/height = null because the PHP source stores no dimensions",
                "JSON object keys are strings; codepoint glyphs also include numeric 'codepoint'",
                "ligatures are detected as string keys with len(key) > 1 (excluding '.notdef')",
            ],
        },
        "glyphs": glyphs_by_key,
    }


def write_json(payload: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_python_module(payload: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# Auto-generated by tools/generate-data.py\n"
        "# Do not edit by hand.\n"
        "from __future__ import annotations\n\n"
        "CHARS_DATA = "
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n"
    )
    out_path.write_text(content, encoding="utf-8")


def write_js_module(payload: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "// Auto-generated by tools/generate-data.py\n"
        "// Do not edit by hand.\n\n"
        "export const charsData = "
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + ";\n\n"
        "export default charsData;\n"
    )
    out_path.write_text(content, encoding="utf-8")


def write_cake_config(payload: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    content = (
        "<?php\n"
        "declare(strict_types=1);\n\n"
        "// Auto-generated by tools/generate-data.py\n"
        "// Runtime-friendly config for CakePHP/plugin usage.\n\n"
        "return "
        + _php_scalar(payload, 0)
        + ";\n"
    )
    out_path.write_text(content, encoding="utf-8")


def main() -> int:
    root = Path(__file__).resolve().parents[1]

    parser = argparse.ArgumentParser(
        description="Parse data/chars.php and export glyph data to JSON/Python/JS/CakePHP config."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=root / "data" / "chars.php",
        help="Input PHP file (default: %(default)s)",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=root / "data" / "chars.json",
        help="Output JSON path (default: %(default)s)",
    )
    parser.add_argument(
        "--py-out",
        type=Path,
        default=root / "data" / "chars.py",
        help="Output Python module path (default: %(default)s)",
    )
    parser.add_argument(
        "--js-out",
        type=Path,
        default=root / "data" / "chars.mjs",
        help="Output JavaScript module path (default: %(default)s)",
    )
    parser.add_argument(
        "--cake-config-out",
        type=Path,
        default=root / "config" / "defont.php",
        help="Output CakePHP config path (default: %(default)s)",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Only write JSON (skip .py and .mjs exports)",
    )
    parser.add_argument(
        "--no-cake-config",
        action="store_true",
        help="Do not write CakePHP config export",
    )
    args = parser.parse_args()

    php_text = args.input.read_text(encoding="utf-8")
    glyphs = parse_php_chars(php_text)
    payload = build_payload(glyphs, args.input)

    write_json(payload, args.json_out)
    if not args.json_only:
        write_python_module(payload, args.py_out)
        write_js_module(payload, args.js_out)

    if not args.no_cake_config:
        cake_payload = build_cake_config_payload(glyphs)
        write_cake_config(cake_payload, args.cake_config_out)

    print(f"Parsed {len(glyphs)} glyphs from {args.input}")
    print(f"Wrote {args.json_out}")
    if not args.json_only:
        print(f"Wrote {args.py_out}")
        print(f"Wrote {args.js_out}")
    if not args.no_cake_config:
        print(f"Wrote {args.cake_config_out}")

    ligs = payload["meta"]["ligature_keys"]
    if ligs:
        print(f"Ligatures: {len(ligs)} ({', '.join(ligs[:10])}{'…' if len(ligs) > 10 else ''})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())