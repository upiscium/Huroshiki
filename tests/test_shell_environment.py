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
            'cp completions/zsh/_just "$out/share/zsh/site-functions/_just"',
            flake,
        )


if __name__ == "__main__":
    unittest.main()
