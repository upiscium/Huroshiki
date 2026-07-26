import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ShellEnvironmentTest(unittest.TestCase):
    def test_direnv_does_not_export_fpath(self) -> None:
        envrc = (ROOT / ".envrc").read_text(encoding="utf-8")

        self.assertEqual(envrc.strip(), "use flake")
        self.assertNotIn("FPATH", envrc)

    def test_dev_shell_does_not_modify_fpath(self) -> None:
        flake = (ROOT / "flake.nix").read_text(encoding="utf-8")
        shell_hook = flake.split("shellHook = ''", 1)[1].split("'';", 1)[0]

        self.assertNotIn("FPATH", shell_hook)
        self.assertIn(
            'cp completions/zsh/_packctl completions/zsh/_huroshiki "$out/share/zsh/site-functions/"',
            flake,
        )
        runtime_inputs = flake.split("runtimeInputs =", 1)[1].split("];", 1)[0]
        self.assertNotIn("just", runtime_inputs)
        self.assertIn("just", flake.split("packages = with pkgs;", 1)[1])

    def test_package_uses_dedicated_completions_without_overriding_just(self) -> None:
        root = ROOT / "shared" / "completions" / "zsh"
        self.assertTrue((root / "_packctl").is_file())
        self.assertTrue((root / "_huroshiki").is_file())
        self.assertFalse((root / "_just").exists())
        packctl_completion = (root / "_packctl").read_text(encoding="utf-8")
        self.assertNotIn("migrate-template", packctl_completion)
        self.assertIn("list-templates", packctl_completion)


if __name__ == "__main__":
    unittest.main()
