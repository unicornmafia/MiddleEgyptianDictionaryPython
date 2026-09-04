#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
mongorestore --drop --db MiddleEgyptianDictionary "${SCRIPT_DIR}/dump/MiddleEgyptianDictionary"
