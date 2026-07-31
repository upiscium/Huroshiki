#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


MODRINTH_API_ROOT = "https://api.modrinth.com/v2"
CURSEFORGE_API_ROOT = "https://api.curseforge.com/v1"
CURSEFORGE_API_HOST = "api.curseforge.com"
CURSEFORGE_API_KEY_ENV = "HUROSHIKI_CURSEFORGE_API_KEY"
CURSEFORGE_GAME_ID = 432
CURSEFORGE_MOD_CLASS_ID = 6
CURSEFORGE_LOADER_TYPES = {
    "forge": 1,
    "fabric": 4,
    "quilt": 5,
    "neoforge": 6,
}
CURSEFORGE_WEB_HOSTS = {"curseforge.com", "www.curseforge.com"}
API_ROOT = MODRINTH_API_ROOT
USER_AGENT = "upiscium-huroshiki/1.0"
NETWORK_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ERROR_RESPONSE_BYTES = 16 * 1024

_CURSEFORGE_MOD_FIELDS = {
    "id",
    "gameId",
    "name",
    "slug",
    "links",
    "summary",
    "status",
    "downloadCount",
    "isFeatured",
    "primaryCategoryId",
    "categories",
    "classId",
    "authors",
    "logo",
    "screenshots",
    "mainFileId",
    "latestFiles",
    "latestFilesIndexes",
    "latestEarlyAccessFilesIndexes",
    "dateCreated",
    "dateModified",
    "dateReleased",
    "allowModDistribution",
    "gamePopularityRank",
    "isAvailable",
    "thumbsUpCount",
    "rating",
    "etag",
}
_CURSEFORGE_AUTHOR_FIELDS = {"id", "name", "url"}


class LookupError(RuntimeError):
    pass


class _RestrictedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_api_url(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_api_url(url: str, allowed_hosts: frozenset[str]) -> None:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise LookupError("Provider redirected to an invalid API endpoint") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise LookupError("Provider redirected to an invalid API endpoint")


def open_provider_request(
    request: Request,
    *,
    timeout: float,
    allowed_hosts: frozenset[str] | None,
):
    if allowed_hosts is None:
        return urlopen(request, timeout=timeout)
    validate_api_url(request.full_url, allowed_hosts)
    opener = build_opener(_RestrictedRedirectHandler(allowed_hosts))
    return opener.open(request, timeout=timeout)


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
    allowed_hosts: frozenset[str] | None = None,
) -> object:
    request_headers = {"User-Agent": USER_AGENT}
    if headers is not None:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    try:
        with open_provider_request(
            request,
            timeout=NETWORK_TIMEOUT_SECONDS,
            allowed_hosts=allowed_hosts,
        ) as response:
            if allowed_hosts is not None:
                validate_api_url(response.geturl(), allowed_hosts)
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


def strict_mapping(
    value: object,
    *,
    context: str,
    allowed: set[str],
    required: set[str],
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise LookupError(f"{context} must be an object")
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise LookupError(f"{context} has unknown field {sorted(unknown)[0]!r}")
    if missing:
        raise LookupError(f"{context} has no {sorted(missing)[0]}")
    return value


def positive_project_id(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise LookupError("CurseForge project ID must be a positive decimal value")
    text = str(value)
    if not text.isdecimal() or len(text) > 20 or int(text) <= 0:
        raise LookupError("CurseForge project ID must be a positive decimal value")
    return str(int(text))


def curseforge_api_key() -> str:
    value = os.environ.get(CURSEFORGE_API_KEY_ENV, "").strip()
    if not value:
        raise LookupError(
            f"Set {CURSEFORGE_API_KEY_ENV} to use CurseForge search or resolve"
        )
    if len(value) > 4096 or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise LookupError(f"{CURSEFORGE_API_KEY_ENV} contains invalid characters")
    return value


def curseforge_request(path: str, parameters: dict[str, str] | None = None) -> object:
    if not path.startswith("/") or "?" in path or "#" in path:
        raise LookupError("Invalid internal CurseForge API path")
    query = "" if not parameters else f"?{urlencode(parameters)}"
    return request_json(
        f"{CURSEFORGE_API_ROOT}{path}{query}",
        headers={"x-api-key": curseforge_api_key()},
        allowed_hosts=frozenset({CURSEFORGE_API_HOST}),
    )


def curseforge_project_record(value: object) -> dict[str, str]:
    record = strict_mapping(
        value,
        context="CurseForge project",
        allowed=_CURSEFORGE_MOD_FIELDS,
        required={"id", "name", "slug", "summary", "authors"},
    )
    project_id = positive_project_id(record["id"])
    name = record["name"]
    slug = record["slug"]
    summary = record["summary"]
    if not all(isinstance(item, str) for item in (name, slug, summary)):
        raise LookupError("CurseForge project has invalid text fields")
    if (
        not name
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in name + summary
        )
    ):
        raise LookupError("CurseForge project has invalid text fields")
    raw_authors = record["authors"]
    if not isinstance(raw_authors, list):
        raise LookupError("CurseForge project authors must be a list")
    author_names: list[str] = []
    for raw_author in raw_authors:
        author = strict_mapping(
            raw_author,
            context="CurseForge author",
            allowed=_CURSEFORGE_AUTHOR_FIELDS,
            required={"name"},
        )
        author_name = author["name"]
        if not isinstance(author_name, str) or any(
            ord(character) < 32 or ord(character) == 127
            for character in author_name
        ):
            raise LookupError("CurseForge author has invalid name")
        if author_name:
            author_names.append(author_name)
    return {
        "project_id": project_id,
        "slug": slug,
        "title": name,
        "description": summary,
        "author": ", ".join(author_names),
    }


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


def curseforge_project_reference(selector: str) -> tuple[str, str]:
    value = selector.strip()
    if value.lower().startswith("cf:"):
        value = value[3:].strip()
    if value.isdecimal():
        return "id", positive_project_id(value)
    parsed = urlparse(value)
    if not parsed.scheme and not parsed.netloc:
        raise LookupError(
            "CurseForge resolve requires a numeric project ID or project URL"
        )
    try:
        port = parsed.port
    except ValueError as error:
        raise LookupError(f"Invalid CurseForge project URL: {selector!r}") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname not in CURSEFORGE_WEB_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or parsed.params
    ):
        raise LookupError(f"Invalid CurseForge project URL: {selector!r}")
    raw_parts = [part for part in parsed.path.split("/") if part]
    if len(raw_parts) != 3 or raw_parts[:2] != ["minecraft", "mc-mods"]:
        raise LookupError(f"Invalid CurseForge project URL: {selector!r}")
    if "%" in raw_parts[2]:
        raise LookupError(f"Invalid CurseForge project URL: {selector!r}")
    slug = unquote(raw_parts[2]).strip().lower()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise LookupError(f"Invalid CurseForge project URL: {selector!r}")
    return "slug", slug


