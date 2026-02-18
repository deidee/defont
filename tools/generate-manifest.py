#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/generate-manifest.py

Generate a universal, JSON font manifest from dist/fonts/{name}.{ext} files.

Output (only):
  - data/manifest.json

Notes on naming:
- We canonicalize the manifest "name" to lowercase.
- We *prefer* lowercase font filenames (e.g. dist/fonts/mmxx.woff2).
- If files exist with different casing, we still find them (case-insensitive) and:
  - record the actual filename in files[].file (so it remains correct)
  - record the recommended lowercase filename in files[].recommendedFile
  - emit a warning on stdout

Usage:
  # simplest: set DEFAULT_NAME below, then run:
  python tools/generate-manifest.py

  # or override:
  python tools/generate-manifest.py --name mmxx
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set


from fontTools.ttLib import TTFont


# =========================
# Config
# =========================
DEFAULT_NAME = "defont"  # <-- set your default font base-name here (WITHOUT extension), lowercase recommended

EXTS = ["woff2", "woff", "ttf", "otf"]  # look for these in dist/fonts
PREFERRED_PARSE_ORDER = ["ttf", "otf", "woff2", "woff"]  # for metadata extraction


@dataclass
class FontFileInfo:
    path: Path
    ext: str
    size: int
    sha256: str
    parse_error: Optional[str] = None
    recommended_file: Optional[str] = None  # lowercase recommendation (relative to dist/fonts)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def compress_to_ranges(codepoints: List[int]) -> List[List[int]]:
    """Compress sorted codepoints into inclusive [start, end] ranges."""
    if not codepoints:
        return []
    cps = sorted(set(codepoints))
    ranges: List[List[int]] = []
    start = prev = cps[0]
    for cp in cps[1:]:
        if cp == prev + 1:
            prev = cp
            continue
        ranges.append([start, prev])
        start = prev = cp
    ranges.append([start, prev])
    return ranges


def safe_get_name(tt: TTFont, name_id: int) -> Optional[str]:
    """Best-effort string from the 'name' table."""
    if "name" not in tt:
        return None

    name_tbl = tt["name"]

    # Try Windows Unicode English (US)
    n = name_tbl.getName(name_id, 3, 1, 0x0409)
    if n:
        try:
            return n.toUnicode()
        except Exception:
            try:
                return str(n)
            except Exception:
                pass

    # Fallback: first record with this nameID
    for rec in name_tbl.names:
        if rec.nameID == name_id:
            try:
                return rec.toUnicode()
            except Exception:
                try:
                    return str(rec)
                except Exception:
                    continue
    return None


def extract_feature_tags(tt: TTFont, table_tag: str) -> List[str]:
    """Extract Feature tags from GSUB/GPOS."""
    if table_tag not in tt:
        return []
    try:
        table = tt[table_tag].table
        fl = getattr(table, "FeatureList", None)
        if not fl or not getattr(fl, "FeatureRecord", None):
            return []
        tags = [
            fr.FeatureTag
            for fr in fl.FeatureRecord
            if getattr(fr, "FeatureTag", None)
        ]
        return sorted(set(tags))
    except Exception:
        return []


def parse_font(path: Path) -> TTFont:
    return TTFont(
        str(path),
        recalcBBoxes=False,
        recalcTimestamp=False,
        lazy=True,
    )


def _case_insensitive_lookup(dist_fonts_dir: Path, want_filename: str) -> Optional[Path]:
    """
    Find a file in dist_fonts_dir by case-insensitive name match.
    Returns the actual path if found, else None.
    """
    want_lc = want_filename.lower()
    if not dist_fonts_dir.exists():
        return None
    for p in dist_fonts_dir.iterdir():
        if p.is_file() and p.name.lower() == want_lc:
            return p
    return None


