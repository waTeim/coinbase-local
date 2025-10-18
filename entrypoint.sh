#!/usr/bin/env bash
set -euo pipefail

DEFAULT_CONFIG="/app/config/app-config.toml"

has_config_arg=0
for arg in "$@"; do
  case "$arg" in
    --config|--config=*)
      has_config_arg=1
      break
      ;;
  esac
done

if [[ ${has_config_arg} -eq 0 ]]; then
  if [[ ! -f "${DEFAULT_CONFIG}" ]]; then
    echo "error: provide --config or ensure ${DEFAULT_CONFIG} exists" >&2
    exit 1
  fi
  set -- --config "${DEFAULT_CONFIG}" "$@"
fi

exec python -m src.main "$@"
