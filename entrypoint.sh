#!/usr/bin/env bash
set -euo pipefail

# Forward to the FastAPI server entrypoint while ensuring products are configured.
if [[ "${PRODUCTS:-}" == "" && "$#" -eq 0 ]]; then
  echo "error: set the PRODUCTS environment variable or pass product symbols as arguments" >&2
  exit 1
fi

if [[ "${1:-}" == "--" ]]; then
  shift
fi

exec python -m src.main "$@"
