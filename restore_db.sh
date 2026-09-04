#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
mongorestore --db MiddleEgyptianDictionary "${SCRIPT_DIR}/dump/MiddleEgyptianDictionary"
