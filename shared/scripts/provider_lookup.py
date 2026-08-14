#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen


MODRINTH_API_ROOT = "https://api.modrinth.com/v2"
API_ROOT = MODRINTH_API_ROOT
USER_AGENT = "upiscium-huroshiki/1.0"
NETWORK_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ERROR_RESPONSE_BYTES = 16 * 1024
_MODRINTH_IMMUTABLE_ID_RE = re.compile(r"^[A-Za-z0-9]{8}$")
_VERSION_TYPES = frozenset(("release", "beta", "alpha"))
_VERSION_TEXT_MAX = 256
_FILENAME_MAX = 512
_MAX_PROVIDER_VERSION_RECORDS = 1000


class LookupError(RuntimeError):
    pass


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LookupError(f"Provider returned duplicate JSON field {key!r}")
        result[key] = value
    return result


def _reject_unsafe_selector_characters(value: str) -> None:
    if any(
        ord(character) < 32
        or ord(character) == 127
        or (ord(character) > 127 and character.isspace())
        for character in value
    ):
        raise LookupError(
            "Invalid Modrinth project selector whitespace or control characters"
        )


def modrinth_project_reference(selector: str) -> str:
    _reject_unsafe_selector_characters(selector)
    value = selector.strip()
    if value.lower().startswith("mr:"):
        _reject_unsafe_selector_characters(value[3:])
        value = value[3:].strip()
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.hostname not in {
            "modrinth.com",
            "www.modrinth.com",
        }:
            raise LookupError(f"Invalid Modrinth project URL: {selector!r}")
        if parsed.username is not None or parsed.password is not None:
            raise LookupError(f"Invalid Modrinth project URL: {selector!r}")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2 or parts[0] not in {"mod", "project"}:
            raise LookupError(f"Invalid Modrinth project URL: {selector!r}")
        value = unquote(parts[1])
        _reject_unsafe_selector_characters(value)
        value = value.strip()
    if not value or any(character.isspace() for character in value):
        raise LookupError(f"Invalid Modrinth project selector: {selector!r}")
    return value


def request_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> object:
    request_headers = {"User-Agent": USER_AGENT}
    if headers is not None:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        try:
            error.read(MAX_ERROR_RESPONSE_BYTES + 1)
        finally:
            error.close()
        raise LookupError(f"Provider request failed with HTTP {error.code}") from error
    except (URLError, TimeoutError) as error:
        raise LookupError("Provider request failed") from error
    if len(payload) > MAX_RESPONSE_BYTES:
        raise LookupError("Provider response exceeded the size limit")
    try:
        return json.loads(payload, object_pairs_hook=_strict_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise LookupError("Provider returned invalid JSON") from error


def required_text(record: object, key: str) -> str:
    if not isinstance(record, dict):
        raise LookupError("Provider returned a non-object project")
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise LookupError(f"Provider project has no {key}")
    return value


def required_modrinth_id(record: object, key: str) -> str:
    value = required_text(record, key)
    if _MODRINTH_IMMUTABLE_ID_RE.fullmatch(value) is None:
        raise LookupError(f"Provider returned invalid immutable Modrinth {key}")
    return value


def optional_text(record: object, key: str) -> str:
    if not isinstance(record, dict):
        raise LookupError("Provider returned a non-object project")
    value = record.get(key, "")
    if not isinstance(value, str):
        raise LookupError(f"Provider project has invalid {key}")
    return value


def normalize_description(value: str) -> str:
    """Validate and collapse display-only description whitespace to one line."""
    if any(
        (ord(character) < 32 and character not in "\t\n\r")
        or ord(character) == 127
        for character in value
    ):
        raise LookupError("Provider description contains unsafe control characters")
    return " ".join(value.split())


def _bounded_text(record: object, key: str, maximum: int) -> str:
    value = required_text(record, key)
    if len(value) > maximum or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise LookupError(f"Provider version has invalid {key}")
    return value


def _version_list(record: object, key: str, requested: str) -> list[str]:
    if not isinstance(record, dict):
        raise LookupError("Provider returned a non-object version")
    values = record.get(key)
    if not isinstance(values, list) or not values or not all(
        isinstance(value, str)
        and bool(value)
        and len(value) <= _VERSION_TEXT_MAX
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
        for value in values
    ):
        raise LookupError(f"Provider version has invalid {key}")
    if requested not in values:
        raise LookupError(f"Provider version does not support requested {key}")
    return values


def modrinth_versions(
    project_id: str,
    *,
    minecraft: str,
    loader: str,
    include_prerelease: bool = False,
    limit: int = 20,
) -> list[dict[str, object]]:
    """Return strictly validated, deterministic Modrinth version candidates."""
    if _MODRINTH_IMMUTABLE_ID_RE.fullmatch(project_id) is None:
        raise LookupError("Invalid canonical Modrinth project ID")
    if (
        not isinstance(minecraft, str)
        or not minecraft
        or len(minecraft) > _VERSION_TEXT_MAX
    ):
        raise LookupError("Invalid Minecraft version")
    if loader not in {"fabric", "forge", "neoforge", "quilt"}:
        raise LookupError("Invalid Modrinth loader")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= 100
    ):
        raise LookupError("Version limit must be between 1 and 100")
    query: dict[str, str] = {
        "game_versions": json.dumps([minecraft], separators=(",", ":")),
        "loaders": json.dumps([loader], separators=(",", ":")),
        "limit": str(limit),
        "include_changelog": "false",
    }
    if not include_prerelease:
        query["version_type"] = "release"
    parameters = urlencode(query)
    response = request_json(
        f"{API_ROOT}/project/{quote(project_id, safe='')}/version?{parameters}"
    )
    if not isinstance(response, list):
        raise LookupError("Provider versions response is not a list")
    if len(response) > _MAX_PROVIDER_VERSION_RECORDS:
        raise LookupError("Provider returned too many version records")
    if len(response) > limit:
        raise LookupError("Provider returned more versions than requested")
    candidates: list[tuple[datetime, dict[str, object]]] = []
    seen: set[str] = set()
    for version in response:
        if not isinstance(version, dict):
            raise LookupError("Provider returned a non-object version")
        returned_project = required_text(version, "project_id")
        if returned_project != project_id:
            raise LookupError("Provider version has a mismatched project ID")
        artifact_id = required_modrinth_id(version, "id")
        if artifact_id in seen:
            raise LookupError("Provider returned duplicate version IDs")
        seen.add(artifact_id)
        version_number = _bounded_text(version, "version_number", _VERSION_TEXT_MAX)
        release_type = required_text(version, "version_type")
        if release_type not in _VERSION_TYPES:
            raise LookupError("Provider version has an invalid release type")
        published_at = _bounded_text(version, "date_published", _VERSION_TEXT_MAX)
        try:
            published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise LookupError("Provider version has an invalid publication date") from error
        if published.tzinfo is None or published.utcoffset() is None:
            raise LookupError("Provider version publication date is not timezone-aware")
        game_versions = _version_list(version, "game_versions", minecraft)
        loaders = _version_list(version, "loaders", loader)
        files = version.get("files")
        if not isinstance(files, list) or not files:
            raise LookupError("Provider version has no files")
        primary: list[str] = []
        for file in files:
            if not isinstance(file, dict):
                raise LookupError("Provider version has an invalid file")
            if file.get("primary") is True:
                primary.append(_bounded_text(file, "filename", _FILENAME_MAX))
        if len(primary) != 1:
            raise LookupError("Provider version must have exactly one primary file")
        candidate = {
            "provider": "modrinth",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "version": version_number,
            "filename": primary[0],
            "game_versions": game_versions,
            "loaders": loaders,
            "release_type": release_type,
            "published_at": published_at,
        }
        if include_prerelease or release_type == "release":
            candidates.append((published, candidate))
    candidates.sort(key=lambda item: item[1]["artifact_id"])
    candidates.sort(
        key=lambda item: item[0].astimezone(timezone.utc),
        reverse=True,
    )
    return [candidate for _, candidate in candidates]


