from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from huroshiki_paths import resolve_root, root_argument


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY_ROOT / "shared" / "scripts"
PACKCTL = SCRIPTS / "packctl.py"
HUROSHIKI = SCRIPTS / "huroshiki.py"


class RootResolutionTest(unittest.TestCase):
    def make_root(self, parent: Path, name: str) -> Path:
        root = parent / name
        pack = root / "packs" / name
        pack.mkdir(parents=True)
        (pack / "pack.yaml").write_text(f"id: {name}\n", encoding="utf-8")
        return root

    def run_packctl(
        self, arguments: list[str], *, cwd: Path, environment_root: Path | None
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        if environment_root is None:
            env.pop("HUROSHIKI_ROOT", None)
        else:
            env["HUROSHIKI_ROOT"] = str(environment_root)
        return subprocess.run(
            [sys.executable, str(PACKCTL), *arguments],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def probe_huroshiki_roots(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        environment_root: Path | None,
    ) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(SCRIPTS)}
        if environment_root is None:
            env.pop("HUROSHIKI_ROOT", None)
        else:
            env["HUROSHIKI_ROOT"] = str(environment_root)
        return subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    f"sys.argv = ['huroshiki', *{arguments!r}]; "
                    "import huroshiki, huroshiki_core, packctl; "
                    "args = huroshiki.parse_args(); "
                    "print(args.root); print(huroshiki.ROOT); "
                    "print(huroshiki_core.ROOT); print(packctl.ROOT)"
                ),
            ],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_root_priority_is_cli_then_environment_then_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            cwd_root = self.make_root(temporary, "cwd")
            env_root = self.make_root(temporary, "environment")
            cli_root = self.make_root(temporary, "command-line")

            cli = self.run_packctl(
                ["--root", str(cli_root), "complete", "packs"],
                cwd=cwd_root,
                environment_root=env_root,
            )
            environment = self.run_packctl(
                ["complete", "packs"], cwd=cwd_root, environment_root=env_root
            )
            current = self.run_packctl(
                ["complete", "packs"], cwd=cwd_root, environment_root=None
            )

            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertEqual(cli.stdout, "command-line\n")
            self.assertEqual(environment.stdout, "environment\n")
            self.assertEqual(current.stdout, "cwd\n")

    def test_resolution_does_not_change_current_directory(self) -> None:
        before = Path.cwd()
        resolved = resolve_root("relative-root", cwd=before)
        self.assertEqual(Path.cwd(), before)
        self.assertEqual(resolved, before / "relative-root")
        self.assertEqual(root_argument(["--root", "first", "--root=second"]), "first")

    def test_bootstrap_scans_only_valid_global_root_arguments(self) -> None:
        self.assertIsNone(root_argument(["complete", "--root", "positional"]))
        self.assertIsNone(root_argument(["--", "--root", "positional"]))
        self.assertIsNone(root_argument(["--root", "--help"]))
        self.assertEqual(root_argument(["--help", "--root=selected"]), "selected")

    def test_positional_option_looking_value_cannot_redirect_mutation_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            managed = temporary / "managed"
            attacker = temporary / "attacker"
            managed.mkdir()
            result = self.run_packctl(
                [
                    "new",
                    "demo",
                    "Demo",
                    "1.21.1",
                    "neoforge",
                    "21.1.1",
                    f"--root={attacker}",
                ],
                cwd=temporary,
                environment_root=managed,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(attacker.exists())
            self.assertFalse((managed / "packs" / "demo").exists())

    def test_huroshiki_imports_share_cli_selected_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cases = [
                ["--root", str(root), "--pack", "demo"],
                ["--pack", "demo", "--root", str(root)],
                ["--root=" + str(root), "--template=demo"],
                ["--template=demo", "--root=" + str(root)],
            ]
            for arguments in cases:
                with self.subTest(arguments=arguments):
                    probe = self.probe_huroshiki_roots(
                        arguments,
                        cwd=Path("/"),
                        environment_root=None,
                    )

                    self.assertEqual(probe.returncode, 0, probe.stderr)
                    self.assertEqual(probe.stdout.splitlines(), [str(root)] * 4)

    def test_huroshiki_root_priority_is_cli_then_environment_then_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            cwd_root = temporary / "cwd"
            env_root = temporary / "environment"
            cli_root = temporary / "command-line"
            cwd_root.mkdir()
            cases = [
                (["--pack", "demo", "--root", str(cli_root)], env_root, cli_root),
                (["--pack", "demo"], env_root, env_root),
                (["--pack", "demo"], None, cwd_root),
            ]
            for arguments, environment_root, expected in cases:
                with self.subTest(arguments=arguments, expected=expected):
                    probe = self.probe_huroshiki_roots(
                        arguments,
                        cwd=cwd_root,
                        environment_root=environment_root,
                    )

                    self.assertEqual(probe.returncode, 0, probe.stderr)
                    self.assertEqual(probe.stdout.splitlines()[1:], [str(expected)] * 3)

    def test_huroshiki_selector_value_cannot_inject_root_option(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            environment_root = temporary / "managed"
            attacker = temporary / "attacker"
            probe = self.probe_huroshiki_roots(
                ["--pack=--root=" + str(attacker)],
                cwd=temporary,
                environment_root=environment_root,
            )

            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertEqual(probe.stdout.splitlines()[0], "None")
            self.assertEqual(probe.stdout.splitlines()[1:], [str(environment_root)] * 3)

    def test_huroshiki_bootstrap_stops_at_double_dash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            environment_root = temporary / "managed"
            attacker = temporary / "attacker"
            env = {
                **os.environ,
                "HUROSHIKI_ROOT": str(environment_root),
                "PYTHONPATH": str(SCRIPTS),
            }
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys; "
                        "sys.argv = ['huroshiki', '--pack', 'demo', '--', "
                        f"'--root={attacker}']; "
                        "import huroshiki, huroshiki_core, packctl; "
                        "print(huroshiki.ROOT); print(huroshiki_core.ROOT); "
                        "print(packctl.ROOT)"
                    ),
                ],
                cwd=temporary,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertEqual(probe.stdout.splitlines(), [str(environment_root)] * 3)

    def test_huroshiki_help_accepts_root_after_selector(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = subprocess.run(
                [
                    sys.executable,
                    str(HUROSHIKI),
                    "--template=demo",
                    "--root=" + temporary_directory,
                    "--help",
                ],
                cwd="/",
                env={**os.environ, "PYTHONPATH": str(SCRIPTS)},
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--root PATH", result.stdout)

    def test_installed_style_module_tree_runs_outside_source_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            installed = temporary / "lib" / "huroshiki"
            shutil.copytree(SCRIPTS, installed)
            external_root = self.make_root(temporary, "external")
            env = os.environ.copy()
            env.pop("HUROSHIKI_ROOT", None)

            huroshiki_help = subprocess.run(
                [sys.executable, str(installed / HUROSHIKI.name), "--root", str(external_root), "--help"],
                cwd="/",
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            packctl_help = subprocess.run(
                [sys.executable, str(installed / PACKCTL.name), "--root", str(external_root), "--help"],
                cwd="/",
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(huroshiki_help.returncode, 0, huroshiki_help.stderr)
            self.assertEqual(packctl_help.returncode, 0, packctl_help.stderr)
            self.assertTrue((installed / "huroshiki.tcss").is_file())
            self.assertTrue((installed / "huroshiki_core.py").is_file())


if __name__ == "__main__":
    unittest.main()
