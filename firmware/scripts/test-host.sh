#!/usr/bin/env bash
# Python model + C firmware core tests (needs python3 >= 3.10 and a C compiler)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
python3 -m unittest discover -s tests -t . "$@"
