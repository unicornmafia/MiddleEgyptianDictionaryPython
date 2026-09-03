#!/usr/bin/env python3
"""
Migrate Faulkner entry placements using Lexicon.txt corpus frequency.

The original import always attached Faulkner translations to the FIRST
Vygus entry for a transliteration. When a transliteration has multiple
sign variants in the database, this is often wrong. This script uses the
OpenGlyp Lexicon.txt corpus frequencies to pick the most-attested sign
variant for each Faulkner entry.

Usage:
    ./venv/bin/python3 migrate_faulkner_placement.py [--dry-run]
"""

import os
import sys
import argparse
from collections import defaultdict
from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.environ.get("MONGO_DB", "MiddleEgyptianDictionary")

LEXICON_TXT = os.path.join(os.path.dirname(__file__),
                           "..", "MiddleEgyptianDataset",
                           "MiddleEgyptianDictionary", "Resources",
                           "Lexicon.txt")

FAULKNER = 4


def _norm_signs(gs: str) -> str:
    return " ".join(gs.upper().split())


def _build_lexicon_index(path: str) -> dict:
    index = defaultdict(list)
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.strip().split(";")
            if len(parts) < 4:
                continue
            sign_parts = [s.strip() for s in parts[0].split(",") if s.strip()]
            signs = " ".join(sign_parts)
            translit = parts[1].strip().replace("=", ".")
            try:
                freq = float(parts[3]) if parts[3].strip() else 0.0
            except ValueError:
                freq = 0.0
            if translit and signs:
                index[translit].append((_norm_signs(signs), freq))
    for k in index:
        index[k].sort(key=lambda x: -x[1])
    return index


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Print planned moves without modifying the database")
    args = parser.parse_args()

    if not os.path.exists(LEXICON_TXT):
        print(f"ERROR: Lexicon.txt not found at {LEXICON_TXT}", file=sys.stderr)
        sys.exit(1)

    print("Building Lexicon frequency index…")
    lex_idx = _build_lexicon_index(LEXICON_TXT)
    print(f"  {len(lex_idx)} transliterations indexed")

    client = MongoClient(MONGO_URI)
    coll = client[MONGO_DB]["DictionaryEntry"]

    print("Loading all entries from MongoDB…")
    all_entries = list(coll.find({}, {"_id": 1, "Transliteration": 1,
                                      "GardinerSigns": 1, "Translations": 1}))
    print(f"  {len(all_entries)} entries loaded")

    # Build index: (translit, norm_signs) → entry
    db_index: dict[tuple, dict] = {}
    # Also: translit → list of entries
    by_translit: dict[str, list] = defaultdict(list)
    for e in all_entries:
        t = e.get("Transliteration", "")
        gs = _norm_signs(e.get("GardinerSigns", ""))
        db_index[(t, gs)] = e
        by_translit[t].append(e)

    # Find all entries that have Faulkner translations
    def _get_faulkner_translations(entry: dict) -> list:
        return [
            t for t in entry.get("Translations", [])
            if any(m.get("DictionaryName") == FAULKNER
                   for m in t.get("TranslationMetadata", []))
        ]

    def _strip_faulkner(translations: list) -> tuple[list, list]:
        """Split translations into (faulkner_ones, non_faulkner_ones)."""
        faulkner, other = [], []
        for t in translations:
            meta_with_f = [m for m in t.get("TranslationMetadata", [])
                           if m.get("DictionaryName") == FAULKNER]
            meta_without_f = [m for m in t.get("TranslationMetadata", [])
                               if m.get("DictionaryName") != FAULKNER]
            if meta_with_f:
                # Separate out the Faulkner metadata
                faulkner_t = {**t, "TranslationMetadata": meta_with_f}
                faulkner.append(faulkner_t)
                if meta_without_f:
                    # Keep remaining metadata on original
                    other.append({**t, "TranslationMetadata": meta_without_f})
            else:
                other.append(t)
        return faulkner, other

    # Identify moves needed
    moves = []  # (source_entry, target_entry, faulkner_translations_to_move)

    for translit, entries in by_translit.items():
        if len(entries) <= 1:
            continue  # no ambiguity

        # Find entries that currently have Faulkner data
        faulkner_holders = [e for e in entries if _get_faulkner_translations(e)]
        if not faulkner_holders:
            continue  # no Faulkner here

        # If Faulkner is already on multiple entries, assume manually curated — skip
        if len(faulkner_holders) > 1:
            continue

        source = faulkner_holders[0]
        current_signs = _norm_signs(source.get("GardinerSigns", ""))

        # Find best target from Lexicon index
        lex_entries = lex_idx.get(translit, [])
        target_signs = None
        for (signs, _freq) in lex_entries:
            if (translit, signs) in db_index:
                target_signs = signs
                break

        if target_signs is None or target_signs == current_signs:
            continue  # no better placement found

        target = db_index[(translit, target_signs)]
        faulkner_ts = _get_faulkner_translations(source)
        moves.append((source, target, faulkner_ts))

    print(f"\nPlanned moves: {len(moves)}")

    if args.dry_run:
        print("\nDRY RUN — showing first 30 moves:")
        for src, tgt, fts in moves[:30]:
            print(f"  {src['Transliteration']!r}: "
                  f"{src.get('GardinerSigns')!r} → {tgt.get('GardinerSigns')!r}")
            for ft in fts:
                print(f"    translation: {ft['translation'][:60]!r}")
        return

    # Execute moves
    moved = 0
    errors = 0
    for src, tgt, faulkner_ts in moves:
        try:
            # 1. Remove Faulkner translations from source
            _, remaining = _strip_faulkner(src["Translations"])
            coll.update_one({"_id": src["_id"]},
                            {"$set": {"Translations": remaining}})

            # 2. Add Faulkner translations to target
            existing_target_translations = list(
                coll.find_one({"_id": tgt["_id"]}, {"Translations": 1})
                ["Translations"]
            )
            for ft in faulkner_ts:
                # Check if this translation text already exists on target
                existing_texts = {t["translation"] for t in existing_target_translations}
                if ft["translation"] in existing_texts:
                    # Append Faulkner metadata to existing translation
                    for t in existing_target_translations:
                        if t["translation"] == ft["translation"]:
                            existing_meta_names = {m["DictionaryName"]
                                                   for m in t["TranslationMetadata"]}
                            for m in ft["TranslationMetadata"]:
                                if m["DictionaryName"] not in existing_meta_names:
                                    t["TranslationMetadata"].append(m)
                            break
                else:
                    existing_target_translations.append(ft)

            coll.update_one({"_id": tgt["_id"]},
                            {"$set": {"Translations": existing_target_translations}})
            moved += 1
        except Exception as ex:
            print(f"  ERROR moving {src.get('Transliteration')}: {ex}", file=sys.stderr)
            errors += 1

    print(f"\nDone. Moved: {moved}, Errors: {errors}")
    if errors:
        print("Some moves failed — check the log above.", file=sys.stderr)


if __name__ == "__main__":
    main()
