from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Sequence


def root_argument(arguments: Sequence[str]) -> str | None:
    """Return the last --root value without changing the argument list."""
    value: str | None = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--root":
            if index + 1 < len(arguments):
                value = arguments[index + 1]
            index += 2
            continue
        if argument.startswith("--root="):
            value = argument.partition("=")[2]
        index += 1
    return value


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
