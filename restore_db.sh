#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${SCRIPT_DIR}/venv/bin/python3"

echo "=== Restoring base dump ==="
mongorestore --db MiddleEgyptianDictionary "${SCRIPT_DIR}/dump/MiddleEgyptianDictionary"

echo ""
echo "=== Importing OpenGlyp Lexicon (DictionaryName=2) ==="
"$PYTHON" "${SCRIPT_DIR}/import_lexicon.py"

echo ""
echo "=== Migrating Faulkner placements using Lexicon corpus frequency ==="
"$PYTHON" "${SCRIPT_DIR}/migrate_faulkner_placement.py"

echo ""
echo "=== Done ==="
