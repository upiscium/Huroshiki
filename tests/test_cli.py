from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import huroshiki_core as core
import packctl


def args(
    identity: str,
    artifact_id: str | None = None,
    *,
    apply: bool = False,
    automatic: bool = False,
    reason: str | None = None,
):
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
            "automatic": automatic,
            "reason": reason,
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


def intent_preview(
    *,
    identity: str = "modrinth:A1b2C3d4",
    installed: str | None = "OldVer01",
    selected: str | None = "NewVer02",
    old_selection: str = "user",
    new_selection: str = "user",
    old_locked: bool | None = False,
    new_locked: bool | None = True,
    reason: str | None = "known-good release",
    status: str | None = "active",
):
    return core.ModVersionIntentPreview(
        identity,
        installed,
        selected,
        old_selection,
        new_selection,
        old_locked,
        new_locked,
        reason,
        status,
        (core.UpdateChange(Path("mods/example.pw.toml"), b"old", b"new"),),
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


class ModVersionIntentCliTest(unittest.TestCase):
    def test_parser_routes_automatic_pin_and_unpin(self) -> None:
        automatic = packctl.parser().parse_args(
            ["version", "demo", "curseforge:309927", "--automatic"]
        )
        self.assertIs(automatic.func, packctl.cmd_version)
        self.assertTrue(automatic.automatic)
        pinned = packctl.parser().parse_args(
            ["pin", "demo", "curseforge:309927", "--reason", "Compatibility"]
        )
        self.assertIs(pinned.func, packctl.cmd_pin)
        self.assertEqual(pinned.reason, "Compatibility")
        unpinned = packctl.parser().parse_args(
            ["unpin", "demo", "curseforge:309927", "--apply"]
        )
        self.assertIs(unpinned.func, packctl.cmd_unpin)
        self.assertTrue(unpinned.apply)

    def run_intent_case(self, command, command_args, *, apply=False):
        transaction = MagicMock()
        transaction.active = True
        transaction.prepare_mod_version_automatic.return_value = intent_preview(
            old_selection="user",
            new_selection="automatic",
            selected="NewVer02",
            old_locked=True,
            new_locked=None,
            reason="Compatibility",
            status="active",
        )
        transaction.prepare_mod_version_pin.return_value = intent_preview()
        transaction.apply.side_effect = lambda **_kwargs: setattr(
            transaction, "active", False
        )
        transaction.discard.side_effect = lambda: setattr(transaction, "active", False)
        output = StringIO()
        with patch.object(core.PackTransaction, "create", return_value=transaction), redirect_stdout(output):
            result = command(command_args)
        return result, transaction, output.getvalue()

    def test_automatic_preview_prints_intent_and_discards(self) -> None:
        result, transaction, output = self.run_intent_case(
            packctl.cmd_version,
            args("modrinth:A1b2C3d4", automatic=True),
        )
        self.assertEqual(result, 0)
        transaction.prepare_mod_version_automatic.assert_called_once_with(
            "modrinth:A1b2C3d4"
        )
        transaction.apply.assert_not_called()
        transaction.discard.assert_called_once_with()
        self.assertIn("MOD: modrinth:A1b2C3d4", output)
        self.assertIn("Selection: User exact -> Automatic", output)
        self.assertIn("Pin: Locked -> N/A", output)
        self.assertIn("Reason: Compatibility", output)
        self.assertIn("Status: active", output)
        self.assertIn("Installed artifact will not change.", output)
        self.assertIn("Dry run only; no files were changed.", output)

    def test_automatic_apply_uses_non_refreshing_transaction_apply(self) -> None:
        result, transaction, output = self.run_intent_case(
            packctl.cmd_version,
            args("curseforge:309927", automatic=True, apply=True),
            apply=True,
        )
        self.assertEqual(result, 0)
        transaction.prepare_mod_version_automatic.assert_called_once_with(
            "curseforge:309927"
        )
        transaction.apply.assert_called_once_with(refresh=False)
        transaction.discard.assert_not_called()
        self.assertIn("MOD version intent applied.", output)

    def test_pin_preview_passes_reason_and_discards(self) -> None:
        result, transaction, output = self.run_intent_case(
            packctl.cmd_pin,
            args("modrinth:A1b2C3d4", apply=False, reason="known-good release"),
        )
        self.assertEqual(result, 0)
        transaction.prepare_mod_version_pin.assert_called_once_with(
            "modrinth:A1b2C3d4", locked=True, reason="known-good release"
        )
        transaction.apply.assert_not_called()
        transaction.discard.assert_called_once_with()
        self.assertIn("Selection: User exact", output)
        self.assertIn("Pin: Unlocked -> Locked", output)
        self.assertIn("Reason: known-good release", output)
        self.assertIn("Dry run only; no files were changed.", output)

    def test_pin_apply_is_publishing_and_unpin_apply_clears_pin(self) -> None:
        for command, locked, reason in (
            (packctl.cmd_pin, True, "keep this version"),
            (packctl.cmd_unpin, False, None),
        ):
            with self.subTest(command=command.__name__):
                transaction = MagicMock(active=True)
                transaction.prepare_mod_version_pin.return_value = intent_preview(
                    new_locked=locked, reason=reason
                )
                transaction.apply.side_effect = lambda **_kwargs: setattr(
                    transaction, "active", False
                )
                with patch.object(core.PackTransaction, "create", return_value=transaction), redirect_stdout(StringIO()):
                    result = command(args("curseforge:309927", apply=True, reason=reason))
                self.assertEqual(result, 0)
                transaction.prepare_mod_version_pin.assert_called_once_with(
                    "curseforge:309927", locked=locked, reason=reason
                )
                transaction.apply.assert_called_once_with(refresh=False)
                transaction.discard.assert_not_called()

    def test_intent_failure_is_stable_nonzero_and_discards(self) -> None:
        transaction = MagicMock(active=True)
        transaction.prepare_mod_version_automatic.side_effect = core.HuroshikiError(
            "override state is stale"
        )
        transaction.discard.side_effect = lambda: setattr(transaction, "active", False)
        parsed = args("modrinth:A1b2C3d4", automatic=True)
        parsed.func = packctl.cmd_version
        stderr = StringIO()
        with patch.object(packctl, "parser") as parser, patch.object(
            core.PackTransaction, "create", return_value=transaction
        ), redirect_stderr(stderr):
            parser.return_value.parse_args.return_value = parsed
            self.assertEqual(packctl.main(), 2)
        self.assertEqual(stderr.getvalue(), "error: override state is stale\n")
        transaction.apply.assert_not_called()
        transaction.discard.assert_called_once_with()

    def test_version_selector_flags_are_mutually_exclusive(self) -> None:
        selectors = (
            ("--automatic", None),
            ("--artifact-id", "artifact"),
            ("--file-id", "file"),
            ("--version-id", "version"),
        )
        for index, (flag, value) in enumerate(selectors):
            for other_flag, other_value in selectors[index + 1 :]:
                with self.subTest(flags=(flag, other_flag)):
                    command = ["version", "demo", "modrinth:A1b2C3d4", flag]
                    if value is not None:
                        command.append(value)
                    command.append(other_flag)
                    if other_value is not None:
                        command.append(other_value)
                    with self.assertRaises(SystemExit) as error:
                        packctl.parser().parse_args(command)
                    self.assertEqual(error.exception.code, 2)


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
