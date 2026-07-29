from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl
from template_import import resolve_template_import_plan


PACK_TOML = """name = "Demo"
pack-format = "packwiz:1.1.0"
[versions]
minecraft = "1.21.1"
neoforge = "21.1.1"
"""


def metadata(name: str, project_id: str, filename: str) -> bytes:
    return f'''name = "{name}"
filename = "{filename}"
side = "both"
[download]
url = "https://cdn.example/{filename}"
hash-format = "sha256"
hash = "00"
[update.modrinth]
mod-id = "{project_id}"
version = "1"
    '''.encode()


def url_metadata(name: str, filename: str, url: str) -> bytes:
    return f'''name = "{name}"
filename = "{filename}"
side = "both"
[download]
url = "{url}"
hash-format = "sha256"
hash = "00"
'''.encode()


class TemplateImportCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.pack = self.packs / "demo"
        self.source = self.pack / "source"
        self.template = self.templates / "base"
        self.source.mkdir(parents=True)
        self.template.mkdir(parents=True)
        (self.pack / "pack.yaml").write_text(
            "id: demo\ndisplay_name: Demo\nenabled: true\n",
            encoding="utf-8",
        )
        (self.source / "pack.toml").write_text(PACK_TOML, encoding="utf-8")
        (self.source / "index.toml").write_text("hash-format = \"sha256\"\n", encoding="utf-8")
        (self.template / "template.yaml").write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: neoforge\n"
            "reference_loader_version: 21.1.0\nmods:\n"
            "  - name: Root\n    provider: modrinth\n"
            "    project_id: root\n    side: client\n",
            encoding="utf-8",
        )
        (self.pack / "content" / "common").mkdir(parents=True)
        (self.pack / "content" / "common" / "keep.txt").write_text("keep")
        self.patches = (
            patch.object(core, "ROOT", self.root),
            patch.object(core, "PACKS", self.packs),
            patch.object(core, "TEMPLATES", self.templates),
            patch.object(packctl, "ROOT", self.root),
            patch.object(packctl, "PACKS", self.packs),
            patch.object(packctl, "TEMPLATES", self.templates),
        )
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def closure(self) -> core.ResolvedModClosure:
        records = (
            core.ResolvedMetadata(
                ("modrinth", "root"),
                Path("mods/root.pw.toml"),
                "root.jar",
                metadata("Root", "root", "root.jar"),
                "modrinth",
                "root",
            ),
            core.ResolvedMetadata(
                ("modrinth", "dependency"),
                Path("mods/dependency.pw.toml"),
                "dependency.jar",
                metadata("Dependency", "dependency", "dependency.jar"),
                "modrinth",
                "dependency",
            ),
        )
        return core.ResolvedModClosure(("modrinth", "root"), records)

    def url_closure(self, actual_id: str = "actual") -> core.ResolvedModClosure:
        record = core.ResolvedMetadata(
            ("url", actual_id),
            Path(f"mods/{actual_id}.pw.toml"),
            f"{actual_id}.jar",
            url_metadata(
                "Actual Root",
                f"{actual_id}.jar",
                "https://mods.example/requested.jar",
            ),
            "url",
            actual_id,
        )
        return core.ResolvedModClosure(("url", actual_id), (record,))

    def use_url_template(self) -> None:
        (self.template / "template.yaml").write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: neoforge\n"
            "reference_loader_version: 21.1.0\nmods:\n"
            "  - name: Requested\n    provider: url\n"
            "    project_id: logical\n    side: client\n"
            "    url: https://mods.example/requested.jar\n",
            encoding="utf-8",
        )

    def use_url_conflict_template(self) -> None:
        (self.template / "template.yaml").write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: neoforge\n"
            "reference_loader_version: 21.1.0\nmods:\n"
            "  - name: Good\n    provider: url\n"
            "    project_id: logical\n    side: client\n"
            "    url: https://mods.example/good.jar\n"
            "  - name: Bad\n    provider: url\n"
            "    project_id: logical\n    side: client\n"
            "    url: https://mods.example/bad.jar\n",
            encoding="utf-8",
        )

    def operation(self) -> core.TemplateImportOperation:
        session = core.TemplateImportSession.create("pack:demo", ["base"])
        resolved = resolve_template_import_plan(session.plan)
        return core.TemplateImportOperation(session, resolved)

    @staticmethod
    def refresh_ok(command: list[str], **_: object) -> core.ResolverProcessResult:
        return core.ResolverProcessResult(0, "", "", False, False)

    def test_dry_run_classifies_root_dependency_and_preserves_real_pack(self) -> None:
        before = core.tree_digest_snapshot(self.source)
        overlay = (self.pack / "content" / "common" / "keep.txt").read_bytes()
        operation = self.operation()
        with (
            patch.object(core, "resolve_mod_closure", return_value=self.closure()),
            patch.object(core, "run_resolver_process", side_effect=self.refresh_ok),
        ):
            operation.run()
        self.assertIsNone(operation.error)
        self.assertEqual(
            [item.actual_identity for item in operation.preview.added_roots],
            [("modrinth", "root")],
        )
        self.assertEqual(
            [item.project_id for item in operation.preview.added_dependencies],
            ["dependency"],
        )
        self.assertEqual(core.tree_digest_snapshot(self.source), before)
        self.assertEqual((self.pack / "content" / "common" / "keep.txt").read_bytes(), overlay)
        operation.discard()
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_apply_publishes_complete_closure_atomically(self) -> None:
        operation = self.operation()
        with (
            patch.object(core, "resolve_mod_closure", return_value=self.closure()),
            patch.object(core, "run_resolver_process", side_effect=self.refresh_ok),
        ):
            operation.run()
            operation.apply()
        self.assertTrue((self.source / "mods/root.pw.toml").is_file())
        self.assertTrue((self.source / "mods/dependency.pw.toml").is_file())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_resolver_refresh_and_template_change_fail_closed(self) -> None:
        for failure in ("resolver", "refresh"):
            with self.subTest(failure=failure):
                operation = self.operation()
                resolver = (
                    patch.object(core, "resolve_mod_closure", side_effect=core.HuroshikiError("resolver failed"))
                    if failure == "resolver"
                    else patch.object(core, "resolve_mod_closure", return_value=self.closure())
                )
                refresh = core.ResolverProcessResult(1, "", "failed", False, False)
                with resolver, patch.object(core, "run_resolver_process", return_value=refresh):
                    operation.run()
                self.assertIsNotNone(operation.error)
                self.assertFalse((self.source / "mods").exists())
                self.assertFalse(packctl.project_lock_is_active("pack:demo"))

        operation = self.operation()
        with (
            patch.object(core, "resolve_mod_closure", return_value=self.closure()),
            patch.object(core, "run_resolver_process", side_effect=self.refresh_ok),
        ):
            operation.run()
        path = self.template / "template.yaml"
        path.write_text(path.read_text() + "\n", encoding="utf-8")
        with self.assertRaisesRegex(core.HuroshikiError, "Template manifest changed"):
            operation.apply()
        self.assertFalse((self.source / "mods").exists())

    def test_cli_conflict_without_resolution_fails_without_transaction(self) -> None:
        mods = self.source / "mods"
        mods.mkdir()
        (mods / "installed.pw.toml").write_bytes(
            metadata("Root", "installed", "installed.jar")
        )
        args = type(
            "Args",
            (),
            {
                "pack": "demo",
                "templates": ["base"],
                "resolution": None,
                "apply": False,
                "json": False,
            },
        )()
        stderr = StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(packctl.cmd_apply_template(args), 2)
        self.assertIn("resolution file", stderr.getvalue())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

        args.json = True
        stdout = StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(packctl.cmd_apply_template(args), 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["plan_digest"], core.prepare_template_import_plan(
            "pack:demo", ["base"]
        ).plan_digest)
        self.assertEqual(payload["conflicts"]["name"][0]["key"], "root")
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_resolution_digest_and_cli_parser_fail_closed(self) -> None:
        plan = core.prepare_template_import_plan("pack:demo", ["base"])
        resolution = self.root / "resolution.yaml"
        resolution.write_text(
            "version: 2\nplan_digest: stale\nname_conflicts: {}\nside_conflicts: {}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(packctl.ConfigError, "stale plan digest"):
            packctl._template_import_resolution(resolution, plan)
        args = packctl.parser().parse_args(
            ["apply-template", "demo", "base", "--apply", "--json"]
        )
        self.assertIs(args.func, packctl.cmd_apply_template)
        self.assertEqual(args.templates, ["base"])
        self.assertTrue(args.apply)

    def test_session_uses_transaction_source_as_only_pack_plan_source(self) -> None:
        original = core.pack_import_candidates
        observed: list[Path] = []

        def inspect_source(source: Path, pack_id: str):
            observed.append(source)
            self.assertNotEqual(source, self.source)
            self.assertTrue(packctl.project_lock_is_active("pack:demo"))
            return original(source, pack_id)

        with patch.object(core, "pack_import_candidates", side_effect=inspect_source):
            session = core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertEqual(observed, [session.transaction.source])
        session.discard()

    def test_external_pack_change_after_session_creation_blocks_apply(self) -> None:
        original = core.pack_import_candidates

        def mutate_real_pack(source: Path, pack_id: str):
            mods = self.source / "mods"
            mods.mkdir(exist_ok=True)
            (mods / "late.pw.toml").write_bytes(metadata("Late", "late", "late.jar"))
            return original(source, pack_id)

        with patch.object(
            core,
            "pack_import_candidates",
            side_effect=mutate_real_pack,
        ):
            operation = self.operation()
        self.assertEqual(operation.plan.pack_candidates, ())
        with (
            patch.object(core, "resolve_mod_closure", return_value=self.closure()),
            patch.object(core, "run_resolver_process", side_effect=self.refresh_ok),
        ):
            operation.run()
        with self.assertRaisesRegex(core.HuroshikiError, "real Packwiz source changed"):
            operation.apply()
        self.assertTrue((self.source / "mods/late.pw.toml").is_file())

    def test_external_config_change_after_preview_blocks_apply(self) -> None:
        operation = self.operation()
        with (
            patch.object(core, "resolve_mod_closure", return_value=self.closure()),
            patch.object(core, "run_resolver_process", side_effect=self.refresh_ok),
        ):
            operation.run()
        (self.pack / "pack.local.yaml").write_text(
            "url_max_jar_size_bytes: 1024\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(core.HuroshikiError, "configuration changed"):
            operation.apply()
        self.assertFalse((self.source / "mods").exists())

    def test_failed_session_creation_releases_project_lock(self) -> None:
        with patch.object(
            core,
            "pack_import_candidates",
            side_effect=core.HuroshikiError("failed"),
        ), self.assertRaisesRegex(core.HuroshikiError, "failed"):
            core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_url_policy_identity_and_cached_closure_reach_preview(self) -> None:
        self.use_url_template()
        (self.template / "template.local.yaml").write_text(
            "url_max_jar_size_bytes: 1234\n"
            "url_allow_private_networks: true\n",
            encoding="utf-8",
        )
        closure = self.url_closure()
        with patch.object(core, "resolve_mod_closure", return_value=closure) as resolver:
            session = core.TemplateImportSession.create("pack:demo", ["base"])
            resolved = resolve_template_import_plan(session.plan)
            operation = core.TemplateImportOperation(session, resolved)
            with patch.object(
                core, "run_resolver_process", side_effect=self.refresh_ok
            ):
                operation.run()
        self.assertEqual(resolver.call_count, 1)
        self.assertEqual(resolver.call_args.kwargs["url_max_jar_size_bytes"], 1234)
        self.assertTrue(resolver.call_args.kwargs["url_allow_private_networks"])
        self.assertEqual(
            operation.preview.added_roots[0].requested_identity,
            ("url", "logical"),
        )
        self.assertEqual(
            operation.preview.added_roots[0].actual_identity,
            ("url", "actual"),
        )
        self.assertEqual(operation.preview.added_dependencies, ())
        operation.discard()

    def test_actual_identity_change_invalidates_resolution(self) -> None:
        self.use_url_template()
        with patch.object(
            core, "resolve_mod_closure", return_value=self.url_closure("first")
        ):
            first = core.TemplateImportSession.create("pack:demo", ["base"])
        first_digest = first.plan.plan_digest
        first.discard()
        with patch.object(
            core, "resolve_mod_closure", return_value=self.url_closure("second")
        ):
            second = core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertNotEqual(first_digest, second.plan.plan_digest)
        second.discard()

    def test_removal_rechecks_actual_identity_before_unlink(self) -> None:
        mods = self.source / "mods"
        mods.mkdir()
        path = mods / "installed.pw.toml"
        path.write_bytes(metadata("Installed", "installed", "installed.jar"))
        transaction = core.PackTransaction.create("pack:demo")
        candidate = core.pack_import_candidates(transaction.source, "demo")[0]
        staged = transaction.source / candidate.metadata_path
        staged.write_bytes(metadata("Changed", "changed", "changed.jar"))
        with self.assertRaisesRegex(core.HuroshikiError, "changed before removal"):
            core._remove_import_candidates(transaction.source, (candidate,))
        transaction.discard()

    def test_failed_unselected_url_candidate_does_not_block_session(self) -> None:
        self.use_url_conflict_template()
        with patch.object(
            core,
            "resolve_mod_closure",
            side_effect=(self.url_closure("good_actual"), core.HuroshikiError("HTTP 404")),
        ) as resolver:
            session = core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertEqual(resolver.call_count, 2)
        self.assertEqual(
            [item.succeeded for item in session.plan.verifications],
            [True, False],
        )
        good, bad = session.plan.template_candidates
        resolved = resolve_template_import_plan(
            session.plan,
            url_selector_resolutions={
                "url:logical": core.ConflictResolution((good.candidate_key,))
            },
        )
        operation = core.TemplateImportOperation(session, resolved)
        with patch.object(core, "run_resolver_process", side_effect=self.refresh_ok):
            operation.run()
        self.assertIsNone(operation.error)
        self.assertEqual(
            operation.preview.added_roots[0].actual_identity,
            ("url", "good_actual"),
        )
        self.assertNotIn(bad.candidate_key, [
            item.candidate_key for item in resolved.selected_template_candidates
        ])
        operation.discard()

    def test_selecting_failed_url_candidate_is_rejected(self) -> None:
        self.use_url_conflict_template()
        with patch.object(
            core,
            "resolve_mod_closure",
            side_effect=(self.url_closure("good_actual"), core.HuroshikiError("HTTP 404")),
        ):
            session = core.TemplateImportSession.create("pack:demo", ["base"])
        _good, bad = session.plan.template_candidates
        with self.assertRaisesRegex(core.TemplateMergeError, "HTTP 404"):
            resolve_template_import_plan(
                session.plan,
                url_selector_resolutions={
                    "url:logical": core.ConflictResolution((bad.candidate_key,))
                },
            )
        session.discard()
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_failed_non_conflicting_candidate_blocks_operation(self) -> None:
        self.use_url_template()
        with patch.object(
            core,
            "resolve_mod_closure",
            side_effect=core.HuroshikiError("invalid JAR"),
        ):
            session = core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertFalse(session.plan.requires_resolution)
        with self.assertRaisesRegex(core.TemplateMergeError, "invalid JAR"):
            resolve_template_import_plan(session.plan)
        session.discard()

    def test_url_verification_cancellation_and_deadline_remain_global(self) -> None:
        self.use_url_template()
        cancelled = threading.Event()

        def cancel_then_fail(**_: object):
            cancelled.set()
            raise core.HuroshikiError("download cancelled")

        with patch.object(
            core, "resolve_mod_closure", side_effect=cancel_then_fail
        ), self.assertRaises(core.LoaderMigrationCancelled):
            core.TemplateImportSession.create(
                "pack:demo", ["base"], cancel_event=cancelled
            )
        with patch.object(
            core,
            "resolve_mod_closure",
            side_effect=core.LoaderMigrationDeadlineExceeded("deadline"),
        ), self.assertRaises(core.LoaderMigrationDeadlineExceeded):
            core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_closure_metadata_change_changes_fingerprint_and_digest(self) -> None:
        self.use_url_template()
        first_closure = self.url_closure("actual")
        changed_record = first_closure.metadata[0].__class__(
            **{
                **first_closure.metadata[0].__dict__,
                "contents": first_closure.metadata[0].contents + b"\n# changed\n",
            }
        )
        second_closure = core.ResolvedModClosure(
            first_closure.root_identity,
            (changed_record,),
        )
        with patch.object(core, "resolve_mod_closure", return_value=first_closure):
            first = core.TemplateImportSession.create("pack:demo", ["base"])
        first_fingerprint = first.plan.verifications[0].closure_fingerprint
        first_digest = first.plan.plan_digest
        first.discard()
        with patch.object(core, "resolve_mod_closure", return_value=second_closure):
            second = core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertNotEqual(
            first_fingerprint,
            second.plan.verifications[0].closure_fingerprint,
        )
        self.assertNotEqual(first_digest, second.plan.plan_digest)
        second.discard()

    def test_logical_divergence_template_selection_replaces_atomically(self) -> None:
        self.use_url_template()
        mods = self.source / "mods"
        mods.mkdir()
        (mods / "logical.pw.toml").write_bytes(
            url_metadata(
                "Installed",
                "old.jar",
                "https://mods.example/old.jar",
            )
        )
        with patch.object(
            core, "resolve_mod_closure", return_value=self.url_closure("actual-new")
        ):
            session = core.TemplateImportSession.create("pack:demo", ["base"])
        incoming = session.plan.template_candidates[0]
        conflict = session.plan.logical_identity_conflicts[0]
        resolved = resolve_template_import_plan(
            session.plan,
            logical_identity_resolutions={
                conflict.key: core.ConflictResolution((incoming.candidate_key,))
            },
        )
        operation = core.TemplateImportOperation(session, resolved)
        with patch.object(core, "run_resolver_process", side_effect=self.refresh_ok):
            operation.run()
            operation.apply()
        self.assertFalse((self.source / "mods/logical.pw.toml").exists())
        self.assertTrue((self.source / "mods/actual-new.pw.toml").is_file())


if __name__ == "__main__":
    unittest.main()
