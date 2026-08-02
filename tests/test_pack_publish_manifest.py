from __future__ import annotations

import threading
import os
import hashlib
import shutil
from pathlib import Path
import tempfile
import tomlkit
import tomllib
import unittest
from unittest.mock import patch

import pack_publish
import packctl


class PackPublishManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.pack = self.packs / "demo"
        source = self.pack / "source" / "mods"
        source.mkdir(parents=True)
        (self.pack / "content" / "common").mkdir(parents=True)
        (self.pack / "content" / "server").mkdir(parents=True)
        (self.pack / "content" / "client").mkdir(parents=True)
        (self.pack / "pack.yaml").write_text(
            "id: demo\ndisplay_name: Demo\nenabled: true\n", encoding="utf-8"
        )
        (self.pack / "source" / "pack.toml").write_text(
            'name = "Demo"\nversion = "1"\npack-format = "packwiz:1.1.0"\n'
            '[index]\nfile = "index.toml"\nhash-format = "sha256"\n'
            'hash = "placeholder"\n'
            '[versions]\nminecraft = "1.21.1"\nneoforge = "21.1.0"\n', encoding="utf-8"
        )
        (self.pack / "source" / "index.toml").write_text(
            'hash-format = "sha256"\n', encoding="utf-8"
        )
        (source / "server.pw.toml").write_text(
            'filename = "server.jar"\nside = "server"\n'
            '[update.modrinth]\nmod-id = "abc"\nversion = "v1"\n', encoding="utf-8"
        )
        (source / "client.pw.toml").write_text(
            'filename = "client.jar"\nside = "client"\n'
            '[update.modrinth]\nmod-id = "client"\nversion = "v1"\n', encoding="utf-8"
        )
        (self.pack / "source" / "README.md").write_bytes(b"ordinary indexed source")
        self.write_index()
        (self.pack / "content" / "common" / "config.txt").write_bytes(b"common")
        (self.pack / "content" / "server" / "server.cfg").write_bytes(b"server")
        self.patches = [patch.object(packctl, "ROOT", self.root), patch.object(packctl, "PACKS", self.packs)]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def write_index(self) -> None:
        records = []
        indexed = sorted(
            path for path in (self.pack / "source").rglob("*")
            if path.is_file() and path.name != "pack.toml" and path.name != "index.toml"
        )
        for metadata in indexed:
            relative = metadata.relative_to(self.pack / "source").as_posix()
            digest = hashlib.sha256(metadata.read_bytes()).hexdigest()
            records.append(
                f'[[files]]\nfile = "{relative}"\nmetafile = true\n'
                f'hash = "{digest}"\n'
                if metadata.name.endswith(".pw.toml")
                else f'[[files]]\nfile = "{relative}"\nhash = "{digest}"\n'
            )
        index = ('hash-format = "sha256"\n' + "".join(records)).encode("utf-8")
        (self.pack / "source" / "index.toml").write_bytes(index)
        self.update_pack_index_hash()

    def update_pack_index_hash(self) -> None:
        pack_path = self.pack / "source" / "pack.toml"
        index = (self.pack / "source" / "index.toml").read_bytes()
        document = tomlkit.parse(pack_path.read_text(encoding="utf-8"))
        document["index"]["hash"] = hashlib.sha256(index).hexdigest()
        pack_path.write_text(tomlkit.dumps(document), encoding="utf-8")

    def manifest_entries(self, manifest: object) -> dict[str, pack_publish.PublishFileEntry]:
        return {entry.relative_path.as_posix(): entry for entry in manifest.files}

    def generated_contents(self, manifest: object, path: str) -> bytes:
        entry = self.manifest_entries(manifest)[path]
        self.assertEqual(entry.source_kind, "generated")
        self.assertIsNotNone(entry.contents)
        return entry.contents or b""

    def generated_index_records(self, manifest: object) -> dict[str, dict[str, object]]:
        index = tomllib.loads(self.generated_contents(manifest, "index.toml").decode("utf-8"))
        return {record["file"]: record for record in index.get("files", [])}

    def test_server_manifest_is_side_aware_and_deterministic(self) -> None:
        first = pack_publish.plan_pack_publish_manifest("demo")
        second = pack_publish.plan_pack_publish_manifest("demo")
        paths = {str(entry.relative_path) for entry in first.files}
        self.assertIn("pack.toml", paths)
        self.assertIn("config.txt", paths)
        self.assertIn("server.cfg", paths)
        self.assertNotIn("mods/client.pw.toml", paths)
        self.assertEqual(
            (first.minecraft_version, first.loader, first.loader_version),
            ("1.21.1", "neoforge", "21.1.0"),
        )
        self.assertEqual(first.manifest_digest, second.manifest_digest)
        self.assertEqual(first.source_snapshot_digest, second.source_snapshot_digest)

        records = self.generated_index_records(first)
        entries = self.manifest_entries(first)
        for path, record in records.items():
            self.assertIn(path, entries)
            self.assertEqual(record["hash"], entries[path].sha256)
        for path, entry in entries.items():
            if entry.source_kind == "packwiz" and path not in {"pack.toml", "index.toml"}:
                self.assertIn(path, records)
        self.assertIn("README.md", records)
        self.assertEqual(records["README.md"].get("metafile", False), False)
        pack = tomllib.loads(self.generated_contents(first, "pack.toml").decode("utf-8"))
        index_bytes = self.generated_contents(first, "index.toml")
        self.assertEqual(pack["index"]["hash"], hashlib.sha256(index_bytes).hexdigest())
        self.assertEqual(first.files, second.files)
        self.assertEqual(index_bytes, self.generated_contents(second, "index.toml"))
        self.assertEqual(self.generated_contents(first, "pack.toml"), self.generated_contents(second, "pack.toml"))

    def test_client_selection_and_target_side_digest(self) -> None:
        client = pack_publish.plan_pack_publish_manifest("demo", target_side="client")
        server = pack_publish.plan_pack_publish_manifest("demo", target_side="server")
        paths = {str(entry.relative_path) for entry in client.files}
        self.assertIn("config.txt", paths)
        self.assertNotIn("server.cfg", paths)
        self.assertNotEqual(client.manifest_digest, server.manifest_digest)
        for manifest in (client, server):
            entries = self.manifest_entries(manifest)
            records = self.generated_index_records(manifest)
            self.assertIn("README.md", entries)
            self.assertIn("README.md", records)
        self.assertNotIn("mods/server.pw.toml", self.manifest_entries(client))
        self.assertNotIn("mods/server.pw.toml", self.generated_index_records(client))
        self.assertNotIn("mods/client.pw.toml", self.manifest_entries(server))
        self.assertNotIn("mods/client.pw.toml", self.generated_index_records(server))

    def test_common_and_side_duplicate_destination_fails(self) -> None:
        (self.pack / "content" / "common" / "same.txt").write_bytes(b"common")
        (self.pack / "content" / "server" / "same.txt").write_bytes(b"server")
        with self.assertRaises(pack_publish.PackPublishError):
            pack_publish.plan_pack_publish_manifest("demo")

    def test_dist_is_not_published(self) -> None:
        before = pack_publish.plan_pack_publish_manifest("demo")
        (self.pack / "dist").mkdir()
        (self.pack / "dist" / "secret.txt").write_bytes(b"not publication input")
        manifest = pack_publish.plan_pack_publish_manifest("demo")
        self.assertNotIn("dist/secret.txt", {str(entry.relative_path) for entry in manifest.files})
        self.assertEqual(before.manifest_digest, manifest.manifest_digest)
        self.assertEqual(before.source_snapshot_digest, manifest.source_snapshot_digest)

    def test_deadline_and_progress_callback_are_bounded(self) -> None:
        phases: list[str] = []
        manifest = pack_publish.plan_pack_publish_manifest("demo", progress=lambda phase: (phases.append(phase), 1 / 0)[1])
        self.assertTrue(manifest.files)
        self.assertEqual(set(phases), {"snapshotting", "validating-config", "validating-packwiz", "validating-content", "building-manifest"})
        with self.assertRaises(pack_publish.PackPublishDeadlineExceeded):
            pack_publish.plan_pack_publish_manifest("demo", deadline=0)

    def test_cancelled_before_snapshot(self) -> None:
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(pack_publish.PackPublishCancelled):
            pack_publish.plan_pack_publish_manifest("demo", cancel_event=cancelled)

    def test_symlink_is_rejected(self) -> None:
        link = self.pack / "content" / "server" / "secret"
        link.symlink_to(self.root / "outside")
        with self.assertRaises(pack_publish.PackPublishError):
            pack_publish.plan_pack_publish_manifest("demo")

    def test_hardlink_and_invalid_side_are_rejected(self) -> None:
        os.link(self.pack / "content" / "common" / "config.txt", self.pack / "content" / "common" / "alias.txt")
        with self.assertRaises(pack_publish.PackPublishError):
            pack_publish.plan_pack_publish_manifest("demo")
        (self.pack / "content" / "common" / "alias.txt").unlink()
        metadata = self.pack / "source" / "mods" / "server.pw.toml"
        metadata.write_text(metadata.read_text().replace('side = "server"', 'side = "invalid"'))
        self.write_index()
        with self.assertRaises(pack_publish.PackPublishError):
            pack_publish.plan_pack_publish_manifest("demo")

    def test_missing_and_invalid_index_are_rejected(self) -> None:
        index = self.pack / "source" / "index.toml"
        index.unlink()
        with self.assertRaises(pack_publish.PackPublishError):
            pack_publish.plan_pack_publish_manifest("demo")
        index.write_text('hash-format = "sha256"\nfiles = "invalid"\n')
        self.update_pack_index_hash()
        with self.assertRaises(pack_publish.PackPublishError):
            pack_publish.plan_pack_publish_manifest("demo")

    def test_empty_index_is_supported(self) -> None:
        shutil.rmtree(self.pack / "source" / "mods")
        (self.pack / "source" / "README.md").unlink()
        (self.pack / "source" / "index.toml").write_text(
            'hash-format = "sha256"\n', encoding="utf-8"
        )
        self.update_pack_index_hash()
        manifest = pack_publish.plan_pack_publish_manifest("demo")
        self.assertEqual(self.generated_index_records(manifest), {})
        self.assertEqual(
            {
                path
                for path, entry in self.manifest_entries(manifest).items()
                if entry.source_kind in {"packwiz", "generated"}
            },
            {"pack.toml", "index.toml"},
        )

    def test_incomplete_or_stale_pack_index_descriptor_is_rejected(self) -> None:
        path = self.pack / "source" / "pack.toml"
        original = path.read_text(encoding="utf-8")
        missing = original.replace(
            '[index]\nfile = "index.toml"\nhash-format = "sha256"\n'
            'hash = "'
            + hashlib.sha256(
                (self.pack / "source" / "index.toml").read_bytes()
            ).hexdigest()
            + '"\n',
            "",
        )
        path.write_text(missing, encoding="utf-8")
        with self.assertRaises(pack_publish.PackPublishError):
            pack_publish.plan_pack_publish_manifest("demo")
        path.write_text(original, encoding="utf-8")
        path.write_text(original.replace('hash-format = "sha256"\n', ""), encoding="utf-8")
        with self.assertRaises(pack_publish.PackPublishError):
            pack_publish.plan_pack_publish_manifest("demo")
        digest = hashlib.sha256(
            (self.pack / "source" / "index.toml").read_bytes()
        ).hexdigest()
        path.write_text(
            original.replace(f'hash = "{digest}"', 'hash = "' + "0" * 64 + '"'),
            encoding="utf-8",
        )
        with self.assertRaises(pack_publish.PackPublishError):
            pack_publish.plan_pack_publish_manifest("demo")

    def test_index_hash_mismatch_is_rejected(self) -> None:
        metadata = self.pack / "source" / "mods" / "server.pw.toml"
        (self.pack / "source" / "index.toml").write_text(
            'hash-format = "sha256"\n[[files]]\nfile = "mods/server.pw.toml"\n'
            'hash = "' + ("0" * 64) + '"\n'
        )
        self.update_pack_index_hash()
        self.assertNotEqual(hashlib.sha256(metadata.read_bytes()).hexdigest(), "0" * 64)
        with self.assertRaises(pack_publish.PackPublishError):
            pack_publish.plan_pack_publish_manifest("demo")

    def test_invalid_config_and_local_override_are_rejected(self) -> None:
        config = self.pack / "pack.yaml"
        original = config.read_text()
        config.write_text(original.replace("enabled: true", 'enabled: "invalid"'))
        with self.assertRaises(pack_publish.PackPublishError):
            pack_publish.plan_pack_publish_manifest("demo")
        config.write_text(original)
        (self.pack / "pack.local.yaml").write_text("minecraft:\n  version: 1.20.1\n")
        with self.assertRaises(pack_publish.PackPublishError):
            pack_publish.plan_pack_publish_manifest("demo")

    def test_duplicate_provider_identity_and_filename_are_rejected(self) -> None:
        mods = self.pack / "source" / "mods"
        duplicate = mods / "duplicate.pw.toml"
        duplicate.write_text(
            'filename = "different.jar"\nside = "client"\n'
            '[update.modrinth]\nmod-id = "abc"\nversion = "v2"\n'
        )
        with self.assertRaisesRegex(pack_publish.PackPublishError, "duplicate provider identity"):
            pack_publish.plan_pack_publish_manifest("demo")
        duplicate.write_text(
            'filename = "SERVER.JAR"\nside = "client"\n'
            '[update.modrinth]\nmod-id = "different"\nversion = "v2"\n'
        )
        with self.assertRaisesRegex(pack_publish.PackPublishError, "filename collision"):
            pack_publish.plan_pack_publish_manifest("demo")

    def test_packwiz_owned_content_destination_is_rejected(self) -> None:
        owned = self.pack / "content" / "server" / "mods"
        owned.mkdir()
        (owned / "server.pw.toml").write_text("not metadata")
        with self.assertRaises(pack_publish.PackPublishError):
            pack_publish.plan_pack_publish_manifest("demo")

    def test_content_collision_with_packwiz_jar_destination_is_rejected(self) -> None:
        mods = self.pack / "content" / "server" / "mods"
        mods.mkdir()
        (mods / "server.jar").write_bytes(b"overlay")
        with self.assertRaisesRegex(pack_publish.PackPublishError, "destination collision"):
            pack_publish.plan_pack_publish_manifest("demo")

    def test_project_root_symlink_is_rejected(self) -> None:
        physical = self.packs / "physical"
        self.pack.rename(physical)
        self.pack.symlink_to(physical, target_is_directory=True)
        with self.assertRaises(pack_publish.PackPublishError):
            pack_publish.plan_pack_publish_manifest("demo")

    def test_unsafe_and_changing_dist_are_irrelevant(self) -> None:
        dist = self.pack / "dist"
        dist.mkdir()
        (dist / "loop").symlink_to(self.root / "outside")

        def mutate_dist(phase: str) -> None:
            if phase == "validating-config":
                (dist / "generated.txt").write_text("changed")

        manifest = pack_publish.plan_pack_publish_manifest("demo", progress=mutate_dist)
        self.assertTrue(manifest.files)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO test requires POSIX")
    def test_special_file_is_rejected(self) -> None:
        os.mkfifo(self.pack / "content" / "server" / "blocked")
        with self.assertRaises(pack_publish.PackPublishError):
            pack_publish.plan_pack_publish_manifest("demo")

    def test_source_mutation_during_planning_is_rejected(self) -> None:
        source_toml = self.pack / "source" / "pack.toml"

        def mutate(phase: str) -> None:
            if phase == "validating-config":
                source_toml.write_text(source_toml.read_text() + "\n# changed\n")

        with self.assertRaises(pack_publish.PackPublishError):
            pack_publish.plan_pack_publish_manifest("demo", progress=mutate)

    def test_project_directory_replacement_is_rejected(self) -> None:
        original_scan = pack_publish.scan_pack_migration_source
        replacement = self.packs / "replacement"

        def replace_after_scan(*args: object, **kwargs: object):
            scan = original_scan(*args, **kwargs)
            if not replacement.exists():
                self.pack.rename(replacement)
                shutil.copytree(replacement, self.pack)
            return scan

        with patch.object(pack_publish, "scan_pack_migration_source", side_effect=replace_after_scan):
            with self.assertRaises(pack_publish.PackPublishError):
                pack_publish.plan_pack_publish_manifest("demo")

    def test_same_semantic_tree_under_different_roots_is_deterministic(self) -> None:
        first = pack_publish.plan_pack_publish_manifest("demo")
        other_root = self.root / "other"
        other_packs = other_root / "packs"
        other_packs.mkdir(parents=True)
        shutil.copytree(self.pack, other_packs / "demo")
        with patch.object(packctl, "ROOT", other_root), patch.object(packctl, "PACKS", other_packs):
            second = pack_publish.plan_pack_publish_manifest("demo")
        self.assertEqual(first.source_snapshot_digest, second.source_snapshot_digest)
        self.assertEqual(first.manifest_digest, second.manifest_digest)

    def test_content_and_mode_changes_alter_manifest_digest(self) -> None:
        first = pack_publish.plan_pack_publish_manifest("demo")
        content = self.pack / "content" / "server" / "server.cfg"
        content.write_bytes(b"changed")
        second = pack_publish.plan_pack_publish_manifest("demo")
        self.assertNotEqual(first.manifest_digest, second.manifest_digest)
        content.chmod(0o600)
        third = pack_publish.plan_pack_publish_manifest("demo")
        self.assertNotEqual(second.manifest_digest, third.manifest_digest)

    def test_cancellation_during_snapshot_streaming_is_bounded(self) -> None:
        (self.pack / "content" / "server" / "large.bin").write_bytes(
            b"x" * (pack_publish._CHUNK * 3)
        )
        cancelled = threading.Event()
        original_read = os.read
        calls = 0

        def cancelling_read(fd: int, size: int) -> bytes:
            nonlocal calls
            result = original_read(fd, size)
            calls += 1
            if calls == 2:
                cancelled.set()
            return result

        with patch("pack_tree_policy.os.read", side_effect=cancelling_read):
            with self.assertRaises(pack_publish.PackPublishCancelled):
                pack_publish.plan_pack_publish_manifest("demo", cancel_event=cancelled)


if __name__ == "__main__":
    unittest.main()