def curseforge_search_response(response: object, *, limit: int) -> list[dict[str, str]]:
    envelope = strict_mapping(
        response,
        context="CurseForge response",
        allowed={"data", "pagination"},
        required={"data", "pagination"},
    )
    raw_results = envelope["data"]
    if not isinstance(raw_results, list) or len(raw_results) > limit:
        raise LookupError("CurseForge response has an invalid results list")
    pagination = strict_mapping(
        envelope["pagination"],
        context="CurseForge pagination",
        allowed={"index", "pageSize", "resultCount", "totalCount"},
        required={"index", "pageSize", "resultCount", "totalCount"},
    )
    for key in ("index", "pageSize", "resultCount", "totalCount"):
        value = pagination[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise LookupError(f"CurseForge pagination has invalid {key}")
    if (
        pagination["index"] != 0
        or pagination["pageSize"] > limit
        or pagination["resultCount"] > pagination["pageSize"]
        or pagination["totalCount"] < pagination["resultCount"]
    ):
        raise LookupError("CurseForge pagination is inconsistent")
    if pagination["resultCount"] != len(raw_results):
        raise LookupError("CurseForge pagination result count is inconsistent")
    return [curseforge_project_record(item) for item in raw_results]


def resolve_curseforge(selector: str) -> dict[str, str]:
    reference_type, reference = curseforge_project_reference(selector)
    if reference_type == "id":
        response = strict_mapping(
            curseforge_request(f"/mods/{quote(reference, safe='')}"),
            context="CurseForge response",
            allowed={"data"},
            required={"data"},
        )
        project = curseforge_project_record(response["data"])
        if project["project_id"] != reference:
            raise LookupError("CurseForge returned a mismatched project ID")
    else:
        projects = curseforge_search_response(
            curseforge_request(
                "/mods/search",
                {
                    "gameId": str(CURSEFORGE_GAME_ID),
                    "classId": str(CURSEFORGE_MOD_CLASS_ID),
                    "slug": reference,
                    "index": "0",
                    "pageSize": "2",
                },
            ),
            limit=2,
        )
        exact = [project for project in projects if project["slug"].lower() == reference]
        if len(exact) != 1:
            raise LookupError("CurseForge project URL did not resolve uniquely")
        project = exact[0]
    return {
        "provider": "curseforge",
        "project_id": project["project_id"],
        "slug": project["slug"],
        "title": project["title"],
    }


def search_curseforge(
    query: str,
    *,
    minecraft: str,
    loader: str,
    limit: int,
) -> dict[str, object]:
    normalized_query = query.strip()
    if (
        not normalized_query
        or len(normalized_query) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized_query)
    ):
        raise LookupError("CurseForge search query is invalid")
    normalized_minecraft = minecraft.strip()
    if (
        not normalized_minecraft
        or len(normalized_minecraft) > 64
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized_minecraft)
    ):
        raise LookupError("CurseForge Minecraft version is invalid")
    if not 1 <= limit <= 50:
        raise LookupError("CurseForge result limit must be between 1 and 50")
    try:
        loader_type = CURSEFORGE_LOADER_TYPES[loader.strip().lower()]
    except KeyError as error:
        raise LookupError(f"Unsupported CurseForge loader: {loader}") from error
    projects = curseforge_search_response(
        curseforge_request(
            "/mods/search",
            {
                "gameId": str(CURSEFORGE_GAME_ID),
                "classId": str(CURSEFORGE_MOD_CLASS_ID),
                "searchFilter": normalized_query,
                "gameVersion": normalized_minecraft,
                "modLoaderType": str(loader_type),
                "index": "0",
                "pageSize": str(limit),
            },
        ),
        limit=limit,
    )
    identities: set[str] = set()
    for project in projects:
        if project["project_id"] in identities:
            raise LookupError(
                f"CurseForge returned duplicate project ID {project['project_id']}"
            )
        identities.add(project["project_id"])
    return {"provider": "curseforge", "results": projects}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-id", required=True)
    parser.add_argument("provider", choices=("modrinth", "curseforge"))
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
            result = (
                resolve_modrinth(args.selector)
                if args.provider == "modrinth"
                else resolve_curseforge(args.selector)
            )
        else:
            search = search_modrinth if args.provider == "modrinth" else search_curseforge
            result = search(
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
