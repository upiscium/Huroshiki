from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import unicodedata


class PortablePathError(ValueError):
    pass


_WINDOWS_DEVICE = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.I)


def _normalize_component(value: str, *, context: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or normalized in {".", ".."}:
        raise PortablePathError(f"{context} must be a non-empty portable basename")
    if "/" in normalized or "\\" in normalized:
        raise PortablePathError(f"{context} must not contain path separators")
    if any(character in '<>:"|?*' for character in normalized):
        raise PortablePathError(f"{context} contains characters invalid on Windows")
    if normalized[-1] in {".", " "}:
        raise PortablePathError(f"{context} must not end with a dot or space")
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise PortablePathError(f"{context} must not contain control characters")
    if _WINDOWS_DEVICE.fullmatch(normalized):
        raise PortablePathError(f"{context} uses a reserved Windows device name")
    return normalized


def portable_basename(value: str, *, context: str = "Filename") -> str:
    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
        raise PortablePathError(f"{context} must be a relative basename")
    if PureWindowsPath(value).drive or value.startswith(("//", "\\\\")):
        raise PortablePathError(f"{context} must not use a drive or UNC path")
    return _normalize_component(value, context=context)


def portable_basename_key(value: str, *, context: str = "Filename") -> str:
    return portable_basename(value, context=context).casefold()


def portable_relative_path(
    value: str | Path,
    *,
    context: str = "Metadata path",
) -> Path:
    raw = str(value)
    if not raw:
        raise PortablePathError(f"{context} must be a non-empty relative path")
    if PurePosixPath(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
        raise PortablePathError(f"{context} must be relative")
    if PureWindowsPath(raw).drive or raw.startswith(("//", "\\\\")):
        raise PortablePathError(f"{context} must not use a drive or UNC path")
    parts = raw.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PortablePathError(f"{context} must not contain empty or traversal components")
    return Path(
        *(
            _normalize_component(part, context=f"{context} component")
            for part in parts
        )
    )


def portable_relative_path_key(
    value: str | Path,
    *,
    context: str = "Metadata path",
) -> str:
    path = portable_relative_path(value, context=context)
    return "/".join(part.casefold() for part in path.parts)
