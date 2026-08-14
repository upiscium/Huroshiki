from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import huroshiki_core as core
import packctl


def args(identity: str, artifact_id: str, *, apply: bool = False):
    provider = identity.split(":", 1)[0]
    return type(
        "Args",
        (),
        {
            "pack": "demo",
            "identity": identity,
            "artifact_id": artifact_id,
            "file_id": None,
            "version_id": None,
            "apply": apply,
            "provider": provider,
        },
    )()


def preview(identity: str, old_id: str, new_id: str):
    provider = identity.split(":", 1)[0]
    return core.ModVersionSelectionPreview(
        identity,
        Path("mods/example.pw.toml"),
        "Example",
        provider,
        "1.0",
        old_id,
        "2.0",
        new_id,
        (core.UpdateChange(Path("mods/example.pw.toml"), b"old", b"new"),),
        2,
        1,
        (f"{provider}:Added001", f"{provider}:Added002"),
        (f"{provider}:Remov001",),
        identity,
        new_id,
        False,
    )


class ExactVersionCliTest(unittest.TestCase):
    def run_case(self, identity: str, artifact_id: str, *, apply: bool = False):
        transaction = MagicMock()
        transaction.active = True
        transaction.prepare_exact_mod_version.return_value = preview(
            identity, "OldVer01" if identity.startswith("modrinth:") else "1", artifact_id
        )
        transaction.apply.side_effect = lambda: setattr(transaction, "active", False)
        transaction.discard.side_effect = lambda: setattr(transaction, "active", False)
        output = StringIO()
        with patch.object(core.PackTransaction, "create", return_value=transaction), redirect_stdout(output):
            result = packctl.cmd_version(args(identity, artifact_id, apply=apply))
        return result, transaction, output.getvalue()

    def test_curseforge_and_modrinth_root_preview_are_non_publishing(self) -> None:
        for identity, artifact_id in (
            ("curseforge:309927", "6529130"),
            ("modrinth:A1b2C3d4", "E5f6G7h8"),
        ):
            with self.subTest(identity=identity):
                result, transaction, output = self.run_case(identity, artifact_id)
                self.assertEqual(result, 0)
                transaction.apply.assert_not_called()
                transaction.discard.assert_called_once_with()
                selection = transaction.prepare_exact_mod_version.call_args.args[0]
                self.assertEqual(selection.identity_label, identity)
                self.assertIn("Added dependencies: 2", output)
                self.assertIn("Removed dependencies: 1", output)
                self.assertIn(
                    f"User selection intent: {identity} -> {artifact_id} (unlocked)",
                    output,
                )

    def test_apply_publishes_only_after_verified_preview(self) -> None:
        result, transaction, _output = self.run_case(
            "curseforge:309927", "6529130", apply=True
        )
        self.assertEqual(result, 0)
        self.assertLess(
            transaction.mock_calls.index(
                unittest.mock.call.prepare_exact_mod_version(unittest.mock.ANY)
            ),
            transaction.mock_calls.index(unittest.mock.call.apply()),
        )

    def test_dependency_selection_uses_same_transaction_api(self) -> None:
        result, transaction, _output = self.run_case(
            "modrinth:A1b2C3d4", "E5f6G7h8"
        )
        self.assertEqual(result, 0)
        transaction.prepare_exact_mod_version.assert_called_once()

    def test_incompatibility_is_stable_nonzero_and_discards(self) -> None:
        transaction = MagicMock()
        transaction.active = True
        transaction.prepare_exact_mod_version.side_effect = core.HuroshikiError(
            "runtime compatibility conflict"
        )
        transaction.discard.side_effect = lambda: setattr(transaction, "active", False)
        parsed = packctl.parser().parse_args(
            [
                "version",
                "demo",
                "modrinth:A1b2C3d4",
                "--version-id",
                "E5f6G7h8",
            ]
        )
        stderr = StringIO()
        with patch.object(packctl, "parser") as parser, patch.object(
            core.PackTransaction, "create", return_value=transaction
        ), redirect_stderr(stderr):
            parser.return_value.parse_args.return_value = parsed
            self.assertEqual(packctl.main(), 2)
        self.assertIn("runtime compatibility conflict", stderr.getvalue())
        transaction.apply.assert_not_called()
        transaction.discard.assert_called_once_with()


class AddExactArtifactCliTest(unittest.TestCase):
    def test_parser_accepts_provider_specific_exact_artifact_flags(self) -> None:
        cases = (
            ("--version-id", "E5f6G7h8"),
            ("--file-id", "6529130"),
            ("--artifact-id", "E5f6G7h8"),
        )
        for flag, value in cases:
            with self.subTest(flag=flag):
                parsed = packctl.parser().parse_args(
                    ["add", "demo", "mr:A1b2C3d4", "both", flag, value]
                )
                self.assertEqual(getattr(parsed, flag[2:].replace("-", "_")), value)

    def test_cmd_add_passes_exact_root_artifact_into_one_transactional_call(self) -> None:
        parsed = packctl.parser().parse_args(
            [
                "add",
                "demo",
                "mr:A1b2C3d4",
                "client",
                "--version-id",
                "E5f6G7h8",
            ]
        )
        with patch.object(
            core, "add_mod_transactionally", return_value=0
        ) as add:
            self.assertEqual(packctl.cmd_add(parsed), 0)
        add.assert_called_once_with(
            "pack:demo",
            "modrinth",
            "A1b2C3d4",
            "client",
            artifact_id="E5f6G7h8",
        )

    def test_cmd_add_rejects_wrong_provider_flag_and_url_exact_selection(self) -> None:
        wrong = packctl.parser().parse_args(
            [
                "add",
                "demo",
                "mr:A1b2C3d4",
                "both",
                "--file-id",
                "123",
            ]
        )
        with self.assertRaisesRegex(packctl.ConfigError, "CurseForge"):
            packctl.cmd_add(wrong)
        url = packctl.parser().parse_args(
            [
                "add",
                "demo",
                "url:https://example.invalid/mod.jar",
                "both",
                "--artifact-id",
                "123",
            ]
        )
        with self.assertRaisesRegex(packctl.ConfigError, "self-hosted URL"):
            packctl.cmd_add(url)


if __name__ == "__main__":
    unittest.main()
