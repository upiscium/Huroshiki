#!/usr/bin/env bash

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
script="$script_dir/huroshiki.py"

if [[ -z "${HUROSHIKI_DATA_DIR-}" && -f "$script_dir/../../share/huroshiki/profiles.yaml" ]]; then
  export HUROSHIKI_DATA_DIR="$script_dir/../../share/huroshiki"
fi

# Preserve source-tree ancestor discovery without coupling installed code to data.
if [[ -z "${HUROSHIKI_ROOT-}" ]]; then
  root="$PWD"
  while [[ "$root" != "/" ]]; do
    if [[ -f "$root/flake.nix" && -f "$root/shared/scripts/huroshiki.py" ]]; then
      export HUROSHIKI_ROOT="$root"
      break
    fi
    root="$(dirname -- "$root")"
  done
fi

exec "${HUROSHIKI_PYTHON-python}" "$script" "$@"
