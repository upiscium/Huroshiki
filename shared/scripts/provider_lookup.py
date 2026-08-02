#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
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

class LookupError(RuntimeError):
    pass


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LookupError(f"Provider returned duplicate JSON field {key!r}")
        result[key] = value
    return result


def modrinth_project_reference(selector: str) -> str:
    value = selector.strip()
    if value.lower().startswith("mr:"):
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
        value = unquote(parts[1]).strip()
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


def optional_text(record: object, key: str) -> str:
    if not isinstance(record, dict):
        raise LookupError("Provider returned a non-object project")
    value = record.get(key, "")
    if not isinstance(value, str):
        raise LookupError(f"Provider project has invalid {key}")
    return value


def resolve_modrinth(selector: str) -> dict[str, str]:
    reference = modrinth_project_reference(selector)
    record = request_json(f"{API_ROOT}/project/{quote(reference, safe='')}")
    return {
        "provider": "modrinth",
        "project_id": required_text(record, "id"),
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
                "project_id": required_text(hit, "project_id"),
                "slug": required_text(hit, "slug"),
                "title": required_text(hit, "title"),
                "description": optional_text(hit, "description"),
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
        else:
            result = search_modrinth(
                args.query,
                minecraft=args.minecraft,
                loader=args.loader,
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
