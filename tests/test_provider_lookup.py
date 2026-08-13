from __future__ import annotations

from contextlib import redirect_stdout
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import huroshiki_core as core
import provider_lookup


class FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ProviderLookupHelperTest(unittest.TestCase):
    def test_normalize_description_collapses_display_whitespace(self) -> None:
        cases = {
            "hello": "hello",
            "": "",
            "  hello  ": "hello",
            "hello   world": "hello world",
            "hello\tworld": "hello world",
            "hello\nworld": "hello world",
            "hello\rworld": "hello world",
            "hello\r\nworld": "hello world",
            "one\n\n two\tthree": "one two three",
            # str.split() intentionally applies the same policy to Unicode
            # whitespace while the ASCII control check remains explicit.
            "hello\u00a0\u2003world": "hello world",
        }
        for value, expected in cases.items():
            with self.subTest(value=repr(value)):
                self.assertEqual(provider_lookup.normalize_description(value), expected)

    def test_normalize_description_rejects_unsafe_controls(self) -> None:
        for control in ("\x00", "\x01", "\x07", "\x1b", "\x1f", "\x7f"):
            with self.subTest(control=repr(control)):
                with self.assertRaisesRegex(
                    provider_lookup.LookupError, "unsafe control characters"
                ):
                    provider_lookup.normalize_description(f"before{control}after")

    def test_modrinth_resolve_accepts_id_slug_and_url(self) -> None:
        calls = []

        def open_request(request, *, timeout):
            calls.append((request.full_url, timeout))
            return FakeResponse(
                json.dumps(
                    {"id": "A1b2C3d4", "slug": "sodium-extra", "title": "Sodium Extra"}
                ).encode()
            )

        for selector in (
            "A1b2C3d4",
            "sodium-extra",
            "https://modrinth.com/mod/sodium-extra",
        ):
            with self.subTest(selector=selector), patch.object(
                provider_lookup, "urlopen", side_effect=open_request
            ):
                result = provider_lookup.resolve_modrinth(selector)
            self.assertEqual(result["project_id"], "A1b2C3d4")
        self.assertEqual(len(calls), 3)

    def test_modrinth_resolve_rejects_noncanonical_provider_id(self) -> None:
        for value in ("sodium", "Abc1234", "Abcd12345", "Abcd-123"):
            with self.subTest(value=value), patch.object(
                provider_lookup,
                "urlopen",
                return_value=FakeResponse(
                    json.dumps(
                        {"id": value, "slug": "sodium", "title": "Sodium"}
                    ).encode()
                ),
            ):
                with self.assertRaisesRegex(
                    provider_lookup.LookupError, "invalid immutable Modrinth id"
                ):
                    provider_lookup.resolve_modrinth("sodium")

    def test_modrinth_project_reference_rejects_unsafe_whitespace_before_normalizing(
        self,
    ) -> None:
        for selector in (
            "mr:\u00a0sodium-extra",
            "https://modrinth.com/mod/sodium-extra%C2%A0",
            "https://modrinth.com/mod/sodium-extra%09",
            "sodium-extra\x00",
        ):
            with self.subTest(selector=repr(selector)):
                with self.assertRaisesRegex(
                    provider_lookup.LookupError,
                    "whitespace or control characters",
                ):
                    provider_lookup.modrinth_project_reference(selector)

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
                                "project_id": "Proj0001",
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
        self.assertEqual(result["results"][0]["project_id"], "Proj0001")

    def test_modrinth_search_normalizes_multiline_description(self) -> None:
        def open_request(request, *, timeout):
            return FakeResponse(
                json.dumps(
                    {
                        "hits": [
                            {
                                "project_id": "Proj0001",
                                "slug": "first",
                                "title": "First",
                                "description": "First line\nSecond\tline\r\nThird",
                                "author": "author",
                            }
                        ]
                    }
                ).encode()
            )

        with patch.object(provider_lookup, "urlopen", side_effect=open_request):
            result = provider_lookup.search_modrinth(
                "query", minecraft="1.21.1", loader="fabric", limit=20
            )
        self.assertEqual(
            result["results"][0]["description"],
            "First line Second line Third",
        )

    def test_modrinth_search_multiline_hit_does_not_fail_other_results(self) -> None:
        descriptions = ["normal description", "multiline\ndescription", "another"]

        def open_request(request, *, timeout):
            return FakeResponse(
                json.dumps(
                    {
                        "hits": [
                            {
                                "project_id": f"Proj{index:04d}",
                                "slug": f"project-{index}",
                                "title": f"Project {index}",
                                "description": description,
                                "author": "author",
                            }
                            for index, description in enumerate(descriptions)
                        ]
                    }
                ).encode()
            )

        with patch.object(provider_lookup, "urlopen", side_effect=open_request):
            result = provider_lookup.search_modrinth(
                "query", minecraft="1.21.1", loader="fabric", limit=20
            )
        self.assertEqual(
            [item["description"] for item in result["results"]],
            ["normal description", "multiline description", "another"],
        )

    def test_strict_json_and_response_limits(self) -> None:
        invalid_payloads = (
            b"not-json",
            b'{"id":"one","id":"two"}',
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), patch.object(
                provider_lookup, "urlopen", return_value=FakeResponse(payload)
            ):
                with self.assertRaises(provider_lookup.LookupError):
                    provider_lookup.search_modrinth(
                        "query", minecraft="1.21.1", loader="fabric", limit=20
                    )

        with patch.object(provider_lookup, "MAX_RESPONSE_BYTES", 4), patch.object(
            provider_lookup, "urlopen", return_value=FakeResponse(b"12345")
        ):
            with self.assertRaisesRegex(provider_lookup.LookupError, "size limit"):
                provider_lookup.resolve_modrinth("one")

    def test_modrinth_cli_preserves_request_id_envelope(self) -> None:
        result = {
            "provider": "modrinth",
            "project_id": "Proj0001",
            "slug": "one",
            "title": "One",
        }
        output = StringIO()
        with patch.object(provider_lookup, "resolve_modrinth", return_value=result), redirect_stdout(
            output
        ):
            returncode = provider_lookup.main(
                ["--request-id", "request-123", "modrinth", "resolve", "one"]
            )
        self.assertEqual(returncode, 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"request_id": "request-123", "result": result},
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
                "result = {'provider':'modrinth','project_id':'A1b2C3d4',"
                "'slug':'slug','title':'Title'}\n"
                "print(json.dumps({'request_id': request_id, 'result': result}))\n",
                encoding="utf-8",
            )
            with patch.object(core, "SCRIPTS", scripts), patch.object(
                core, "ROOT", scripts
            ):
                result = core.resolve_project_selector("modrinth", "slug")
        self.assertEqual(result.canonical_project_id, "A1b2C3d4")
        self.assertEqual(result.display_label, "Title")

    def test_core_selector_normalization_rejects_unsafe_prefix_whitespace(self) -> None:
        with patch.object(core, "run_resolver_process") as resolver:
            for selector in (
                "mr:\u00a0slug",
                "mr:slug\x00",
                "\u00a0slug",
                "https://modrinth.com/mod/slug%C2%A0",
            ):
                with self.subTest(selector=repr(selector)):
                    with self.assertRaisesRegex(core.HuroshikiError, "unsafe"):
                        core.resolve_project_selector("modrinth", selector)
            resolver.assert_not_called()

    def test_search_validates_results_and_rejects_duplicates(self) -> None:
        payload = {
            "provider": "modrinth",
            "results": [
                {
                    "project_id": "Proj0001",
                    "slug": "first",
                    "title": "Same",
                    "description": "",
                    "author": "",
                },
                {
                    "project_id": "Proj0002",
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
        self.assertEqual([project.project_id for project in projects], ["Proj0001", "Proj0002"])
        command = runner.call_args.args[0]
        self.assertIn("--minecraft", command)
        self.assertIn("--loader", command)

        payload["results"][1]["project_id"] = "Proj0001"
        with patch.object(
            core, "run_resolver_process", side_effect=self.responder(payload)
        ):
            with self.assertRaisesRegex(core.HuroshikiError, "duplicate project ID"):
                core.search_provider_projects(
                    "modrinth", "same", minecraft="1.21.1", loader="neoforge"
                )

    def test_search_accepts_normalized_description_but_rejects_raw_newline(self) -> None:
        payload = {
            "provider": "modrinth",
            "results": [
                {
                    "project_id": "Proj0001",
                    "slug": "first",
                    "title": "First",
                    "description": "First line Second line",
                    "author": "author",
                }
            ],
        }
        with patch.object(
            core, "run_resolver_process", side_effect=self.responder(payload)
        ):
            projects = core.search_provider_projects(
                "modrinth", "first", minecraft="1.21.1", loader="neoforge"
            )
        self.assertEqual(projects[0].description, "First line Second line")

        payload["results"][0]["description"] = "First line\nSecond line"
        with patch.object(
            core, "run_resolver_process", side_effect=self.responder(payload)
        ):
            with self.assertRaisesRegex(
                core.HuroshikiError,
                "Provider lookup returned control characters in description",
            ):
                core.search_provider_projects(
                    "modrinth", "first", minecraft="1.21.1", loader="neoforge"
                )

    def test_search_identity_fields_remain_strict(self) -> None:
        base_result = {
            "project_id": "Proj0001",
            "slug": "first",
            "title": "First",
            "description": "description",
            "author": "author",
        }
        for field, value, message in (
            ("project_id", "bad\nid", "control characters in project_id"),
            ("slug", "first\tsecond", "control characters in slug"),
            ("title", "First\nSecond", "control characters in title"),
            ("author", "author\x1b", "control characters in author"),
        ):
            with self.subTest(field=field):
                result = dict(base_result)
                result[field] = value
                payload = {"provider": "modrinth", "results": [result]}
                with patch.object(
                    core, "run_resolver_process", side_effect=self.responder(payload)
                ):
                    with self.assertRaisesRegex(core.HuroshikiError, message):
                        core.search_provider_projects(
                            "modrinth",
                            "first",
                            minecraft="1.21.1",
                            loader="neoforge",
                        )

    def test_lookup_process_and_protocol_failures(self) -> None:
        valid = {
            "provider": "modrinth",
            "project_id": "Proj0001",
            "slug": "one",
            "title": "One",
        }
        for overrides, message in (
            (dict(cancelled=True), "cancelled"),
            (dict(timed_out=True), "deadline exceeded"),
            (dict(termination_incomplete=True), "termination was incomplete"),
            (dict(orphaned_descendants=True), "background processes"),
            (dict(returncode=7, stderr="lookup failed"), "lookup failed"),
        ):
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
            with self.assertRaisesRegex(core.HuroshikiError, "invalid JSON"):
                core.resolve_project_selector("modrinth", "one")
        for envelope, message in (
            ({"request_id": "wrong", "result": valid}, "mismatched request ID"),
            (
                {"request_id": "wrong", "result": valid, "extra": True},
                "invalid response envelope",
            ),
        ):
            with self.subTest(message=message), patch.object(
                core, "run_resolver_process", return_value=self.process(envelope)
            ):
                with self.assertRaisesRegex(core.HuroshikiError, message):
                    core.resolve_project_selector("modrinth", "one")
        for invalid in (
            {"provider": "modrinth", "slug": "one", "title": "One"},
            {
                "provider": "modrinth",
                "project_id": "Proj0001",
                "slug": "one",
                "title": "bad\nvalue",
            },
        ):
            with self.subTest(invalid=invalid), patch.object(
                core, "run_resolver_process", side_effect=self.responder(invalid)
            ):
                with self.assertRaises(core.HuroshikiError):
                    core.resolve_project_selector("modrinth", "one")

    def test_curseforge_core_is_numeric_and_network_free(self) -> None:
        with patch.object(core, "run_resolver_process") as resolver:
            for selector in ("12345", "cf:12345", "cf:00012345"):
                with self.subTest(selector=selector):
                    resolved = core.resolve_project_selector("curseforge", selector)
                    self.assertEqual(resolved.canonical_project_id, "12345")
            for selector in (
                "create",
                "create-mod",
                "https://www.curseforge.com/minecraft/mc-mods/create",
            ):
                with self.subTest(selector=selector), self.assertRaisesRegex(
                    core.HuroshikiError, "positive decimal"
                ):
                    core.resolve_project_selector("curseforge", selector)
            resolver.assert_not_called()

        with patch.object(core, "run_resolver_process") as resolver:
            with self.assertRaisesRegex(core.HuroshikiError, "unavailable"):
                core.search_provider_projects(
                    "curseforge",
                    "Create",
                    minecraft="1.21.1",
                    loader="neoforge",
                )
            resolver.assert_not_called()
        with self.assertRaisesRegex(core.HuroshikiError, "only for Modrinth"):
            core.ProviderSearchOperation(
                provider="curseforge",
                query="Create",
                minecraft="1.21.1",
                loader="neoforge",
            )

    def test_cancel_and_deadline_are_forwarded(self) -> None:
        cancel = threading.Event()
        deadline = time.monotonic() + 10
        valid = {
            "provider": "modrinth",
            "project_id": "Proj0001",
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


if __name__ == "__main__":
    unittest.main()
