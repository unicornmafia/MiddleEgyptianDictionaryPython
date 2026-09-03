#!/usr/bin/env python3
"""
Import OpenGlyp Lexicon.txt (DictionaryName=2) into MongoDB.

Merges into existing entries by (Transliteration, GardinerSigns) key.
Does not duplicate entries already tagged as DictionaryName=2.

Usage:
    ./venv/bin/python3 import_lexicon.py [--dry-run]
"""

import os
import sys
import argparse
from collections import defaultdict
from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.environ.get("MONGO_DB", "MiddleEgyptianDictionary")

LEXICON_PATH = os.path.join(os.path.dirname(__file__), "dictionaries", "Lexicon.txt")
LEXICON = 2


def _norm_signs(gs: str) -> str:
    return " ".join(gs.upper().split())


def parse_lexicon(path: str) -> list[dict]:
    entries = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            parts = line.split(";")
            if len(parts) < 3:
                continue
            sign_parts = [s.strip() for s in parts[0].split(",") if s.strip()]
            signs = _norm_signs(" ".join(sign_parts))
            translit = parts[1].strip().replace("=", ".")
            translation = parts[2].strip().replace("''", "'")
            if not translit or not translation:
                continue
            entries.append({
                "transliteration": translit,
                "gardiner_signs":  signs,
                "translation":     translation,
            })
    return entries


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not os.path.exists(LEXICON_PATH):
        print(f"ERROR: {LEXICON_PATH} not found", file=sys.stderr)
        sys.exit(1)

    print("Parsing Lexicon.txt…")
    lexicon_entries = parse_lexicon(LEXICON_PATH)
    print(f"  {len(lexicon_entries)} entries parsed")

    client = MongoClient(MONGO_URI)
    coll = client[MONGO_DB]["DictionaryEntry"]

    print("Loading existing DB entries…")
    all_db = list(coll.find({}, {"_id": 1, "Transliteration": 1,
                                  "GardinerSigns": 1, "Translations": 1}))
    print(f"  {len(all_db)} entries loaded")

    # Build index: (translit, norm_signs) → entry
    db_index = {}
    for e in all_db:
        key = (e["Transliteration"], _norm_signs(e.get("GardinerSigns", "")))
        db_index[key] = e

    # Plan operations
    to_merge = []   # (db_entry, translation_text) — add Lexicon metadata to existing translation
    to_add   = []   # (db_entry, new_translation_dict) — add new translation to existing entry
    to_create = []  # new_entry_dict — create entirely new entry

    merged_count = 0
    added_count = 0
    created_count = 0

    for le in lexicon_entries:
        key = (le["transliteration"], le["gardiner_signs"])
        translation = le["translation"]

        if key in db_index:
            db_entry = db_index[key]
            # Check if this translation text already exists
            existing_texts = {t["translation"]: t for t in db_entry.get("Translations", [])}

            if translation in existing_texts:
                # Check if Lexicon metadata already on it
                existing_t = existing_texts[translation]
                existing_names = {m["DictionaryName"]
                                  for m in existing_t.get("TranslationMetadata", [])}
                if LEXICON not in existing_names:
                    to_merge.append((db_entry["_id"], translation))
                # else: already imported, skip
            else:
                to_add.append((db_entry["_id"], {
                    "translation": translation,
                    "TranslationMetadata": [{
                        "DictionaryName": LEXICON,
                        "PartOfSpeech": None,
                        "Page": None,
                        "IndexOnPage": None,
                    }]
                }))
        else:
            to_create.append({
                "Transliteration": le["transliteration"],
                "GardinerSigns":   le["gardiner_signs"],
                "Res":             None,
                "ManuelDeCodage":  None,
                "Translations": [{
                    "translation": translation,
                    "TranslationMetadata": [{
                        "DictionaryName": LEXICON,
                        "PartOfSpeech": None,
                        "Page": None,
                        "IndexOnPage": None,
                    }]
                }]
            })

    print(f"\nPlan:")
    print(f"  Add Lexicon metadata to existing translations: {len(to_merge)}")
    print(f"  Add new translations to existing entries:      {len(to_add)}")
    print(f"  Create new entries:                            {len(to_create)}")
    total = len(to_merge) + len(to_add) + len(to_create)
    print(f"  Total operations: {total}")

    if args.dry_run:
        print("\nDRY RUN — not writing to DB")
        print("Sample new entries to create:")
        for e in to_create[:5]:
            print(f"  {e['Transliteration']!r} | {e['GardinerSigns']!r} | "
                  f"{e['Translations'][0]['translation'][:50]!r}")
        return

    # Execute
    errors = 0

    # 1. Add Lexicon metadata tag to existing translations
    for entry_id, translation_text in to_merge:
        try:
            coll.update_one(
                {"_id": entry_id, "Translations.translation": translation_text},
                {"$addToSet": {"Translations.$.TranslationMetadata": {
                    "DictionaryName": LEXICON,
                    "PartOfSpeech": None,
                    "Page": None,
                    "IndexOnPage": None,
                }}}
            )
            merged_count += 1
        except Exception as ex:
            print(f"  merge error: {ex}", file=sys.stderr)
            errors += 1

    # 2. Add new translations to existing entries
    for entry_id, new_translation in to_add:
        try:
            coll.update_one(
                {"_id": entry_id},
                {"$push": {"Translations": new_translation}}
            )
            added_count += 1
        except Exception as ex:
            print(f"  add error: {ex}", file=sys.stderr)
            errors += 1

    # 3. Create new entries
    if to_create:
        try:
            coll.insert_many(to_create)
            created_count = len(to_create)
        except Exception as ex:
            print(f"  create error: {ex}", file=sys.stderr)
            errors += 1

    print(f"\nDone.")
    print(f"  Lexicon metadata added to existing translations: {merged_count}")
    print(f"  New translations added to existing entries:      {added_count}")
    print(f"  New entries created:                             {created_count}")
    if errors:
        print(f"  Errors: {errors}", file=sys.stderr)


if __name__ == "__main__":
    main()
