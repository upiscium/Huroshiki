from __future__ import annotations

from io import BytesIO
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

    def test_actual_helper_process_protocol_is_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scripts = Path(directory)
            helper = scripts / "provider_lookup.py"
            helper.write_text(
                "import json\n"
                "print(json.dumps({'provider':'modrinth','project_id':'canonical',"
                "'slug':'slug','title':'Title'}))\n",
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
            core, "run_resolver_process", return_value=self.process(payload)
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
            core, "run_resolver_process", return_value=self.process(payload)
        ):
            with self.assertRaisesRegex(core.HuroshikiError, "duplicate project ID"):
                core.search_provider_projects(
                    "modrinth", "same", minecraft="1.21.1", loader="neoforge"
                )

    def test_lookup_cancel_deadline_orphan_and_protocol_failures(self) -> None:
        cases = (
            (dict(cancelled=True), "cancelled"),
            (dict(timed_out=True), "deadline exceeded"),
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
                return_value=self.process(valid, **overrides),
            ):
                with self.assertRaisesRegex(core.HuroshikiError, message):
                    core.resolve_project_selector("modrinth", "one")

        invalid_results = (
            "not json",
            json.dumps({"provider": "modrinth", "slug": "one", "title": "One"}),
            json.dumps(
                {
                    "provider": "modrinth",
                    "project_id": "one",
                    "slug": "one",
                    "title": "bad\nvalue",
                }
            ),
        )
        for stdout in invalid_results:
            with self.subTest(stdout=stdout), patch.object(
                core,
                "run_resolver_process",
                return_value=core.ResolverProcessResult(0, stdout, "", False, False),
            ):
                with self.assertRaises(core.HuroshikiError):
                    core.resolve_project_selector("modrinth", "one")

    def test_cancel_is_forwarded_and_curseforge_search_fails_before_process(self) -> None:
        cancel = threading.Event()
        deadline = time.monotonic() + 10
        valid = {
            "provider": "modrinth",
            "project_id": "one",
            "slug": "one",
            "title": "One",
        }
        with patch.object(
            core, "run_resolver_process", return_value=self.process(valid)
        ) as runner:
            core.resolve_project_selector(
                "modrinth", "one", cancel_event=cancel, deadline=deadline
            )
        self.assertIs(runner.call_args.kwargs["cancel_event"], cancel)
        self.assertEqual(runner.call_args.kwargs["deadline"], deadline)

        with patch.object(core, "run_resolver_process") as runner:
            with self.assertRaisesRegex(core.HuroshikiError, "numeric project ID"):
                core.search_provider_projects(
                    "curseforge", "name", minecraft="1.21.1", loader="neoforge"
                )
            resolved = core.resolve_project_selector("curseforge", "cf:12345")
        runner.assert_not_called()
        self.assertEqual(resolved.canonical_project_id, "12345")


if __name__ == "__main__":
    unittest.main()
