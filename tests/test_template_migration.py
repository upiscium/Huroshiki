from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import packctl
import template_migration as migration


class TemplateMigrationCoreTest(unittest.TestCase):
    class _TerminationFailure(RuntimeError):
        termination_incomplete = True

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.templates = self.root / "templates"
        self.templates.mkdir()
        self.state = self.root / ".huroshiki"
        self.stack = patch.object(packctl, "ROOT", self.root)
        self.stack.start()
        for name, value in (("TEMPLATES", self.templates), ("STATE_ROOT", self.state),
                            ("TRANSACTION_ROOT", self.state / "transactions"),
                            ("LOG_ROOT", self.state / "logs"), ("TRASH_ROOT", self.state / "trash")):
            if hasattr(packctl, name):
                setattr(self, name, patch.object(packctl, name, value))
                getattr(self, name).start()
        self.source = self.templates / "base"
        self.source.mkdir()
        self._write_template(
            self.source,
            mods=[{"name": "Root", "provider": "modrinth", "project_id": "Abc123", "side": "client"}],
        )

    def tearDown(self) -> None:
        for name in ("TEMPLATES", "STATE_ROOT", "TRANSACTION_ROOT", "LOG_ROOT", "TRASH_ROOT"):
            value = getattr(self, name, None)
            if value is not None:
                value.stop()
        self.stack.stop()
        self.tmp.cleanup()

    def _write_template(self, directory: Path, *, mods: list[dict], loader: str = "neoforge",
                        minecraft: str = "1.21.1", reference: str = "21.1.0",
                        local: str | None = None, overrides: list[dict] | None = None) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        lines = [f"id: {directory.name}", "display_name: Base", "enabled: true",
                 f"minecraft: {minecraft}", f"loader: {loader}",
                 f"reference_loader_version: {reference}", "mods:"]
        for mod in mods:
            lines += [f"  - name: {mod['name']}", f"    provider: {mod['provider']}",
                      f"    project_id: {mod['project_id']}", f"    side: {mod['side']}"]
            if mod.get("url"):
                lines.append(f"    url: {mod['url']}")
        if overrides:
            lines.append("mod_version_overrides:")
            for item in overrides:
                lines += [f"  - provider: {item['provider']}", f"    project_id: {item['project_id']}",
                          f"    artifact_id: {item['artifact_id']}", f"    scope: {item['scope']}"]
        (directory / "template.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if local is not None:
            (directory / "template.local.yaml").write_text(local, encoding="utf-8")

    def target(self, *, loader: str = "neoforge", minecraft: str = "1.21.4",
               reference: str = "21.4.1") -> migration.TemplateMigrationTarget:
        return migration.TemplateMigrationTarget("next", "Next", minecraft, loader, reference)

    def plan(self, target: migration.TemplateMigrationTarget | None = None, **kwargs):
        return migration.plan_template_copy_migration_at(
            "base", target or self.target(), root=self.source,
            deadline=kwargs.pop("deadline", time.monotonic() + 30), **kwargs)

    @staticmethod
    def metadata(provider: str, project: str, artifact: str, path: str) -> SimpleNamespace:
        update = (f'[update.modrinth]\nmod-id = "{project}"\nversion = "{artifact}"\n'
                   if provider == "modrinth" else
                   f'[update.curseforge]\nproject-id = {project}\nfile-id = {artifact}\n')
        contents = (f'name = "{project}"\nfilename = "{path}.jar"\nside = "both"\n' + update).encode()
        return SimpleNamespace(relative_path=Path(path + ".pw.toml"), contents=contents,
                               identity=(provider, project))

    def resolver_patches(self, *, closure=None, calls=None):
        closure = closure or SimpleNamespace(
            root_identity=("modrinth", "Abc123"),
            metadata=(self.metadata("modrinth", "Abc123", "v1", "root"),),
        )
        calls = calls if calls is not None else []

        def resolve(**kwargs):
            calls.append(kwargs)
            return closure

        def create(root, **kwargs):
            (root / "mods").mkdir(parents=True, exist_ok=True)
            (root / "pack.toml").write_text("name = 'resolver'\n", encoding="utf-8")

        def merge(root, value, **kwargs):
            (root / "mods" / "root.pw.toml").write_bytes(value.metadata[0].contents)

        return patch.multiple(
            "huroshiki_core", resolve_mod_closure=resolve,
            resolve_project_selector=lambda provider, selector, **kwargs: SimpleNamespace(provider=provider, canonical_project_id=selector),
            resolved_closure_fingerprint=lambda value: "closure-digest",
            create_resolver_source=create, merge_metadata_closure=merge,
            run_noninteractive_packwiz=lambda argv, **kwargs: calls.append({"argv": argv, "cwd": kwargs["cwd"]}),
        ), calls

    def test_target_authority_and_reference_loader_are_forwarded(self) -> None:
        calls = []
        context, calls = self.resolver_patches(calls=calls)
        with context:
            for target in (self.target(loader="neoforge", reference="21.4.1"),
                           self.target(loader="fabric", minecraft="1.20.1", reference="0.16.5")):
                plan = self.plan(target)
                result = migration.resolve_template_migration_plan_at(plan)
                self.assertEqual(result.status, "resolved")
                migration.discard_template_migration_plan(plan)
        root_calls = [call for call in calls if "provider" in call]
        self.assertEqual((root_calls[0]["minecraft"], root_calls[0]["loader"], root_calls[0]["loader_version"]),
                         ("1.21.4", "neoforge", "21.4.1"))
        self.assertEqual((root_calls[1]["minecraft"], root_calls[1]["loader"], root_calls[1]["loader_version"]),
                         ("1.20.1", "fabric", "0.16.5"))

    def test_snapshot_is_path_independent_and_source_is_immutable(self) -> None:
        before = (self.source / "template.yaml").read_bytes()
        first = migration.snapshot_template_migration_source_at("base", self.source)
        other = self.root / "elsewhere" / "base"
        self._write_template(other, mods=[{"name": "Root", "provider": "modrinth", "project_id": "Abc123", "side": "client"}])
        second = migration.snapshot_template_migration_source_at("base", other)
        self.assertEqual(first.snapshot_digest, second.snapshot_digest)
        plan = self.plan()
        self.assertEqual(before, (self.source / "template.yaml").read_bytes())
        migration.discard_template_migration_plan(plan)
        self.assertEqual(before, (self.source / "template.yaml").read_bytes())

    def test_unsafe_symlink_and_special_entries_are_rejected(self) -> None:
        (self.source / "link").symlink_to(self.source / "template.yaml")
        with self.assertRaises(migration.TemplateMigrationError):
            migration.snapshot_template_migration_source_at("base", self.source)
        (self.source / "link").unlink()
        fifo = self.source / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(migration.TemplateMigrationError):
            migration.snapshot_template_migration_source_at("base", self.source)

    def test_manifest_symlink_and_fifo_fail_without_blocking(self) -> None:
        manifest = self.source / "template.yaml"
        original = manifest.read_bytes()
        manifest.unlink(); manifest.symlink_to(self.root / "outside.yaml")
        with self.assertRaises(migration.TemplateMigrationError):
            migration.snapshot_template_migration_source_at("base", self.source)
        manifest.unlink(); os.mkfifo(manifest)
        started = time.monotonic()
        with self.assertRaises(migration.TemplateMigrationError):
            migration.snapshot_template_migration_source_at("base", self.source)
        self.assertLess(time.monotonic() - started, 1)
        manifest.unlink(); manifest.write_bytes(original)

    def test_existing_target_is_no_clobber_and_locks_are_released(self) -> None:
        target = self.templates / "next"
        target.mkdir()
        (target / "template.yaml").write_text("sentinel", encoding="utf-8")
        with self.assertRaises(migration.TemplateMigrationError):
            self.plan()
        self.assertEqual((target / "template.yaml").read_text(), "sentinel")
        self.assertFalse(packctl.project_lock_is_active("template:base"))
        self.assertFalse(packctl.project_lock_is_active("template:next"))

    def test_order_and_sides_are_preserved_and_dependencies_are_not_persisted(self) -> None:
        self._write_template(self.source, mods=[
            {"name": "First", "provider": "modrinth", "project_id": "Abc123", "side": "client"},
            {"name": "Second", "provider": "curseforge", "project_id": "1234", "side": "server"},
        ])
        closure = SimpleNamespace(root_identity=("modrinth", "Abc123"), metadata=(
            self.metadata("modrinth", "Abc123", "v1", "root"),
            self.metadata("modrinth", "Dep999", "d1", "dependency")))
        calls = []
        context, _ = self.resolver_patches(closure=closure, calls=calls)
        second = SimpleNamespace(root_identity=("curseforge", "1234"), metadata=(
            self.metadata("curseforge", "1234", "1", "second"),))
        with context:
            with patch("huroshiki_core.resolve_mod_closure", side_effect=[closure, second]):
                plan = self.plan(); result = migration.resolve_template_migration_plan_at(plan)
        self.assertEqual(result.status, "resolved")
        self.assertEqual([(r.source_index, r.side) for r in result.resolved], [(0, "client"), (1, "server")])
        config = (plan._state.staging / "template.yaml").read_text()
        self.assertNotIn("Dep999", config)
        migration.discard_template_migration_plan(plan)

    def test_event_and_deadline_must_be_plan_identity(self) -> None:
        event = threading.Event()
        plan = self.plan(cancel_event=event)
        with self.assertRaises(migration.TemplateMigrationOperationError):
            migration.resolve_template_migration_plan_at(plan, cancel_event=threading.Event())
        with self.assertRaises(migration.TemplateMigrationOperationError):
            migration.resolve_template_migration_plan_at(plan, deadline=plan.deadline + 1)
        migration.discard_template_migration_plan(plan)

    def test_root_exact_intent_is_resolved_and_persisted(self) -> None:
        self._write_template(self.source, mods=[
            {"name": "Root", "provider": "modrinth", "project_id": "Project1", "side": "both"}],
            overrides=[{"provider": "modrinth", "project_id": "Project1",
                        "artifact_id": "Version1", "scope": "root"}])
        closure = SimpleNamespace(root_identity=("modrinth", "Project1"), metadata=(
            self.metadata("modrinth", "Project1", "Version1", "root"),))
        context, _ = self.resolver_patches(closure=closure)
        with context, patch("huroshiki_core.exact_mod_artifact_selection", return_value=SimpleNamespace()), \
             patch("huroshiki_core.resolve_exact_mod_closure", return_value=closure) as exact, \
             patch("huroshiki_core.verify_exact_mod_metadata"):
            plan = self.plan(); result = migration.resolve_template_migration_plan_at(plan)
        self.assertEqual(result.status, "resolved")
        exact.assert_called_once()
        manifest = (plan._state.staging / "template.yaml").read_text()
        self.assertIn("artifact_id: Version1", manifest)
        self.assertIn("scope: root", manifest)
        migration.discard_template_migration_plan(plan)

    def test_root_exact_unavailable_is_version_intent_blocked(self) -> None:
        self._write_template(self.source, mods=[
            {"name": "Root", "provider": "modrinth", "project_id": "Project1", "side": "both"}],
            overrides=[{"provider": "modrinth", "project_id": "Project1",
                        "artifact_id": "Version1", "scope": "root"}])
        context, _ = self.resolver_patches()
        with context, patch("huroshiki_core.exact_mod_artifact_selection", return_value=SimpleNamespace()), \
             patch("huroshiki_core.resolve_exact_mod_closure", side_effect=RuntimeError("artifact unavailable")):
            plan = self.plan(); result = migration.resolve_template_migration_plan_at(plan)
        self.assertEqual(result.status, "resolution-required")
        self.assertEqual(result.unresolved[0].code, "version-intent-blocked")
        self.assertEqual(result.unresolved[0].version_issue, "Version1")
        migration.discard_template_migration_plan(plan)

    def test_dependency_exact_intent_constrains_but_never_becomes_root(self) -> None:
        self._write_template(self.source, mods=[
            {"name": "Root", "provider": "modrinth", "project_id": "Project1", "side": "both"}],
            overrides=[{"provider": "modrinth", "project_id": "Depends1",
                        "artifact_id": "DepVer01", "scope": "dependency"}])
        compatible = SimpleNamespace(root_identity=("modrinth", "Project1"), metadata=(
            self.metadata("modrinth", "Project1", "Version1", "root"),
            self.metadata("modrinth", "Depends1", "DepVer01", "dependency")))
        context, _ = self.resolver_patches(closure=compatible)
        with context:
            plan = self.plan(); result = migration.resolve_template_migration_plan_at(plan)
        self.assertEqual(result.status, "resolved")
        manifest = (plan._state.staging / "template.yaml").read_text()
        self.assertIn("scope: dependency", manifest)
        self.assertNotIn("name: Depends1", manifest)
        migration.discard_template_migration_plan(plan)

        automatic = SimpleNamespace(root_identity=("modrinth", "Project1"), metadata=(
            self.metadata("modrinth", "Project1", "Version1", "root"),
            self.metadata("modrinth", "Depends1", "OtherVer", "dependency")))
        context, _ = self.resolver_patches(closure=automatic)
        with context, patch("huroshiki_core.exact_mod_artifact_selection", side_effect=lambda *args: args), \
             patch("huroshiki_core.resolve_exact_mod_closure", return_value=compatible) as constrained:
            plan = self.plan(); result = migration.resolve_template_migration_plan_at(plan)
        self.assertEqual(result.status, "resolved")
        self.assertEqual(constrained.call_args.kwargs["preseed_selections"],
                         (("modrinth", "Depends1", "DepVer01"),))
        migration.discard_template_migration_plan(plan)

        context, _ = self.resolver_patches(closure=automatic)
        with context, patch("huroshiki_core.exact_mod_artifact_selection", side_effect=lambda *args: args), \
             patch("huroshiki_core.resolve_exact_mod_closure", side_effect=RuntimeError("exact dependency unavailable")):
            plan = self.plan(); result = migration.resolve_template_migration_plan_at(plan)
        self.assertEqual(result.status, "resolution-required")
        self.assertEqual(result.unresolved[0].code, "version-intent-blocked")
        migration.discard_template_migration_plan(plan)

    def test_source_stale_and_staging_digest_mutation_fail_closed(self) -> None:
        plan = self.plan()
        context, _ = self.resolver_patches()
        with context:
            result = migration.resolve_template_migration_plan_at(plan)
        (plan._state.staging / "template.yaml").write_bytes(
            (plan._state.staging / "template.yaml").read_bytes() + b"# mutated\n"
        )
        with self.assertRaises(migration.TemplateMigrationOperationError):
            migration.prepare_template_migration_publication(plan, result)
        migration.discard_template_migration_plan(plan)

        plan = self.plan()
        with context:
            result = migration.resolve_template_migration_plan_at(plan)
        (self.source / "template.yaml").write_bytes((self.source / "template.yaml").read_bytes() + b"# stale\n")
        with self.assertRaises(migration.TemplateMigrationOperationError):
            migration.prepare_template_migration_publication(plan, result)
        migration.discard_template_migration_plan(plan)

    def test_staging_replacement_and_target_appearance_fail_closed(self) -> None:
        plan = self.plan(); context, _ = self.resolver_patches()
        with context:
            result = migration.resolve_template_migration_plan_at(plan)
        replaced = plan._state.tx / "replaced-staging"
        plan._state.staging.rename(replaced); plan._state.staging.mkdir()
        with self.assertRaises(migration.TemplateMigrationOperationError):
            migration.prepare_template_migration_publication(plan, result)
        plan._state.staging.rmdir(); replaced.rename(plan._state.staging)
        publication = migration.prepare_template_migration_publication(plan, result)
        (self.templates / "next").mkdir()
        with self.assertRaises(migration.TemplateMigrationOperationError):
            migration.apply_template_migration_publication(publication)
        (self.templates / "next").rmdir()
        migration.discard_template_migration_plan(plan)

    def test_resolver_cancellation_and_deadline_leave_target_absent(self) -> None:
        event = threading.Event(); plan = self.plan(cancel_event=event)
        def cancelled(**kwargs):
            event.set(); raise RuntimeError("resolution cancelled")
        with patch("huroshiki_core.resolve_project_selector", side_effect=cancelled):
            with self.assertRaises(migration.TemplateMigrationOperationError):
                migration.resolve_template_migration_plan_at(plan)
        self.assertFalse((self.templates / "next").exists())
        event.clear(); migration.discard_template_migration_plan(plan, deadline=time.monotonic() + 30)

        plan = self.plan(); plan._state.deadline = time.monotonic() - 1
        with self.assertRaises(migration.TemplateMigrationOperationError):
            migration.resolve_template_migration_plan_at(plan)
        self.assertFalse((self.templates / "next").exists())
        migration.discard_template_migration_plan(plan, deadline=time.monotonic() + 30)

    def test_unresolved_result_never_publishes(self) -> None:
        plan = self.plan()
        with patch("huroshiki_core.resolve_mod_closure", side_effect=RuntimeError("no compatible file")):
            result = migration.resolve_template_migration_plan_at(plan)
        self.assertEqual(result.status, "resolution-required")
        self.assertFalse((self.templates / "next").exists())
        with self.assertRaises(migration.TemplateMigrationOperationError):
            migration.prepare_template_migration_publication(plan, result)
        migration.discard_template_migration_plan(plan)

    def test_url_compatibility_unknown_and_policy_are_recorded(self) -> None:
        self._write_template(self.source, mods=[{"name": "Jar", "provider": "url",
            "project_id": "jar", "side": "both", "url": "https://example.invalid/jar.jar"}],
            local="url_max_jar_size_bytes: 12345\nurl_allow_private_networks: true\n")
        def url_closure(loaders: str, versions: str):
            contents = ("name = 'jar'\nfilename = 'jar.jar'\nside = 'both'\n"
                        f"[huroshiki]\nloaders = [{loaders}]\nminecraft-versions = [{versions}]\n").encode()
            return SimpleNamespace(root_identity=("url", "jar"), metadata=(
                SimpleNamespace(relative_path=Path("jar.pw.toml"), contents=contents,
                                 identity=("url", "jar")),))
        compatible = url_closure('"neoforge"', '"1.21.4"')
        context, calls = self.resolver_patches(closure=compatible, calls=[])
        with context:
            plan = self.plan(); result = migration.resolve_template_migration_plan_at(plan)
        self.assertEqual(result.url_evidence[0].status, "compatible")
        self.assertEqual(result.url_evidence[0].effective_max_size_bytes, 12345)
        self.assertTrue(result.url_evidence[0].effective_allow_private_networks)
        self.assertEqual(calls[0]["url_max_jar_size_bytes"], 12345)
        self.assertTrue(calls[0]["url_allow_private_networks"])
        migration.discard_template_migration_plan(plan)

        for loaders, versions, expected in [('"fabric"', '"1.21.4"', "incompatible"),
                                             ('"neoforge"', '"future"', "unknown")]:
            closure = url_closure(loaders, versions)
            with patch("huroshiki_core.resolve_mod_closure", return_value=closure):
                plan = self.plan()
                result = migration.resolve_template_migration_plan_at(plan)
            self.assertEqual(result.url_evidence[0].status, expected)
            self.assertEqual(result.status, "resolution-required")
            migration.discard_template_migration_plan(plan)

    def test_metadata_collision_and_resolver_termination_failure_do_not_publish(self) -> None:
        plan = self.plan()
        context, _ = self.resolver_patches()
        with context:
            with patch("huroshiki_core.merge_metadata_closure", side_effect=RuntimeError("metadata collision")):
                with self.assertRaises(RuntimeError):
                    migration.resolve_template_migration_plan_at(plan)
        self.assertFalse((self.templates / "next").exists())
        migration.discard_template_migration_plan(plan)

        plan = self.plan()
        bad = self._TerminationFailure("resolver did not terminate")
        context, _ = self.resolver_patches()
        with context:
            with patch("huroshiki_core.resolve_mod_closure", side_effect=bad):
                with self.assertRaises(migration.TemplateMigrationOperationError):
                    migration.resolve_template_migration_plan_at(plan)
        migration.discard_template_migration_plan(plan)

    def test_publication_precommit_failure_leaves_target_absent(self) -> None:
        plan = self.plan(); context, _ = self.resolver_patches()
        with context:
            result = migration.resolve_template_migration_plan_at(plan)
        publication = migration.prepare_template_migration_publication(plan, result)
        with patch.object(packctl, "renameat2", side_effect=OSError("precommit")):
            with self.assertRaises(migration.TemplateMigrationOperationError):
                migration.apply_template_migration_publication(publication)
        self.assertFalse((self.templates / "next").exists())
        migration.discard_template_migration_plan(plan)

    def test_atomic_publication_and_cleanup_retry_preserve_published_target(self) -> None:
        plan = self.plan(); context, _ = self.resolver_patches()
        with context:
            result = migration.resolve_template_migration_plan_at(plan)
        publication = migration.prepare_template_migration_publication(plan, result)
        original = migration._cleanup
        with patch.object(migration, "_cleanup", side_effect=migration.TemplateMigrationOperationError("cleanup")):
            with self.assertRaises(migration.TemplateMigrationOperationError):
                migration.apply_template_migration_publication(publication)
        self.assertTrue((self.templates / "next" / "template.yaml").is_file())
        self.assertTrue(packctl.project_lock_is_active("template:base"))
        with patch.object(migration, "_cleanup", original):
            migration.retry_template_migration_cleanup(publication, deadline=time.monotonic() + 30)
        self.assertFalse(packctl.project_lock_is_active("template:base"))

    def test_postcommit_verification_failure_retains_cleanup_retry(self) -> None:
        plan = self.plan(); context, _ = self.resolver_patches()
        with context:
            result = migration.resolve_template_migration_plan_at(plan)
        publication = migration.prepare_template_migration_publication(plan, result)
        original = migration.snapshot_template_migration_source_at
        calls = 0
        def fail_published(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise migration.TemplateMigrationOperationError("verification failed")
            return original(*args, **kwargs)
        with patch.object(migration, "snapshot_template_migration_source_at", side_effect=fail_published):
            with self.assertRaises(migration.TemplateMigrationOperationError):
                migration.apply_template_migration_publication(publication)
        self.assertTrue((self.templates / "next" / "template.yaml").is_file())
        self.assertIsNotNone(plan._state.cleanup_error)
        migration.retry_template_migration_cleanup(publication, deadline=time.monotonic() + 30)


if __name__ == "__main__":
    unittest.main()
