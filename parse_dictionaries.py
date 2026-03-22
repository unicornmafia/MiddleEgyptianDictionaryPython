#!/usr/bin/env python3
"""
Parse Vygus 2018, Dickson 2006, and Faulkner 1991 Middle Egyptian dictionary PDFs
and import a unified dataset into MongoDB (matching the app's schema).

Also parses supplementary .hwd / .hrw / .csv vocabulary files from a dictionary
directory and merges them into the unified dataset.

Usage:
    .venv/bin/python3 parse_dictionaries.py [--dry-run] [--out entries.json]
    .venv/bin/python3 parse_dictionaries.py --dict-dir ./dictionaries --from-json entries.json

Environment variables:
    MONGO_URI       (default: mongodb://localhost:27017)
    MONGO_DB        (default: MiddleEgyptianDictionary)
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict

import pdfplumber
from pymongo import MongoClient, UpdateOne

# ---------------------------------------------------------------------------
# DataSource enum values matching the app
# ---------------------------------------------------------------------------
DICKSON          = 0
VYGUS            = 1
FAULKNER         = 4
COLLIER_MANLEY   = 5
ALLEN            = 6
HOCH             = 7
KAMRIN           = 8
GARDINER_GRAMMAR = 9
EVANS            = 10

PDFS_DIR = os.path.join(os.path.dirname(__file__), "pdfs")

# ---------------------------------------------------------------------------
# Gardiner code regex
# Matches codes like A1, A10, A10A, Aa1, Aa18, Z1, Z2 …
# ---------------------------------------------------------------------------
_GARD_TOKEN = r'[A-Z][a-z]?\d+[A-Za-z]?'
_GARD_SEQ_DASH   = re.compile(
    r'(?:' + _GARD_TOKEN + r')(?:\s*-\s*(?:' + _GARD_TOKEN + r'))*$'
)
_GARD_SEQ_SPACE  = re.compile(
    r'\{((?:' + _GARD_TOKEN + r'\s*)+)\}'
)

# ---------------------------------------------------------------------------
# VYGUS 2018 parser
# ---------------------------------------------------------------------------
# Line format (typical):
#   <translit> <gloss> [ <pos> ] { <domain> } GARD - GARD - GARD
# Gardiner codes are at the END, separated by " - "

_VYGUS_POS    = re.compile(r'\[([^\]]+)\]')
_VYGUS_DOMAIN = re.compile(r'\{([^}]+)\}')


def _parse_vygus_line(line: str) -> dict | None:
    line = line.strip()
    if not line or line.isdigit():
        return None

    # Find trailing Gardiner codes: "WORD - WORD - WORD" at end of line
    gm = _GARD_SEQ_DASH.search(line)
    if not gm:
        return None

    gardiner_raw = gm.group(0)
    gardiner_signs = " ".join(p.strip() for p in gardiner_raw.split("-"))
    rest = line[: gm.start()].strip()

    # Extract domain { ... } and POS [ ... ]
    domain = None
    dm = _VYGUS_DOMAIN.search(rest)
    if dm:
        domain = dm.group(1).strip()
        rest = (rest[: dm.start()] + rest[dm.end() :]).strip()

    pos = None
    pm = _VYGUS_POS.search(rest)
    if pm:
        pos = pm.group(1).strip()
        rest = (rest[: pm.start()] + rest[pm.end() :]).strip()

    # First whitespace-delimited token is transliteration; rest is gloss
    parts = rest.split(None, 1)
    if not parts:
        return None

    translit = parts[0]
    gloss    = parts[1].strip() if len(parts) > 1 else ""

    if not translit or not gloss:
        return None

    return {
        "transliteration": translit,
        "gardiner_signs":  gardiner_signs,
        "translation":     gloss,
        "pos":             pos,
        "source":          VYGUS,
    }


def parse_vygus(pdf_path: str) -> list[dict]:
    entries = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue
            for line in text.splitlines():
                e = _parse_vygus_line(line)
                if e:
                    entries.append(e)
    print(f"  Vygus: parsed {len(entries)} entries", file=sys.stderr)
    return entries


# ---------------------------------------------------------------------------
# DICKSON 2006 parser
# ---------------------------------------------------------------------------
# Line format:
#   [<translit>] <gloss> {<GARD> <GARD> ...}

_DICKSON_ENTRY = re.compile(
    r'^\[([^\]]+)\]\s+(.+?)\s+\{([^}]+)\}\s*$'
)


def parse_dickson(pdf_path: str) -> list[dict]:
    entries = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue
            for line in text.splitlines():
                line = line.strip()
                m = _DICKSON_ENTRY.match(line)
                if not m:
                    continue
                translit = m.group(1).strip()
                gloss    = m.group(2).strip()
                gard_raw = m.group(3).strip()
                # codes are space-separated inside { }
                gardiner_signs = " ".join(gard_raw.split())
                if translit and gloss:
                    entries.append({
                        "transliteration": translit,
                        "gardiner_signs":  gardiner_signs,
                        "translation":     gloss,
                        "pos":             None,
                        "source":          DICKSON,
                    })
    print(f"  Dickson: parsed {len(entries)} entries", file=sys.stderr)
    return entries


# ---------------------------------------------------------------------------
# FAULKNER 1991 (Jegorović 2017 digitization) parser
# ---------------------------------------------------------------------------
# Entries live on pages 17–419.
# Each entry line looks like:
#   - <transliteration> - <definition text ...>
# Definitions may span multiple lines; the next "- <word> -" signals a new entry.
#
# No Gardiner codes are present; page/index within page are stored so the
# app's Faulkner browser (which queries by page number) still works.

_FAULK_ENTRY_START = re.compile(r'^\s*[-–]\s+(\S+)\s+[-–]\s+(.*)')
_FAULK_PAGE_HEADER = re.compile(r'^\s*\d+\s*$')   # bare page numbers


def _faulkner_pages(pdf) -> tuple[int, int]:
    """Return (start_page_index, end_page_index) for the dictionary body."""
    # The body starts at PDF page where text matches page 17 entries
    # We'll scan forward for the first page containing "- A -" style entries
    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        if _FAULK_ENTRY_START.search(text):
            return i, len(pdf.pages) - 1
    return 0, len(pdf.pages) - 1


def parse_faulkner(pdf_path: str) -> list[dict]:
    entries       = []
    current_page  = 17   # Faulkner body starts at page 17 per the app
    page_index    = 0    # index within current_page

    cur_translit  = None
    cur_lines     = []

    def _flush():
        nonlocal cur_translit, cur_lines, page_index
        if cur_translit:
            definition = " ".join(cur_lines).strip()
            if definition:
                entries.append({
                    "transliteration": cur_translit,
                    "gardiner_signs":  "",
                    "translation":     definition,
                    "pos":             None,
                    "source":          FAULKNER,
                    "page":            current_page,
                    "index_on_page":   page_index,
                })
                page_index += 1
        cur_translit = None
        cur_lines    = []

    with pdfplumber.open(pdf_path) as pdf:
        start_idx, end_idx = _faulkner_pages(pdf)
        for pdf_page in pdf.pages[start_idx: end_idx + 1]:
            text = pdf_page.extract_text()
            if not text:
                continue

            lines = text.splitlines()
            # Detect page breaks by checking for a bare number at top of page text
            # In the digitized Faulkner, the printed page number appears as a
            # standalone line (often the first line).
            for i, line in enumerate(lines):
                # Detect a page-number line (one or two digit-only tokens)
                stripped = line.strip()
                if re.match(r'^\d{1,3}$', stripped):
                    # This is likely the printed page number
                    detected = int(stripped)
                    if 17 <= detected <= 419:
                        _flush()
                        current_page = detected
                        page_index   = 0
                    continue

                m = _FAULK_ENTRY_START.match(line)
                if m:
                    _flush()
                    cur_translit = m.group(1)
                    cur_lines    = [m.group(2).strip()]
                elif cur_translit:
                    # Continuation of previous entry
                    cur_lines.append(stripped)
        _flush()

    print(f"  Faulkner: parsed {len(entries)} entries", file=sys.stderr)
    return entries


# ---------------------------------------------------------------------------
# Vocabulary file parser (.hwd / .hrw / .csv)
# ---------------------------------------------------------------------------
# All three formats share the same CSV layout:
#   Row 0 (header): source_name, author, date, email, desc1, desc2, desc3
#   Data rows:      type, category, glyph_positions, gardiner_codes,
#                   transliteration, translation, notes
# Gardiner codes in field 3 are separated by " - "; we normalise to space-sep.

_DICT_DIR = os.path.join(os.path.dirname(__file__), "dictionaries")
_POS_BRACKET = re.compile(r'\[\s*([^\]]+?)\s*\]')


def _map_source_name(name: str) -> int:
    """Map a human-readable source name from a file header to a DictionaryName int."""
    n = name.lower()
    if "vygus" in n or "hieroglyph dictionary" in n:
        return VYGUS
    if "collier" in n or "manley" in n:
        return COLLIER_MANLEY
    if "allen" in n:
        return ALLEN
    if "hoch" in n:
        return HOCH
    if "kamrin" in n or "chapter" in n:
        return KAMRIN
    if "gardiner" in n:
        return GARDINER_GRAMMAR
    # Biliterals, Triliterals, Kate Evans
    return EVANS


def _normalise_gardiner_codes(raw: str) -> str:
    """Convert ' - '-separated code string to space-separated, trimmed."""
    # Split on ' - ' or '-' surrounded by optional spaces, then rejoin
    parts = re.split(r'\s*-\s*', raw.strip())
    return " ".join(p.strip() for p in parts if p.strip())


def _parse_vocab_file(filepath: str) -> list[dict]:
    """Parse a single .hwd / .hrw / .csv vocabulary file."""
    entries = []
    try:
        with open(filepath, newline="", encoding="utf-8", errors="replace") as fh:
            rows = list(csv.reader(fh))
    except OSError as e:
        print(f"  Warning: could not read {filepath}: {e}", file=sys.stderr)
        return entries

    if not rows:
        return entries

    # Row 0 is the header; determine source name from first field
    source_id = _map_source_name(rows[0][0] if rows[0] else "")

    for row in rows[1:]:
        if len(row) < 6:
            continue
        # field indices: 0=type, 1=category, 2=glyph_positions, 3=gardiner_codes,
        #                4=transliteration, 5=translation, 6=notes (optional)
        gardiner_raw  = row[3].strip()
        transliteration = row[4].strip()
        translation     = row[5].strip()
        notes           = row[6].strip() if len(row) > 6 else ""

        if not transliteration or not translation:
            continue

        gardiner = _normalise_gardiner_codes(gardiner_raw)

        # Extract part-of-speech from notes: [ noun ], [ verb ], etc.
        pos = None
        pm = _POS_BRACKET.search(notes)
        if pm:
            pos = pm.group(1).strip()

        entries.append({
            "transliteration": transliteration,
            "gardiner_signs":  gardiner,
            "translation":     translation,
            "pos":             pos,
            "source":          source_id,
        })

    return entries


def parse_all_vocab_files(directory: str) -> list[dict]:
    """
    Walk a directory tree and parse all .hwd, .hrw, and .csv files.
    Gardiner individual lesson files (Gardiner2.csv … Gardiner33.csv) are
    skipped in favour of Gardinerall.csv which contains the same content.
    """
    _GARDINER_LESSON_RE = re.compile(r'^Gardiner\d+\.csv$', re.IGNORECASE)

    all_entries = []
    for root, _dirs, files in os.walk(directory):
        for fname in sorted(files):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in (".hwd", ".hrw", ".csv"):
                continue
            # Skip individual Gardiner lesson files — Gardinerall.csv covers them
            if _GARDINER_LESSON_RE.match(fname):
                continue
            fpath = os.path.join(root, fname)
            parsed = _parse_vocab_file(fpath)
            print(f"  {fname}: {len(parsed)} entries (source={parsed[0]['source'] if parsed else '?'})",
                  file=sys.stderr)
            all_entries.extend(parsed)

    print(f"  Vocab files total: {len(all_entries)} entries", file=sys.stderr)
    return all_entries


# ---------------------------------------------------------------------------
# Merge into unified DictionaryEntry documents
# ---------------------------------------------------------------------------
# Entries from Vygus/Dickson are keyed by (transliteration, gardiner_signs).
# Faulkner entries are keyed by transliteration only (no Gardiner), and are
# merged into an existing Vygus/Dickson entry when there is an exact
# transliteration match; otherwise they stand alone.


def _normalise_gard(gs: str) -> str:
    """Normalise Gardiner sign string for merging."""
    return " ".join(gs.upper().split())


def _normalise_translit(t: str) -> str:
    return t.strip()


def merge_entries(vygus: list, dickson: list, faulkner: list, vocab: list | None = None) -> list[dict]:
    """
    Produce a list of DictionaryEntry dicts matching the app's MongoDB schema.

    Schema:
    {
      "Transliteration": str,
      "GardinerSigns":   str,   # space-separated
      "Res":             None,
      "ManuelDeCodage":  None,
      "Translations": [
        {
          "translation": str,
          "TranslationMetadata": [
            {
              "DictionaryName": int,   # DICKSON/VYGUS/FAULKNER
              "PartOfSpeech":   str | None,
              "Page":           int | None,
              "IndexOnPage":    int | None,
            }
          ]
        }
      ]
    }
    """
    # Key: (translit_norm, gardiner_norm) → entry dict
    merged: dict[tuple, dict] = {}

    def _get_or_create(translit: str, gardiner: str) -> dict:
        key = (_normalise_translit(translit), _normalise_gard(gardiner))
        if key not in merged:
            merged[key] = {
                "Transliteration": translit,
                "GardinerSigns":   _normalise_gard(gardiner),
                "Res":             None,
                "ManuelDeCodage":  None,
                "Translations":    [],
            }
        return merged[key]

    def _add_translation(entry: dict, translation: str, source: int,
                         pos: str | None, page: int | None, idx: int | None):
        # Check if this exact translation text already exists from this source
        for t in entry["Translations"]:
            if t["translation"] == translation:
                # Append metadata for additional source
                for m in t["TranslationMetadata"]:
                    if m["DictionaryName"] == source:
                        return  # already recorded
                t["TranslationMetadata"].append({
                    "DictionaryName": source,
                    "PartOfSpeech":   pos,
                    "Page":           page,
                    "IndexOnPage":    idx,
                })
                return
        entry["Translations"].append({
            "translation": translation,
            "TranslationMetadata": [{
                "DictionaryName": source,
                "PartOfSpeech":   pos,
                "Page":           page,
                "IndexOnPage":    idx,
            }],
        })

    # --- Vygus ---
    for e in vygus:
        entry = _get_or_create(e["transliteration"], e["gardiner_signs"])
        _add_translation(entry, e["translation"], VYGUS, e["pos"], None, None)

    # --- Dickson ---
    for e in dickson:
        entry = _get_or_create(e["transliteration"], e["gardiner_signs"])
        _add_translation(entry, e["translation"], DICKSON, e["pos"], None, None)

    # --- Faulkner: try to match by transliteration to existing entry ---
    # Build an index of transliterations already present
    translit_index: dict[str, list[dict]] = defaultdict(list)
    for entry in merged.values():
        translit_index[_normalise_translit(entry["Transliteration"])].append(entry)

    for e in faulkner:
        norm = _normalise_translit(e["transliteration"])
        if norm in translit_index:
            # Attach to first matching entry (closest match)
            target = translit_index[norm][0]
        else:
            # Create standalone Faulkner-only entry (no Gardiner signs)
            target = _get_or_create(e["transliteration"], "")
            translit_index[norm].append(target)

        _add_translation(
            target,
            e["translation"],
            FAULKNER,
            e["pos"],
            e["page"],
            e["index_on_page"],
        )

    # --- Vocabulary files (Allen, Collier&Manley, Hoch, Kamrin, etc.) ---
    if vocab:
        for e in vocab:
            entry = _get_or_create(e["transliteration"], e["gardiner_signs"])
            _add_translation(entry, e["translation"], e["source"], e["pos"], None, None)

    result = list(merged.values())
    print(f"  Merged: {len(result)} unique entries", file=sys.stderr)
    return result


def merge_vocab_into_existing(existing: list[dict], vocab: list[dict]) -> list[dict]:
    """
    Merge vocabulary file entries into an already-built entries list (e.g. loaded
    from entries.json).  Returns the updated list.
    """
    # Build lookup index from existing entries
    index: dict[tuple, dict] = {}
    for entry in existing:
        key = (_normalise_translit(entry["Transliteration"]),
               _normalise_gard(entry.get("GardinerSigns", "")))
        index[key] = entry

    def _add(entry: dict, translation: str, source: int, pos, page, idx):
        for t in entry["Translations"]:
            if t["translation"] == translation:
                for m in t["TranslationMetadata"]:
                    if m["DictionaryName"] == source:
                        return
                t["TranslationMetadata"].append({
                    "DictionaryName": source,
                    "PartOfSpeech":   pos,
                    "Page":           page,
                    "IndexOnPage":    idx,
                })
                return
        entry["Translations"].append({
            "translation": translation,
            "TranslationMetadata": [{
                "DictionaryName": source,
                "PartOfSpeech":   pos,
                "Page":           page,
                "IndexOnPage":    idx,
            }],
        })

    new_entries = 0
    for e in vocab:
        key = (_normalise_translit(e["transliteration"]),
               _normalise_gard(e["gardiner_signs"]))
        if key in index:
            _add(index[key], e["translation"], e["source"], e["pos"], None, None)
        else:
            new_entry = {
                "Transliteration": e["transliteration"],
                "GardinerSigns":   _normalise_gard(e["gardiner_signs"]),
                "Res":             None,
                "ManuelDeCodage":  None,
                "Translations": [{
                    "translation": e["translation"],
                    "TranslationMetadata": [{
                        "DictionaryName": e["source"],
                        "PartOfSpeech":   e["pos"],
                        "Page":           None,
                        "IndexOnPage":    None,
                    }],
                }],
            }
            index[key] = new_entry
            existing.append(new_entry)
            new_entries += 1

    print(f"  Vocab merge: {new_entries} new entries added, "
          f"{len(vocab) - new_entries} translations merged into existing entries",
          file=sys.stderr)
    return existing


# ---------------------------------------------------------------------------
# Keyword index builder
# ---------------------------------------------------------------------------

_STOP_WORDS = {
    "a", "an", "the", "of", "to", "in", "is", "it", "or", "and", "for",
    "with", "as", "at", "by", "on", "be", "up", "do", "so", "if", "no",
    "not", "but", "are", "was", "has", "had", "its", "one", "two",
}


def _keywords_from(translation: str) -> list[str]:
    words = re.split(r"\W+", translation.lower())
    return [w for w in words if w and w not in _STOP_WORDS and len(w) > 1]


def build_keyword_index(entries: list[dict], id_field: str = "_id") -> dict[str, list]:
    """Return {keyword: [entry_id, ...]} mapping."""
    index: dict[str, set] = defaultdict(set)
    for entry in entries:
        eid = entry[id_field]
        for t in entry.get("Translations", []):
            for kw in _keywords_from(t.get("translation", "")):
                index[kw].add(eid)
    return {kw: list(ids) for kw, ids in index.items()}


# ---------------------------------------------------------------------------
# MongoDB import
# ---------------------------------------------------------------------------

def import_to_mongo(entries: list[dict], uri: str, db_name: str):
    from bson import ObjectId

    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    db     = client[db_name]

    entries_col   = db["DictionaryEntry"]
    keywords_col  = db["KeywordSearch"]

    print(f"  Dropping existing collections…", file=sys.stderr)
    entries_col.drop()
    keywords_col.drop()

    # Assign ObjectIds
    for e in entries:
        e["_id"] = ObjectId()

    print(f"  Inserting {len(entries)} entries…", file=sys.stderr)
    entries_col.insert_many(entries)

    # Build and insert keyword index
    kw_index = build_keyword_index(entries)
    print(f"  Building keyword index ({len(kw_index)} keywords)…", file=sys.stderr)

    kw_docs = [
        {"Keyword": kw, "EntryIds": ids}
        for kw, ids in kw_index.items()
    ]
    if kw_docs:
        keywords_col.insert_many(kw_docs)

    # Create indexes for fast search
    entries_col.create_index("Transliteration")
    entries_col.create_index("GardinerSigns")
    keywords_col.create_index("Keyword")

    print(f"  Done. Inserted {len(entries)} entries, {len(kw_docs)} keywords.",
          file=sys.stderr)
    client.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Parse ME dictionaries and load to MongoDB")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse only; do not write to MongoDB")
    parser.add_argument("--out", default="entries.json",
                        help="JSON output file (default: entries.json)")
    parser.add_argument("--from-json", metavar="FILE",
                        help="Skip PDF parsing; load base entries from an existing JSON file")
    parser.add_argument("--dict-dir", metavar="DIR", default=None,
                        help="Directory of .hwd/.hrw/.csv vocab files to merge in "
                             "(default: ./dictionaries if it exists)")
    parser.add_argument("--skip-vygus",    action="store_true")
    parser.add_argument("--skip-dickson",  action="store_true")
    parser.add_argument("--skip-faulkner", action="store_true")
    args = parser.parse_args()

    uri     = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
    db_name = os.environ.get("MONGO_DB",  "MiddleEgyptianDictionary")

    if args.from_json:
        print(f"Loading entries from {args.from_json}…", file=sys.stderr)
        with open(args.from_json, encoding="utf-8") as f:
            entries = json.load(f)
        print(f"  Loaded {len(entries)} entries.", file=sys.stderr)
    else:
        vygus_path    = os.path.join(PDFS_DIR, "vygus_2018.pdf")
        dickson_path  = os.path.join(PDFS_DIR, "dickson_2006.pdf")
        faulkner_path = os.path.join(PDFS_DIR, "faulkner_1991.pdf")

        print("Parsing dictionaries…", file=sys.stderr)

        vygus_entries    = parse_vygus(vygus_path)       if not args.skip_vygus    else []
        dickson_entries  = parse_dickson(dickson_path)   if not args.skip_dickson  else []
        faulkner_entries = parse_faulkner(faulkner_path) if not args.skip_faulkner else []

        print("Merging…", file=sys.stderr)
        entries = merge_entries(vygus_entries, dickson_entries, faulkner_entries)

    # --- Vocabulary files ---
    dict_dir = args.dict_dir
    if dict_dir is None and os.path.isdir(_DICT_DIR):
        dict_dir = _DICT_DIR

    if dict_dir:
        print(f"Parsing vocabulary files in {dict_dir}…", file=sys.stderr)
        vocab = parse_all_vocab_files(dict_dir)
        if vocab:
            print("Merging vocabulary entries…", file=sys.stderr)
            entries = merge_vocab_into_existing(entries, vocab)

    # Write JSON output (without ObjectIds – they're added at import time)
    out_path = os.path.join(os.path.dirname(__file__), args.out)
    print(f"Writing {out_path}…", file=sys.stderr)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    print(f"  Wrote {len(entries)} entries to {args.out}", file=sys.stderr)

    if not args.dry_run:
        print(f"Importing to MongoDB {uri} / {db_name}…", file=sys.stderr)
        import_to_mongo(entries, uri, db_name)
    else:
        print("Dry run — skipping MongoDB import.", file=sys.stderr)


if __name__ == "__main__":
    main()
