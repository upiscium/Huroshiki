from __future__ import annotations

from contextlib import redirect_stdout
from io import BytesIO
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

import huroshiki_core as core
import provider_lookup


class FakeResponse(BytesIO):
    def __init__(self, contents: bytes, url: str = "https://api.curseforge.com/v1/test"):
        super().__init__(contents)
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def geturl(self):
        return self.url


class ProviderLookupHelperTest(unittest.TestCase):
    @staticmethod
    def curseforge_project(
        project_id: int = 328085,
        *,
        slug: str = "create",
        name: str = "Create",
    ) -> dict[str, object]:
        return {
            "id": project_id,
            "name": name,
            "slug": slug,
            "summary": "Aesthetic Technology",
            "authors": [{"id": 1, "name": "simibubi", "url": "https://example.invalid"}],
        }

    @classmethod
    def curseforge_search_payload(
        cls,
        projects: list[dict[str, object]],
        *,
        page_size: int = 20,
    ) -> bytes:
        return json.dumps(
            {
                "data": projects,
                "pagination": {
                    "index": 0,
                    "pageSize": page_size,
                    "resultCount": len(projects),
                    "totalCount": len(projects),
                },
            }
        ).encode()

    def test_modrinth_resolve_accepts_id_slug_and_url(self) -> None:
        calls = []

        def open_request(request, *, timeout):
            calls.append((request.full_url, timeout))
            return FakeResponse(
                json.dumps(
                    {"id": "canonical", "slug": "sodium-extra", "title": "Sodium Extra"}
                ).encode()
            )

        for selector in (
            "canonical",
            "sodium-extra",
            "https://modrinth.com/mod/sodium-extra",
        ):
            with self.subTest(selector=selector), patch.object(
                provider_lookup, "urlopen", side_effect=open_request
            ):
                result = provider_lookup.resolve_modrinth(selector)
            self.assertEqual(result["project_id"], "canonical")
        self.assertEqual(len(calls), 3)

    def test_modrinth_search_sends_filters_and_canonical_results(self) -> None:
        seen_url = ""

        def open_request(request, *, timeout):
            nonlocal seen_url
            seen_url = request.full_url
            return FakeResponse(
                json.dumps(
                    {
                        "hits": [
                            {
                                "project_id": "one",
                                "slug": "sodium-extra",
                                "title": "Sodium Extra",
                                "description": "Extra options",
                                "author": "author",
                            }
                        ]
                    }
                ).encode()
            )

        with patch.object(provider_lookup, "urlopen", side_effect=open_request):
            result = provider_lookup.search_modrinth(
                "sodium extra", minecraft="1.21.1", loader="neoforge", limit=20
            )
        query = parse_qs(urlparse(seen_url).query)
        facets = json.loads(query["facets"][0])
        self.assertIn(["project_type:mod"], facets)
        self.assertIn(["versions:1.21.1"], facets)
        self.assertIn(["categories:neoforge"], facets)
        self.assertEqual(result["results"][0]["project_id"], "one")

    def test_curseforge_search_maps_filters_and_returns_neutral_results(self) -> None:
        seen = []

        def open_request(request, *, timeout, allowed_hosts):
            seen.append((request, timeout, allowed_hosts))
            return FakeResponse(
                self.curseforge_search_payload([self.curseforge_project()], page_size=7),
                request.full_url,
            )

        with patch.dict(
            os.environ,
            {provider_lookup.CURSEFORGE_API_KEY_ENV: "secret-key"},
            clear=True,
        ), patch.object(
            provider_lookup, "open_provider_request", side_effect=open_request
        ):
            result = provider_lookup.search_curseforge(
                "Create", minecraft="1.21.1", loader="neoforge", limit=7
            )

        request = seen[0][0]
        query = parse_qs(urlparse(request.full_url).query)
        self.assertEqual(query["searchFilter"], ["Create"])
        self.assertEqual(query["gameVersion"], ["1.21.1"])
        self.assertEqual(query["modLoaderType"], ["6"])
        self.assertEqual(query["pageSize"], ["7"])
        self.assertEqual(request.get_header("X-api-key"), "secret-key")
        self.assertNotIn("secret-key", request.full_url)
        self.assertEqual(
            result,
            {
                "provider": "curseforge",
                "results": [
                    {
                        "project_id": "328085",
                        "slug": "create",
                        "title": "Create",
                        "description": "Aesthetic Technology",
                        "author": "simibubi",
                    }
                ],
            },
        )

    def test_curseforge_loader_mapping_zero_results_and_limit(self) -> None:
        seen_types = []

        def open_request(request, *, timeout, allowed_hosts):
            query = parse_qs(urlparse(request.full_url).query)
            seen_types.append(query["modLoaderType"][0])
            return FakeResponse(
                self.curseforge_search_payload([], page_size=int(query["pageSize"][0])),
                request.full_url,
            )

        with patch.dict(
            os.environ,
            {provider_lookup.CURSEFORGE_API_KEY_ENV: "key"},
            clear=True,
        ), patch.object(
            provider_lookup, "open_provider_request", side_effect=open_request
        ):
            for loader in ("forge", "neoforge", "fabric", "quilt"):
                result = provider_lookup.search_curseforge(
                    "none", minecraft="1.20.1", loader=loader, limit=3
                )
                self.assertEqual(result["results"], [])
        self.assertEqual(seen_types, ["1", "6", "4", "5"])
        for loader, limit in (("unknown", 20), ("forge", 0), ("forge", 51)):
            with self.subTest(loader=loader, limit=limit), self.assertRaises(
                provider_lookup.LookupError
            ):
                provider_lookup.search_curseforge(
                    "none", minecraft="1.20.1", loader=loader, limit=limit
                )

    def test_curseforge_resolve_numeric_and_project_url(self) -> None:
        requests = []

        def open_request(request, *, timeout, allowed_hosts):
            requests.append(request)
            if "/mods/328085" in request.full_url:
                payload = json.dumps({"data": self.curseforge_project()}).encode()
            else:
                payload = self.curseforge_search_payload(
                    [self.curseforge_project()], page_size=2
                )
            return FakeResponse(payload, request.full_url)

        with patch.dict(
            os.environ,
            {provider_lookup.CURSEFORGE_API_KEY_ENV: "key"},
            clear=True,
        ), patch.object(
            provider_lookup, "open_provider_request", side_effect=open_request
        ):
            numeric = provider_lookup.resolve_curseforge("328085")
            project_url = provider_lookup.resolve_curseforge(
                "https://www.curseforge.com/minecraft/mc-mods/create"
            )
        self.assertEqual(numeric["project_id"], "328085")
        self.assertEqual(project_url["project_id"], "328085")
        self.assertIn("/mods/328085", requests[0].full_url)
        self.assertEqual(parse_qs(urlparse(requests[1].full_url).query)["slug"], ["create"])

    def test_curseforge_cli_preserves_request_id_envelope(self) -> None:
        result = {
            "provider": "curseforge",
            "project_id": "328085",
            "slug": "create",
            "title": "Create",
        }
        output = StringIO()
        with patch.object(provider_lookup, "resolve_curseforge", return_value=result), redirect_stdout(
            output
        ):
            returncode = provider_lookup.main(
                ["--request-id", "request-123", "curseforge", "resolve", "328085"]
            )
        self.assertEqual(returncode, 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"request_id": "request-123", "result": result},
        )

    def test_curseforge_resolve_rejects_ambiguous_or_invalid_selectors(self) -> None:
        invalid = (
            "Create",
            "0",
            "http://www.curseforge.com/minecraft/mc-mods/create",
            "https://example.com/minecraft/mc-mods/create",
            "https://www.curseforge.com/minecraft/modpacks/create",
            "https://user:pass@www.curseforge.com/minecraft/mc-mods/create",
            "https://www.curseforge.com/minecraft/mc-mods/create?x=1",
            "https://www.curseforge.com:bad/minecraft/mc-mods/create",
        )
        for selector in invalid:
            with self.subTest(selector=selector), self.assertRaises(provider_lookup.LookupError):
                provider_lookup.curseforge_project_reference(selector)

    def test_curseforge_credential_and_response_fail_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                provider_lookup.LookupError, "HUROSHIKI_CURSEFORGE_API_KEY"
            ):
                provider_lookup.search_curseforge(
                    "Create", minecraft="1.21.1", loader="fabric", limit=20
                )

    def test_curseforge_http_errors_and_redirects_do_not_leak_key(self) -> None:
        secret = "do-not-print-this-key"
        error_body = FakeResponse(secret.encode())
        http_error = HTTPError(
            "https://api.curseforge.com/v1/mods/search",
            403,
            "Forbidden",
            {},
            error_body,
        )
        with patch.dict(
            os.environ,
            {provider_lookup.CURSEFORGE_API_KEY_ENV: secret},
            clear=True,
        ), patch.object(
            provider_lookup, "open_provider_request", side_effect=http_error
        ):
            with self.assertRaises(provider_lookup.LookupError) as raised:
                provider_lookup.search_curseforge(
                    "Create", minecraft="1.21.1", loader="fabric", limit=20
                )
        self.assertNotIn(secret, str(raised.exception))
        self.assertTrue(error_body.closed)

        handler = provider_lookup._RestrictedRedirectHandler(
            frozenset({provider_lookup.CURSEFORGE_API_HOST})
        )
        with self.assertRaisesRegex(provider_lookup.LookupError, "invalid API endpoint"):
            handler.redirect_request(
                Request(
                    "https://api.curseforge.com/v1/mods/search",
                    headers={"x-api-key": secret},
                ),
                None,
                302,
                "Found",
                {},
                "https://example.com/stolen",
            )

        invalid_payloads = (
            b"not-json",
            b'{"data":[],"data":[],"pagination":{}}',
            json.dumps({"data": [], "pagination": {}}).encode(),
            json.dumps(
                {
                    "data": [],
                    "pagination": {
                        "index": 0,
                        "pageSize": 20,
                        "resultCount": 0,
                        "totalCount": 0,
                    },
                    "unknown": True,
                }
            ).encode(),
            self.curseforge_search_payload(
                [{**self.curseforge_project(), "unknown": True}], page_size=20
            ),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), patch.dict(
                os.environ,
                {provider_lookup.CURSEFORGE_API_KEY_ENV: "secret-key"},
                clear=True,
            ), patch.object(
                provider_lookup,
                "open_provider_request",
                return_value=FakeResponse(payload),
            ):
                with self.assertRaises(provider_lookup.LookupError) as raised:
                    provider_lookup.search_curseforge(
                        "Create", minecraft="1.21.1", loader="fabric", limit=20
                    )
                self.assertNotIn("secret-key", str(raised.exception))

        with patch.object(provider_lookup, "MAX_RESPONSE_BYTES", 4), patch.dict(
            os.environ,
            {provider_lookup.CURSEFORGE_API_KEY_ENV: "secret-key"},
            clear=True,
        ), patch.object(
            provider_lookup,
            "open_provider_request",
            return_value=FakeResponse(b"12345"),
        ):
            with self.assertRaisesRegex(provider_lookup.LookupError, "size limit"):
                provider_lookup.search_curseforge(
                    "Create", minecraft="1.21.1", loader="fabric", limit=20
                )

    def test_curseforge_rejects_duplicate_and_invalid_project_ids(self) -> None:
        for projects, message in (
            (
                [self.curseforge_project(), self.curseforge_project()],
                "duplicate project ID",
            ),
            ([self.curseforge_project(0)], "positive decimal"),
            ([self.curseforge_project(10**21)], "positive decimal"),
        ):
            with self.subTest(message=message), patch.dict(
                os.environ,
                {provider_lookup.CURSEFORGE_API_KEY_ENV: "key"},
                clear=True,
            ), patch.object(
                provider_lookup,
                "open_provider_request",
                return_value=FakeResponse(
                    self.curseforge_search_payload(projects, page_size=20)
                ),
            ):
                with self.assertRaisesRegex(provider_lookup.LookupError, message):
                    provider_lookup.search_curseforge(
                        "Create", minecraft="1.21.1", loader="forge", limit=20
                    )


