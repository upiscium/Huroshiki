root="${HUROSHIKI_ROOT-}"
if [[ -z "$root" ]]; then
  root="$PWD"
  while [[ "$root" != "/" ]]; do
    if [[ -f "$root/flake.nix" && -f "$root/shared/scripts/huroshiki.py" ]]; then
      break
    fi
    root="$(dirname "$root")"
  done
fi

script="$root/shared/scripts/huroshiki.py"
if [[ ! -f "$script" ]]; then
  echo "huroshiki: not inside the MODPACK monorepo" >&2
  exit 1
fi

exec "${HUROSHIKI_PYTHON-python}" "$script" "$@"