def find_font_files(dist_fonts_dir: Path, name_lc: str) -> List[FontFileInfo]:
    """
    Prefer exact lowercase match {name_lc}.{ext}.
    If not found, try case-insensitive lookup and record recommendations.
    """
    found: List[FontFileInfo] = []

    for ext in EXTS:
        expected = f"{name_lc}.{ext}"
        expected_path = dist_fonts_dir / expected

        actual_path: Optional[Path] = None
        if expected_path.exists() and expected_path.is_file():
            actual_path = expected_path
        else:
            actual_path = _case_insensitive_lookup(dist_fonts_dir, expected)

        if actual_path and actual_path.exists() and actual_path.is_file():
            size = actual_path.stat().st_size
            digest = sha256_file(actual_path)

            info = FontFileInfo(
                path=actual_path,
                ext=ext,
                size=size,
                sha256=digest,
                recommended_file=expected,  # always lowercase recommendation
            )
            found.append(info)

    return found


def choose_primary_parse_file(files: List[FontFileInfo]) -> Optional[FontFileInfo]:
    by_ext = {f.ext: f for f in files}
    for ext in PREFERRED_PARSE_ORDER:
        if ext in by_ext:
            return by_ext[ext]
    return files[0] if files else None


def _range_intersects(ranges: List[List[int]], lo: int, hi: int) -> bool:
    # ranges are inclusive [a,b] and sorted
    for a, b in ranges:
        if b < lo:
            continue
        if a > hi:
            return False
        return True
    return False


def build_manifest(name_lc: str, files: List[FontFileInfo], repo_root: Path) -> Dict:
    cmap_codepoints: Set[int] = set()
    gsub_tags: Set[str] = set()
    gpos_tags: Set[str] = set()

    primary_info = choose_primary_parse_file(files)

    meta: Dict[str, Optional[object]] = {
        "family": None,
        "subfamily": None,
        "fullName": None,
        "postScriptName": None,
        "unitsPerEm": None,
        "glyphCount": None,
    }

    # Parse primary first for metadata + initial coverage/features
    if primary_info:
        try:
            tt = parse_font(primary_info.path)

            meta["family"] = safe_get_name(tt, 1)          # Font Family
            meta["subfamily"] = safe_get_name(tt, 2)       # Subfamily
            meta["fullName"] = safe_get_name(tt, 4)        # Full name
            meta["postScriptName"] = safe_get_name(tt, 6)  # PostScript name

            try:
                meta["unitsPerEm"] = int(tt["head"].unitsPerEm) if "head" in tt else None
            except Exception:
                meta["unitsPerEm"] = None

            try:
                meta["glyphCount"] = len(tt.getGlyphOrder())
            except Exception:
                meta["glyphCount"] = None

            if "cmap" in tt:
                best = tt["cmap"].getBestCmap() or {}
                cmap_codepoints.update(best.keys())

            gsub_tags.update(extract_feature_tags(tt, "GSUB"))
            gpos_tags.update(extract_feature_tags(tt, "GPOS"))

            tt.close()
        except Exception as e:
            primary_info.parse_error = f"{type(e).__name__}: {e}"

    # Parse the rest for union coverage/features (best-effort)
    for info in files:
        if primary_info and info.path == primary_info.path:
            continue
        try:
            tt = parse_font(info.path)

            if "cmap" in tt:
                best = tt["cmap"].getBestCmap() or {}
                cmap_codepoints.update(best.keys())

            gsub_tags.update(extract_feature_tags(tt, "GSUB"))
            gpos_tags.update(extract_feature_tags(tt, "GPOS"))

            tt.close()
        except Exception as e:
            info.parse_error = f"{type(e).__name__}: {e}"

    ranges = compress_to_ranges(sorted(cmap_codepoints))

    total_codepoints = len(cmap_codepoints)
    bmp_codepoints = sum(1 for cp in cmap_codepoints if 0x0000 <= cp <= 0xFFFF)
    astral_codepoints = total_codepoints - bmp_codepoints

    common_blocks = {
        "basicLatin": _range_intersects(ranges, 0x0020, 0x007E),
        "latin1Supplement": _range_intersects(ranges, 0x00A0, 0x00FF),
        "latinExtendedA": _range_intersects(ranges, 0x0100, 0x017F),
        "latinExtendedB": _range_intersects(ranges, 0x0180, 0x024F),
    }

    dist_fonts_dir = repo_root / "dist" / "fonts"
    file_entries = []
    # Keep predictable order by EXTS
    for info in sorted(files, key=lambda x: EXTS.index(x.ext)):
        # actual file relative to dist/fonts (correct path)
        try:
            rel_actual = info.path.relative_to(dist_fonts_dir).as_posix()
        except Exception:
            rel_actual = info.path.name

        entry = {
            "ext": info.ext,
            "file": rel_actual,
            "recommendedFile": info.recommended_file or f"{name_lc}.{info.ext}",
            "bytes": info.size,
            "sha256": info.sha256,
        }
        if info.parse_error:
            entry["parseError"] = info.parse_error

        file_entries.append(entry)

    casing_mismatches = [
        e for e in file_entries
        if e.get("file", "").lower() != e.get("recommendedFile", "").lower()
        or e.get("file", "") != e.get("recommendedFile", "")
    ]

    return {
        "manifestVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "name": name_lc,  # canonical lowercase name
        "metadata": meta,
        "files": file_entries,
        "unicodeRanges": ranges,  # inclusive [start,end] codepoint ranges
        "counts": {
            "codepointsTotal": total_codepoints,
            "codepointsBMP": bmp_codepoints,
            "codepointsAstral": astral_codepoints,
        },
        "features": {
            "GSUB": sorted(gsub_tags),
            "GPOS": sorted(gpos_tags),
        },
        "hints": {
            "commonBlocks": common_blocks,
            "preferredLowercaseFilenames": True,
            "hasCasingMismatches": bool(casing_mismatches),
            "notes": [
                "unicodeRanges is derived from cmap.getBestCmap() across all parseable font files.",
                "GSUB/GPOS feature tags are extracted when those tables exist.",
                "Ligature contents are not enumerated here (by design); use shaping in your sample to demonstrate them.",
                "If files[].file differs from files[].recommendedFile, consider renaming to the recommended lowercase filenames for portability (CDN/Linux).",
            ],
        },
    }


