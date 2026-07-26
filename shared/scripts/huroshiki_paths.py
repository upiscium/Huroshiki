from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Sequence


_IMPORT_ROOT: Path | None = None


def root_argument(arguments: Sequence[str]) -> str | None:
    """Return the first valid global --root value without changing arguments."""
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            break
        if argument == "--root":
            if index + 1 < len(arguments) and not arguments[index + 1].startswith("-"):
                return arguments[index + 1]
            index += 2
            continue
        if argument.startswith("--root="):
            return argument.partition("=")[2]
        if not argument.startswith("-"):
            break
        index += 1
    return None


def set_import_root(root: Path) -> None:
    """Keep root-derived globals consistent across the TUI module tree."""
    global _IMPORT_ROOT
    _IMPORT_ROOT = root


def import_root_argument(arguments: Sequence[str]) -> str | os.PathLike[str] | None:
    if _IMPORT_ROOT is not None:
        return _IMPORT_ROOT
    return root_argument(arguments)


def resolve_root(
    command_line_root: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    current = Path.cwd() if cwd is None else Path(cwd).expanduser()
    selected = command_line_root or environment.get("HUROSHIKI_ROOT")
    candidate = current if selected is None else Path(selected).expanduser()
    if not candidate.is_absolute():
        candidate = current / candidate
    return Path(os.path.abspath(candidate))
