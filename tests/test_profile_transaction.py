from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl
from pack_migration_roots import PackRootRecord, write_pack_root_manifest


SHA256 = "0" * 64
BAD_SHA256 = "1" * 64


def metadata(provider: str, project_id: str, side: str = "both") -> str:
    update = (
        f'[update.modrinth]\nmod-id = "{project_id}"\n'
        if provider == "modrinth"
        else f"[update.curseforge]\nproject-id = {project_id}\n"
    )
    return f'''name = "MOD {project_id}"
filename = "{project_id}.jar"
side = "{side}"
 [download]
url = "https://cdn.example/{project_id}.jar"
hash-format = "sha256"
hash = "{SHA256}"
{update}'''


class ProfileTransactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.source = self.packs / "demo" / "source"
        (self.source / "mods").mkdir(parents=True)
        (self.source / "pack.toml").write_text(
            'name = "Demo"\n[versions]\nminecraft = "1.21.1"\n'
            'neoforge = "21.1.234"\n',
            encoding="utf-8",
        )
        (self.source / "index.toml").write_bytes(b"original index\n")
        (self.packs / "demo" / "pack.yaml").write_text(
            "id: demo\ndisplay_name: Demo\nenabled: true\n", encoding="utf-8"
        )
        self.patches = [
            patch.object(core, "ROOT", self.root),
            patch.object(core, "PACKS", self.packs),
            patch.object(core, "STATE_ROOT", self.root / ".huroshiki"),
            patch.object(
                core,
                "TRANSACTION_ROOT",
                self.root / ".huroshiki" / "transactions",
            ),
            patch.object(packctl, "ROOT", self.root),
            patch.object(packctl, "PACKS", self.packs),
            patch.object(packctl, "SHARED", self.root / "shared"),
        ]
        for item in self.patches:
            item.start()
        self.key = core.project_key("pack", "demo")

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def snapshot(self) -> dict[Path, bytes | str]:
        result: dict[Path, bytes | str] = {}
        for path in sorted(self.source.rglob("*")):
            relative = path.relative_to(self.source)
            if path.is_symlink():
                result[relative] = f"symlink:{path.readlink()}"
            elif path.is_file():
                result[relative] = path.read_bytes()
        return result

    @staticmethod
    def profiles(*entries: dict[str, object]) -> dict[str, object]:
        return {"base": list(entries)}

    def install(self, events: list[str] | None = None):
        def run(command: list[str], *, cwd: Path | None = None) -> None:
            assert cwd is not None
            provider = command[2]
            project_id = command[-1]
            if events is not None:
                events.append(project_id)
            (cwd / "mods" / f"{project_id}.pw.toml").write_text(
                metadata(provider, project_id), encoding="utf-8"
            )

        return run

    def resolver(
        self,
        events: list[str] | None = None,
        fail_project: str | None = None,
    ):
        def resolve(*, provider, selector, **_):
            project_id = str(selector)
            if events is not None:
                events.append(project_id)
            if project_id == fail_project:
                raise core.HuroshikiError("resolver failed")
            contents = metadata(provider, project_id).encode("utf-8")
            identity = (provider, project_id)
            record = core.ResolvedMetadata(
                identity,
                Path("mods") / f"{project_id}.pw.toml",
                f"{project_id}.jar",
                contents,
                provider,
                project_id,
            )
            return core.ResolvedModClosure(identity, (record,))

        return resolve

    @staticmethod
    def refresh_success(command, *, cwd, **_):
        if command == ["packwiz", "refresh"]:
            (cwd / "index.toml").write_bytes(b"refreshed index\n")
        return core.ResolverProcessResult(0, "", "", False, False)

    def test_success_installs_only_in_copy_and_atomically_applies(self) -> None:
        original = self.snapshot()
        profile = self.profiles(
            {"source": "curseforge", "project": 101, "side": "client"},
            {"source": "curseforge", "project": 202, "side": "server"},
        )
        install_directories: list[Path] = []

        def install(command, *, cwd=None):
            self.assertEqual(self.snapshot(), original)
            self.assertNotEqual(cwd, self.source)
            install_directories.append(cwd)
            self.install()(command, cwd=cwd)

        with patch.object(core, "resolve_mod_closure", side_effect=self.resolver()), patch.object(
            core, "run_resolver_process", side_effect=self.refresh_success
        ) as run:
            core.apply_profiles(self.key, profile, ["base"])

        self.assertEqual(install_directories, [])
        self.assertEqual(run.call_count, 1)
        self.assertIn('side = "client"', (self.source / "mods/101.pw.toml").read_text())
        self.assertIn('side = "server"', (self.source / "mods/202.pw.toml").read_text())
        self.assertEqual((self.source / "index.toml").read_bytes(), b"refreshed index\n")

    def test_middle_install_failure_rolls_back_every_entry(self) -> None:
        original = self.snapshot()
        profile = self.profiles(
            {"source": "curseforge", "project": 101, "side": "client"},
            {"source": "curseforge", "project": 202, "side": "server"},
        )

        with patch.object(
            core,
            "resolve_mod_closure",
            side_effect=self.resolver(fail_project="202"),
        ):
            with self.assertRaisesRegex(
                core.HuroshikiError, r"Profile 'base' entry 2.*202"
            ):
                core.apply_profiles(self.key, profile, ["base"])
        self.assertEqual(self.snapshot(), original)

    def test_refresh_failure_and_interrupt_leave_source_unchanged(self) -> None:
        profile = self.profiles(
            {"source": "curseforge", "project": 101, "side": "client"}
        )
        for failure in (
            core.ResolverProcessResult(9, "", "refresh failed", False, False),
            KeyboardInterrupt(),
        ):
            with self.subTest(failure=type(failure).__name__):
                original = self.snapshot()

                def refresh(command, *, cwd, **_):
                    (cwd / "index.toml").write_bytes(b"partial\n")
                    if isinstance(failure, BaseException):
                        raise failure
                    return failure

                with patch.object(core, "resolve_mod_closure", side_effect=self.resolver()), patch.object(
                    core, "run_resolver_process", side_effect=refresh
                ):
                    expected = KeyboardInterrupt if isinstance(failure, KeyboardInterrupt) else core.HuroshikiError
                    with self.assertRaises(expected):
                        core.apply_profiles(self.key, profile, ["base"])
                self.assertEqual(self.snapshot(), original)

    def test_external_change_is_preserved_and_transaction_is_rejected(self) -> None:
        profile = self.profiles(
            {"source": "curseforge", "project": 101, "side": "client"}
        )

        def refresh(command, *, cwd, **_):
            (self.source / "index.toml").write_bytes(b"external index\n")
            return core.ResolverProcessResult(0, "", "", False, False)

        with patch.object(core, "resolve_mod_closure", side_effect=self.resolver()), patch.object(
            core, "run_resolver_process", side_effect=refresh
        ):
            with self.assertRaisesRegex(core.HuroshikiError, "real Packwiz source changed"):
                core.apply_profiles(self.key, profile, ["base"])
        self.assertEqual((self.source / "index.toml").read_bytes(), b"external index\n")
        self.assertFalse((self.source / "mods/101.pw.toml").exists())

    def test_lock_contention_does_not_run_entries(self) -> None:
        original = self.snapshot()
        profile = self.profiles(
            {"source": "curseforge", "project": 101, "side": "client"}
        )
        with packctl.ProjectLock(self.key, "other operation"), patch.object(
            core, "resolve_mod_closure"
        ) as install:
            with self.assertRaisesRegex(core.HuroshikiError, "Project is locked"):
                core.apply_profiles(self.key, profile, ["base"])
        install.assert_not_called()
        self.assertEqual(self.snapshot(), original)

    def test_template_project_is_rejected_before_transaction_creation(self) -> None:
        with patch.object(core.PackTransaction, "create") as create:
            with self.assertRaisesRegex(core.HuroshikiError, "only.*MODPACK"):
                core.apply_profiles("template:base", {}, [])
        create.assert_not_called()

    def test_pack_local_change_during_transaction_copy_aborts_profile_and_persists(self) -> None:
        original_copy = core.copy_transaction_source
        local = self.packs / "demo" / "pack.local.yaml"

        def racing_copy(source, destination, **kwargs):
            result = original_copy(source, destination, **kwargs)
            local.write_text("url_max_jar_size_bytes: 1024\n", encoding="utf-8")
            return result

        with patch.object(core, "copy_transaction_source", side_effect=racing_copy):
            with self.assertRaisesRegex(core.HuroshikiError, "while.*copy"):
                core.apply_profiles(self.key, self.profiles(), ["base"])
        self.assertEqual(local.read_text(encoding="utf-8"), "url_max_jar_size_bytes: 1024\n")

    def test_existing_identity_is_not_reinstalled_and_side_is_union(self) -> None:
        target = self.source / "mods/existing.pw.toml"
        target.write_text(metadata("curseforge", "101", "client"), encoding="utf-8")
        profile = self.profiles(
            {"source": "curseforge", "project": 101, "side": "server"}
        )
        with patch.object(core, "resolve_mod_closure", side_effect=self.resolver()) as install, patch.object(
            core, "run_resolver_process", side_effect=self.refresh_success
        ):
            core.apply_profiles(self.key, profile, ["base"])
        install.assert_called_once()
        self.assertIn('side = "both"', target.read_text(encoding="utf-8"))

    def test_cross_provider_dependency_collision_merges_with_side_union(self) -> None:
        mods = self.source / "mods"
        existing_root = mods / "existing-root.pw.toml"
        dependency = mods / "shared.pw.toml"
        existing_root.write_text(metadata("modrinth", "existing-root"), encoding="utf-8")
        dependency_contents = metadata("modrinth", "shared")
        dependency.write_text(dependency_contents, encoding="utf-8")
        incoming = core.ResolvedModClosure(
            ("curseforge", "101"),
            (
                core.ResolvedMetadata(
                    ("curseforge", "101"),
                    Path("mods/incoming-root.pw.toml"),
                    "incoming-root.jar",
                    metadata("curseforge", "101").encode(),
                    "curseforge",
                    "101",
                ),
                core.ResolvedMetadata(
                    ("curseforge", "202"),
                    Path("mods/shared.pw.toml"),
                    "shared.jar",
                    metadata("curseforge", "202", "server").replace(
                        'filename = "202.jar"', 'filename = "shared.jar"'
                    ).encode(),
                    "curseforge",
                    "202",
                ),
            ),
        )
        with patch.object(core, "resolve_mod_closure", return_value=incoming), patch.object(
            core, "run_resolver_process", side_effect=self.refresh_success
        ):
            core.apply_profiles(
                self.key,
                self.profiles(
                    {"source": "curseforge", "project": 101, "side": "server"}
                ),
                ["base"],
            )
        self.assertEqual(len(list(mods.glob("*.pw.toml"))), 3)
        self.assertEqual(
            dependency.read_text(encoding="utf-8"),
            dependency_contents.replace('side = "client"', 'side = "both"'),
        )
        self.assertFalse((self.source / ".huroshiki-roots.json").exists())

    def test_cross_provider_non_equivalent_dependency_collision_fails_closed(self) -> None:
        mods = self.source / "mods"
        dependency = mods / "shared.pw.toml"
        dependency.write_text(metadata("modrinth", "shared"), encoding="utf-8")
        write_pack_root_manifest(
            self.source,
            (PackRootRecord("modrinth", "existing-root", "client"),),
        )
        incoming = core.ResolvedModClosure(
            ("curseforge", "101"),
            (
                core.ResolvedMetadata(
                    ("curseforge", "101"), Path("mods/incoming-root.pw.toml"),
                    "incoming-root.jar", metadata("curseforge", "101").encode(),
                    "curseforge", "101",
                ),
                core.ResolvedMetadata(
                    ("curseforge", "202"), Path("mods/shared.pw.toml"),
                    "shared.jar", metadata("curseforge", "202").replace(
                        f'hash = "{SHA256}"', f'hash = "{BAD_SHA256}"'
                    ).encode(), "curseforge", "202",
                ),
            ),
        )
        before = self.snapshot()
        with patch.object(core, "resolve_mod_closure", return_value=incoming):
            with self.assertRaises(core.HuroshikiError):
                core.apply_profiles(
                    self.key,
                    self.profiles({"source": "curseforge", "project": 101, "side": "server"}),
                    ["base"],
                )
        self.assertEqual(self.snapshot(), before)

    def test_multiple_profiles_keep_order_and_roll_back_together(self) -> None:
        original = self.snapshot()
        profiles = {
            "first": [{"source": "curseforge", "project": 3, "side": "client"}],
            "second": [
                {"source": "curseforge", "project": 1, "side": "server"},
                {"source": "curseforge", "project": 2, "side": "both"},
            ],
        }
        events: list[str] = []

        with patch.object(
            core,
            "resolve_mod_closure",
            side_effect=self.resolver(events, fail_project="2"),
        ):
            with self.assertRaisesRegex(core.HuroshikiError, r"Profile 'second' entry 2"):
                core.apply_profiles(self.key, profiles, ["first", "second"])
        self.assertEqual(events, ["3", "1", "2"])
        self.assertEqual(self.snapshot(), original)

    def test_cli_reports_profile_and_entry_context(self) -> None:
        args = argparse.Namespace(pack="demo", names=["base"])
        profiles = self.profiles(
            {"source": "curseforge", "project": "invalid", "side": "client"}
        )
        with patch.object(packctl, "load_profiles", return_value=profiles):
            with self.assertRaisesRegex(
                packctl.ConfigError, r"Profile 'base' entry 1.*invalid"
            ):
                packctl.cmd_profile(args)


if __name__ == "__main__":
    unittest.main()