def write_json(path: Path, obj: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate data/manifest.json for a font in dist/fonts.")
    ap.add_argument(
        "--name",
        default=DEFAULT_NAME,
        help=f"Base filename (without extension) in dist/fonts/ (default: {DEFAULT_NAME!r}).",
    )
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parents[1]  # tools/.. -> repo root
    dist_fonts_dir = repo_root / "dist" / "fonts"
    out_path = repo_root / "data" / "manifest.json"

    name_in = (args.name or "").strip()
    if not name_in:
        raise SystemExit("ERROR: name is empty. Set DEFAULT_NAME or pass --name.")

    name_lc = name_in.lower()
    if name_in != name_lc:
        print(f"Note: canonicalizing name to lowercase: {name_in!r} -> {name_lc!r}")

    files = find_font_files(dist_fonts_dir, name_lc)

    if not files:
        minimal = {
            "manifestVersion": 1,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "name": name_lc,
            "error": f"No font files found matching dist/fonts/{name_lc}.{{{','.join(EXTS)}}} (case-insensitive lookup attempted).",
            "files": [],
            "unicodeRanges": [],
            "counts": {"codepointsTotal": 0, "codepointsBMP": 0, "codepointsAstral": 0},
            "features": {"GSUB": [], "GPOS": []},
        }
        write_json(out_path, minimal)
        print(minimal["error"])
        print(f"Wrote: {out_path}")
        return 2

    manifest = build_manifest(name_lc, files, repo_root)
    write_json(out_path, manifest)

    print(f"Wrote: {out_path}")
    print("Found files:")
    for f in sorted(files, key=lambda x: EXTS.index(x.ext)):
        actual = f.path.name
        rec = f.recommended_file or actual.lower()
        if actual != rec:
            print(f"  - {actual}  (recommended: {rec})")
        else:
            print(f"  - {actual}")

    print(f"Codepoints: {manifest['counts']['codepointsTotal']}")
    if any(f.parse_error for f in files):
        print("Note: Some files could not be parsed; see files[].parseError in the manifest.")
    if manifest.get("hints", {}).get("hasCasingMismatches"):
        print("Warning: Some filenames differ from recommended lowercase names (see files[].recommendedFile).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