def resolve_modrinth(selector: str) -> dict[str, str]:
    reference = modrinth_project_reference(selector)
    record = request_json(f"{API_ROOT}/project/{quote(reference, safe='')}")
    return {
        "provider": "modrinth",
        "project_id": required_modrinth_id(record, "id"),
        "slug": required_text(record, "slug"),
        "title": required_text(record, "title"),
    }


def search_modrinth(
    query: str,
    *,
    minecraft: str,
    loader: str,
    limit: int,
) -> dict[str, object]:
    if not query.strip():
        raise LookupError("Search query cannot be empty")
    facets = [
        ["project_type:mod"],
        [f"versions:{minecraft}"],
        [f"categories:{loader}"],
    ]
    parameters = urlencode(
        {
            "query": query.strip(),
            "limit": str(limit),
            "facets": json.dumps(facets, separators=(",", ":")),
        }
    )
    response = request_json(f"{API_ROOT}/search?{parameters}")
    if not isinstance(response, dict) or not isinstance(response.get("hits"), list):
        raise LookupError("Provider search response has no results list")
    hits = response["hits"]
    if len(hits) > limit:
        raise LookupError("Provider returned more search results than requested")
    results = []
    for hit in hits:
        results.append(
            {
                "project_id": required_modrinth_id(hit, "project_id"),
                "slug": required_text(hit, "slug"),
                "title": required_text(hit, "title"),
                "description": normalize_description(
                    optional_text(hit, "description")
                ),
                "author": optional_text(hit, "author"),
            }
        )
    return {"provider": "modrinth", "results": results}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-id", required=True)
    parser.add_argument("provider", choices=("modrinth",))
    subcommands = parser.add_subparsers(dest="action", required=True)
    resolve = subcommands.add_parser("resolve")
    resolve.add_argument("selector")
    search = subcommands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--minecraft", required=True)
    search.add_argument("--loader", required=True)
    search.add_argument("--limit", type=int, default=20, choices=range(1, 51))
    versions = subcommands.add_parser("versions")
    versions.add_argument("project_id")
    versions.add_argument("--minecraft", required=True)
    versions.add_argument(
        "--loader", required=True, choices=("fabric", "forge", "neoforge", "quilt")
    )
    versions.add_argument("--include-prerelease", action="store_true")
    versions.add_argument("--limit", type=int, default=20, choices=range(1, 101))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (
        not args.request_id
        or len(args.request_id) > 128
        or any(ord(character) < 33 or ord(character) > 126 for character in args.request_id)
    ):
        print("Invalid provider request ID", file=sys.stderr)
        return 2
    try:
        if args.action == "resolve":
            result = resolve_modrinth(args.selector)
        elif args.action == "search":
            result = search_modrinth(
                args.query,
                minecraft=args.minecraft,
                loader=args.loader,
                limit=args.limit,
            )
        else:
            result = modrinth_versions(
                args.project_id,
                minecraft=args.minecraft,
                loader=args.loader,
                include_prerelease=args.include_prerelease,
                limit=args.limit,
            )
    except LookupError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"request_id": args.request_id, "result": result},
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
