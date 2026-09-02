from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import sys
import threading
import types
import unittest
from unittest.mock import ANY, Mock, patch
from pathlib import Path
from contextlib import redirect_stderr

import packctl
import huroshiki_core as core


class TemplateMigrationCliTest(unittest.TestCase):
    @staticmethod
    def _args(**overrides):
        values = dict(
            source_template="base", copy_to="new", display_name="New",
            minecraft="1.21.1", loader="fabric", loader_version="0.16",
            apply=False, ack_warnings=None, template_migration_removals=None,
            template_migration_replacements=None,
        )
        values.update(overrides)
        return types.SimpleNamespace(**values)

    def test_cli_preview_prints_formatter_output_without_url_secret(self) -> None:
        output = StringIO()
        raw = "https://user:password@example.invalid/mod.jar?access_token=secret&normal=value"
        target = core.TemplateMigrationTarget(
            "new", "New", "1.21.1", "fabric", "0.16"
        )
        view = core.TemplateCopyMigrationView(
            "resolved", "base", target, "1.20.1", "forge", "47.0",
            (), (), (), (), (), (),
            (types.SimpleNamespace(
                url=raw, status="unknown", loader_status="unknown",
                minecraft_status="unknown", detail=raw,
            ),),
            (), (), (), (), 1, "c" * 64, "precommit", False, False, None,
        )
        preview = core.TemplateCopyMigrationPreview(
            view, "a" * 64, "b" * 64, 1, "c" * 64
        )
        with redirect_stdout(output):
            packctl._template_migration_preview(preview, core)
        rendered = output.getvalue()
        self.assertNotIn("password", rendered)
        self.assertNotIn("secret", rendered)
        self.assertIn("normal=value", rendered)

    def test_nested_command_has_required_target_arguments(self) -> None:
        args = packctl.parser().parse_args([
            "template", "migrate", "base", "--copy-to", "new", "--display-name", "New",
            "--minecraft", "1.21.1", "--loader", "fabric", "--loader-version", "0.16",
        ])
        self.assertIs(args.func, packctl.cmd_template_migrate)
        self.assertFalse(args.apply)

    def test_nested_help_and_required_target_arguments(self) -> None:
        self.assertIn("template", packctl.parser().format_help())
        with redirect_stderr(StringIO()), redirect_stdout(StringIO()), self.assertRaises(SystemExit):
            packctl.parser().parse_args(["template", "migrate", "--help"])

    def test_nested_parser_rejects_missing_target_arguments(self) -> None:
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            packctl.parser().parse_args(["template", "migrate", "base"])

    def test_partial_choices_preserve_selector_after_first_colon(self) -> None:
        class Choice:
            def __init__(self, source_index, action, **kwargs):
                self.source_index = source_index
                self.action = action
                self.__dict__.update(kwargs)

        core = types.SimpleNamespace(TemplateMigrationRootResolution=Choice)
        args = types.SimpleNamespace(
            template_migration_removals=["2"],
            template_migration_replacements=["3=modrinth:https://host/mod:a"],
        )
        choices = packctl._template_migration_choices(args, core)
        self.assertEqual(choices[0].source_index, 2)
        self.assertEqual(choices[1].replacement_project_id, "https://host/mod:a")

    def test_preview_does_not_publish(self) -> None:
        class Target:
            def __init__(self, *args, **kwargs):
                self.kwargs = (args, kwargs)

        class Session:
            state = "resolved"

            def __init__(self, *args):
                self.args = args
                self.view = types.SimpleNamespace(publication_lifecycle="precommit")
                self.preview_value = object()

            def start(self):
                return self.view

            def preview(self):
                return self.preview_value

            def discard(self):
                self.discarded = True

            def prepare_publication(self, acknowledgements, *, expected_preview):
                self.prepared = (acknowledgements, expected_preview)

            def publish(self):
                self.published = True

        fake_core = types.SimpleNamespace(
            TemplateMigrationTarget=Target,
            TemplateCopyMigrationSession=Session,
            format_template_copy_migration_preview=lambda value: ("preview",),
            format_template_copy_migration_requirements=lambda value: (),
        )
        args = self._args()
        output = StringIO()
        with patch.dict(sys.modules, {"huroshiki_core": fake_core}), redirect_stdout(output):
            self.assertEqual(packctl.cmd_template_migrate(args), 0)
        self.assertIn("Preview ready", output.getvalue())

    def test_apply_passes_exact_preview_and_warning_tuple(self) -> None:
        class Target:
            def __init__(self, *args, **kwargs): pass
        class Session:
            state = "resolved"
            def __init__(self, *args):
                self.view = types.SimpleNamespace(publication_lifecycle="precommit")
                self.preview_object = object()
            def start(self): pass
            def preview(self): return self.preview_object
            def prepare_publication(self, warnings, *, expected_preview):
                self.prepared = warnings, expected_preview
            def publish(self): self.published = True
        fake = types.SimpleNamespace(
            TemplateMigrationTarget=Target, TemplateCopyMigrationSession=Session,
            format_template_copy_migration_preview=lambda p: ("Template: base",),
            format_template_copy_migration_requirements=lambda s: (),
        )
        args = self._args(apply=True, ack_warnings=["url-risk", "side-change"])
        with patch.dict(sys.modules, {"huroshiki_core": fake}):
            self.assertEqual(packctl.cmd_template_migrate(args), 0)

    def test_choices_are_rejected_when_resolution_is_not_required(self) -> None:
        class Session:
            state = "resolved"
            def __init__(self, *args):
                self.view = types.SimpleNamespace(publication_lifecycle="precommit")
            def start(self): pass
            def discard(self): self.discarded = True
        class Choice:
            def __init__(self, *args, **kwargs):
                self.source_index = args[0]
        fake = types.SimpleNamespace(
            TemplateMigrationTarget=lambda *a, **k: object(),
            TemplateCopyMigrationSession=Session,
            TemplateMigrationRootResolution=Choice,
            format_template_copy_migration_preview=lambda p: (),
            format_template_copy_migration_requirements=lambda s: (),
        )
        with patch.dict(sys.modules, {"huroshiki_core": fake}):
            with self.assertRaisesRegex(packctl.ConfigError, "not currently required"):
                packctl.cmd_template_migrate(self._args(template_migration_removals=["4"]))

    def test_source_does_not_import_private_template_migration_module(self) -> None:
        source = Path(packctl.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import template_migration", source)
        self.assertNotIn("from template_migration", source)

    def test_cli_uses_one_event_deadline_and_typed_resolution_choices(self) -> None:
        calls = []
        event = threading.Event()

        class Session:
            state = "resolution-required"
            view = types.SimpleNamespace(publication_lifecycle="precommit")
            def __init__(self, source, target, cancel_event, deadline):
                calls.append((source, target, cancel_event, deadline))
                self.cancel_event, self.deadline = cancel_event, deadline
            def start(self): pass
            def resolve_choices(self, choices):
                calls.append(("resolve", choices, self.cancel_event, self.deadline))
                self.state = "resolved"
            def preview(self): return types.SimpleNamespace(required_warnings=())
            def discard(self): calls.append("discard")

        fake = types.SimpleNamespace(
            TemplateMigrationTarget=core.TemplateMigrationTarget,
            TemplateCopyMigrationSession=Session,
            TemplateMigrationRootResolution=core.TemplateMigrationRootResolution,
            format_template_copy_migration_preview=lambda p: ("preview",),
            format_template_copy_migration_requirements=lambda s: (),
        )
        with patch.dict(sys.modules, {"huroshiki_core": fake}), patch(
            "packctl.threading.Event", return_value=event
        ), patch("packctl.time.monotonic", return_value=100.0):
            result = packctl.cmd_template_migrate(self._args(
                template_migration_removals=["2"],
                template_migration_replacements=["3=modrinth:https://example.test/project:part"],
            ))
        self.assertEqual(result, 0)
        self.assertEqual(calls[0][0], "base")
        self.assertIs(calls[0][2], event)
        self.assertEqual(calls[0][3], 100.0 + packctl.PACKWIZ_OPERATION_TIMEOUT_SECONDS)
        self.assertEqual(calls[1][0], "resolve")
        self.assertEqual([c.action for c in calls[1][1]], ["remove", "replace"])
        self.assertEqual(calls[1][1][1].replacement_project_id, "https://example.test/project:part")

    def test_unresolved_is_partial_and_never_publishes(self) -> None:
        session = Mock(state="resolution-required", view=types.SimpleNamespace(publication_lifecycle="precommit"))
        session.start.return_value = None
        session.resolve_choices.return_value = None
        session.preview.return_value = object()
        fake = types.SimpleNamespace(
            TemplateMigrationTarget=lambda **kw: object(), TemplateCopyMigrationSession=lambda *a: session,
            TemplateMigrationRootResolution=core.TemplateMigrationRootResolution,
            format_template_copy_migration_preview=lambda p: (),
            format_template_copy_migration_requirements=lambda s: ("required",),
        )
        with patch.dict(sys.modules, {"huroshiki_core": fake}), redirect_stdout(StringIO()) as output:
            result = packctl.cmd_template_migrate(self._args(template_migration_removals=["1"]))
        self.assertEqual(result, 2)
        session.resolve_choices.assert_called_once()
        session.preview.assert_not_called()
        session.discard.assert_called_once()
        self.assertIn("required", output.getvalue())

    def test_duplicate_choices_and_unknown_ack_are_cleaned_by_session(self) -> None:
        with self.assertRaisesRegex(packctl.ConfigError, "Duplicate Template migration removal"):
            packctl._template_migration_choices(self._args(template_migration_removals=["1", "1"]), core)

        class Session:
            instances = []
            state = "resolved"
            view = types.SimpleNamespace(publication_lifecycle="precommit")
            def __init__(self, *args): self.calls = []; self.instances.append(self)
            def start(self): self.calls.append("start")
            def preview(self): return object()
            def prepare_publication(self, warnings, *, expected_preview):
                self.calls.append(("prepare", warnings, expected_preview))
                raise core.TemplateMigrationOperationError("Unknown Template migration warning acknowledgement: nope")
            def discard(self): self.calls.append("discard")
        fake = types.SimpleNamespace(
            TemplateMigrationTarget=lambda **kw: object(), TemplateCopyMigrationSession=Session,
            format_template_copy_migration_preview=lambda p: (), format_template_copy_migration_requirements=lambda s: (),
        )
        with patch.dict(sys.modules, {"huroshiki_core": fake}):
            with self.assertRaisesRegex(packctl.ConfigError, "Unknown Template migration warning"):
                packctl.cmd_template_migrate(self._args(apply=True, ack_warnings=["nope"]))
        self.assertEqual(len(Session.instances), 1)
        self.assertEqual(Session.instances[0].calls, ["start", ("prepare", ("nope",), ANY), "discard"])

    def test_apply_warning_ack_and_expected_preview_reach_success_target(self) -> None:
        class Session:
            instances = []
            state = "resolved"
            view = types.SimpleNamespace(publication_lifecycle="precommit")
            def __init__(self, *args): self.preview_value = object(); self.prepared = None; self.published = False; self.discarded = False; self.instances.append(self)
            def start(self): pass
            def preview(self): return self.preview_value
            def prepare_publication(self, warnings, *, expected_preview): self.prepared = (warnings, expected_preview)
            def publish(self): self.published = True
        fake = types.SimpleNamespace(TemplateMigrationTarget=lambda **kw: object(), TemplateCopyMigrationSession=Session,
            format_template_copy_migration_preview=lambda p: ("EXPECTED PREVIEW",), format_template_copy_migration_requirements=lambda s: ())
        output = StringIO()
        with patch.dict(sys.modules, {"huroshiki_core": fake}), redirect_stdout(output):
            self.assertEqual(packctl.cmd_template_migrate(self._args(apply=True, ack_warnings=["warning"])), 0)
        session = Session.instances[-1]
        self.assertEqual(session.prepared[0], ("warning",)); self.assertIs(session.prepared[1], session.preview_value)
        self.assertTrue(session.published); self.assertIn("Target Template: new", output.getvalue())

    def test_precommit_cleanup_is_attempted_and_cleanup_pending_is_distinct(self) -> None:
        class Session:
            state = "resolved"
            view = types.SimpleNamespace(publication_lifecycle="precommit")
            instances = []
            def __init__(self, *args): self.calls = []; self.instances.append(self)
            def start(self): self.calls.append("start")
            def preview(self): return object()
            def prepare_publication(self, *args, **kwargs): self.calls.append("prepare")
            def publish(self): self.calls.append("publish"); raise RuntimeError("before commit")
            def discard(self): self.calls.append("discard")
        fake = types.SimpleNamespace(TemplateMigrationTarget=lambda **kw: object(), TemplateCopyMigrationSession=Session,
            format_template_copy_migration_preview=lambda p: (), format_template_copy_migration_requirements=lambda s: ())
        with patch.dict(sys.modules, {"huroshiki_core": fake}):
            with self.assertRaisesRegex(packctl.ConfigError, "before commit"):
                packctl.cmd_template_migrate(self._args(apply=True))
        self.assertIn("discard", Session.instances[-1].calls)

    def test_committed_failure_retries_only_and_uncertain_never_discards(self) -> None:
        class Session:
            instances = []
            def __init__(self, *args): self.calls = []; self.instances.append(self); self.state = "resolved"; self.lifecycle = "committed"; self.view = types.SimpleNamespace(publication_lifecycle="committed")
            def start(self): pass
            def preview(self): return object()
            def prepare_publication(self, *a, **k): pass
            def publish(self): self.state = "cleanup-pending"; raise RuntimeError("committed")
            def retry_cleanup(self): self.calls.append("retry")
            def discard(self): self.calls.append("discard")
        fake = types.SimpleNamespace(TemplateMigrationTarget=lambda **kw: object(), TemplateCopyMigrationSession=Session,
            format_template_copy_migration_preview=lambda p: (), format_template_copy_migration_requirements=lambda s: ())
        with patch.dict(sys.modules, {"huroshiki_core": fake}):
            self.assertEqual(packctl.cmd_template_migrate(self._args(apply=True)), 0)
        self.assertEqual(Session.instances[-1].calls, ["retry"])

        uncertain = Session()
        uncertain.view.publication_lifecycle = "uncertain"
        uncertain.state = "publication-uncertain"
        with patch.dict(sys.modules, {"huroshiki_core": fake}):
            error = packctl._template_migration_failure(
                uncertain, RuntimeError("outcome")
            )
        self.assertIn("uncertain", str(error))
        self.assertNotIn("discard", uncertain.calls)


if __name__ == "__main__":
    unittest.main()