class ProviderLookupCoreTest(unittest.TestCase):
    @staticmethod
    def process(payload: object, **overrides) -> core.ResolverProcessResult:
        values = dict(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
            cancelled=False,
            timed_out=False,
            orphaned_descendants=False,
        )
        values.update(overrides)
        return core.ResolverProcessResult(**values)

    @classmethod
    def responder(cls, payload: object, **overrides):
        def respond(command, **_kwargs):
            request_id = command[command.index("--request-id") + 1]
            return cls.process(
                {"request_id": request_id, "result": payload},
                **overrides,
            )

        return respond

    def test_actual_helper_process_protocol_is_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scripts = Path(directory)
            helper = scripts / "provider_lookup.py"
            helper.write_text(
                "import json, sys\n"
                "request_id = sys.argv[sys.argv.index('--request-id') + 1]\n"
                "result = {'provider':'modrinth','project_id':'canonical',"
                "'slug':'slug','title':'Title'}\n"
                "print(json.dumps({'request_id': request_id, 'result': result}))\n",
                encoding="utf-8",
            )
            with patch.object(core, "SCRIPTS", scripts), patch.object(
                core, "ROOT", scripts
            ):
                result = core.resolve_project_selector("modrinth", "slug")
        self.assertEqual(result.canonical_project_id, "canonical")
        self.assertEqual(result.display_label, "Title")

    def test_search_validates_results_and_rejects_duplicates(self) -> None:
        payload = {
            "provider": "modrinth",
            "results": [
                {
                    "project_id": "one",
                    "slug": "first",
                    "title": "Same",
                    "description": "",
                    "author": "",
                },
                {
                    "project_id": "two",
                    "slug": "second",
                    "title": "Same",
                    "description": "details",
                    "author": "author",
                },
            ],
        }
        with patch.object(
            core, "run_resolver_process", side_effect=self.responder(payload)
        ) as runner:
            projects = core.search_provider_projects(
                "modrinth", "same", minecraft="1.21.1", loader="neoforge"
            )
        self.assertEqual([project.project_id for project in projects], ["one", "two"])
        command = runner.call_args.args[0]
        self.assertIn("--minecraft", command)
        self.assertIn("--loader", command)

        payload["results"][1]["project_id"] = "one"
        with patch.object(
            core, "run_resolver_process", side_effect=self.responder(payload)
        ):
            with self.assertRaisesRegex(core.HuroshikiError, "duplicate project ID"):
                core.search_provider_projects(
                    "modrinth", "same", minecraft="1.21.1", loader="neoforge"
                )

    def test_lookup_cancel_deadline_orphan_and_protocol_failures(self) -> None:
        cases = (
            (dict(cancelled=True), "cancelled"),
            (dict(timed_out=True), "deadline exceeded"),
            (dict(termination_incomplete=True), "termination was incomplete"),
            (dict(orphaned_descendants=True), "background processes"),
            (dict(returncode=7, stderr="lookup failed"), "lookup failed"),
        )
        valid = {
            "provider": "modrinth",
            "project_id": "one",
            "slug": "one",
            "title": "One",
        }
        for overrides, message in cases:
            with self.subTest(message=message), patch.object(
                core,
                "run_resolver_process",
                side_effect=self.responder(valid, **overrides),
            ):
                with self.assertRaisesRegex(core.HuroshikiError, message):
                    core.resolve_project_selector("modrinth", "one")

        with patch.object(
            core,
            "run_resolver_process",
            return_value=core.ResolverProcessResult(0, "not json", "", False, False),
        ):
            with self.assertRaises(core.HuroshikiError):
                core.resolve_project_selector("modrinth", "one")
        for envelope, message in (
            ({"request_id": "wrong", "result": valid}, "mismatched request ID"),
            (
                {"request_id": "wrong", "result": valid, "extra": True},
                "invalid response envelope",
            ),
        ):
            with self.subTest(message=message), patch.object(
                core,
                "run_resolver_process",
                return_value=self.process(envelope),
            ):
                with self.assertRaisesRegex(core.HuroshikiError, message):
                    core.resolve_project_selector("modrinth", "one")
        for invalid in (
            {"provider": "modrinth", "slug": "one", "title": "One"},
            {
                "provider": "modrinth",
                "project_id": "one",
                "slug": "one",
                "title": "bad\nvalue",
            },
        ):
            with self.subTest(invalid=invalid), patch.object(
                core,
                "run_resolver_process",
                side_effect=self.responder(invalid),
            ):
                with self.assertRaises(core.HuroshikiError):
                    core.resolve_project_selector("modrinth", "one")

    def test_cancel_deadline_are_forwarded_and_curseforge_is_canonical(self) -> None:
        cancel = threading.Event()
        deadline = time.monotonic() + 10
        valid = {
            "provider": "modrinth",
            "project_id": "one",
            "slug": "one",
            "title": "One",
        }
        with patch.object(
            core, "run_resolver_process", side_effect=self.responder(valid)
        ) as runner:
            core.resolve_project_selector(
                "modrinth", "one", cancel_event=cancel, deadline=deadline
            )
        self.assertIs(runner.call_args.kwargs["cancel_event"], cancel)
        self.assertEqual(runner.call_args.kwargs["deadline"], deadline)

        curseforge_search = {
            "provider": "curseforge",
            "results": [
                {
                    "project_id": "328085",
                    "slug": "create",
                    "title": "Create",
                    "description": "Technology",
                    "author": "simibubi",
                }
            ],
        }
        with patch.dict(
            os.environ,
            {provider_lookup.CURSEFORGE_API_KEY_ENV: "secret-key"},
        ), patch.object(
            core,
            "run_resolver_process",
            side_effect=self.responder(curseforge_search),
        ) as runner:
            projects = core.search_provider_projects(
                "curseforge",
                "Create",
                minecraft="1.21.1",
                loader="neoforge",
                cancel_event=cancel,
                deadline=deadline,
            )
        self.assertEqual(projects[0].provider, "curseforge")
        self.assertEqual(projects[0].project_id, "328085")
        command = runner.call_args.args[0]
        self.assertEqual(command[command.index("curseforge") :][:2], ["curseforge", "search"])
        self.assertNotIn("secret-key", command)
        self.assertIs(runner.call_args.kwargs["cancel_event"], cancel)
        self.assertEqual(runner.call_args.kwargs["deadline"], deadline)

        with patch.object(core, "run_resolver_process") as resolver:
            resolved = core.resolve_project_selector("curseforge", "cf:328085")
        resolver.assert_not_called()
        self.assertEqual(resolved.canonical_project_id, "328085")

    def test_core_rejects_invalid_curseforge_protocol_fields(self) -> None:
        invalid = (
            {
                "provider": "curseforge",
                "project_id": "0",
                "slug": "bad",
                "title": "Bad",
            },
            {
                "provider": "curseforge",
                "project_id": "1",
                "slug": "one",
                "title": "One",
                "unknown": True,
            },
        )
        for payload in invalid:
            with self.subTest(payload=payload), patch.object(
                core,
                "run_resolver_process",
                side_effect=self.responder(payload),
            ):
                with self.assertRaises(core.HuroshikiError):
                    core.resolve_project_selector(
                        "curseforge",
                        "https://www.curseforge.com/minecraft/mc-mods/one",
                    )

    def test_provider_search_operation_shares_absolute_deadline(self) -> None:
        deadline = time.monotonic() + 10
        operation = core.ProviderSearchOperation(
            provider="curseforge",
            query="Create",
            minecraft="1.21.1",
            loader="neoforge",
            deadline=deadline,
        )
        with patch.object(core, "search_provider_projects", return_value=()) as search:
            self.assertEqual(operation.run(), ())
        self.assertIs(search.call_args.kwargs["cancel_event"], operation.cancel_event)
        self.assertEqual(search.call_args.kwargs["deadline"], deadline)


if __name__ == "__main__":
    unittest.main()
