from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl


def migration_args(*extra: str):
    return packctl.parser().parse_args(
        [
            "migrate", "demo",
            "--copy-to", "next",
            "--display-name", "Next",
            "--minecraft", "1.21.4",
            "--loader", "fabric",
            "--loader-version", "0.16.0",
            *extra,
        ]
    )


class FakeMigrationSession:
    instances: list["FakeMigrationSession"] = []
    start_state = "resolved"
    lifecycle = "precommit"
    publish_error: BaseException | None = None
    retry_error: BaseException | None = None

    def __init__(self, source_key, target, cancel_event, deadline) -> None:
        self.source_key = source_key
        self.target = target
        self.cancel_event = cancel_event
        self.deadline = deadline
        self.state = "new"
        self.calls: list[object] = []
        self.candidates = ()
        self.unresolved = ()
        self.view = SimpleNamespace(publication_lifecycle=self.lifecycle)
        self.instances.append(self)

    def start(self) -> None:
        self.calls.append("start")
        self.state = self.start_state

    def select_root_candidates(self, selections) -> None:
        self.calls.append(("roots", selections))
        self.state = "resolved"
        self.candidates = ()

    def resolve_conflicts(self, choices) -> None:
        self.calls.append(("conflicts", choices))
        self.state = "resolved"
        self.unresolved = ()

    def preview(self):
        self.calls.append("preview")
        return object()

    def prepare_publication(self, acknowledgements) -> None:
        self.calls.append(("prepare", acknowledgements))
        self.state = "ready"

    def publish(self) -> None:
        self.calls.append("publish")
        if self.publish_error is not None:
            self.view.publication_lifecycle = "committed"
            self.state = "cleanup-pending"
            raise self.publish_error
        self.state = "published"

    def retry_cleanup(self) -> None:
        self.calls.append("retry-cleanup")
        if self.retry_error is not None:
            raise self.retry_error
        self.state = "published"

    def discard(self) -> None:
        self.calls.append("discard")
        self.state = "discarded"


class PackMigrationCliTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeMigrationSession.instances.clear()
        FakeMigrationSession.start_state = "resolved"
        FakeMigrationSession.lifecycle = "precommit"
        FakeMigrationSession.publish_error = None
        FakeMigrationSession.retry_error = None

    def run_command(self, *extra: str) -> tuple[int, str, FakeMigrationSession]:
        output = StringIO()
        with patch.object(
            core, "PackCopyMigrationSession", FakeMigrationSession
        ), patch.object(
            core, "format_pack_copy_migration_preview", return_value=("PREVIEW",)
        ), redirect_stdout(output):
            result = packctl.cmd_migrate(migration_args(*extra))
        return result, output.getvalue(), FakeMigrationSession.instances[-1]

    def test_parser_exposes_copy_only_required_options(self) -> None:
        args = migration_args("--apply", "--ack-warning", "review")
        self.assertIs(args.func, packctl.cmd_migrate)
        self.assertEqual(args.source_pack, "demo")
        self.assertEqual(args.copy_to, "next")
        self.assertEqual(args.loader, "fabric")
        self.assertEqual(args.ack_warnings, ["review"])

    def test_preview_is_default_and_discards_without_publication(self) -> None:
        result, output, session = self.run_command()
        self.assertEqual(result, 0)
        self.assertEqual(session.calls, ["start", "preview", "discard"])
        self.assertIn("PREVIEW", output)
        self.assertIn("target not published", output)

    def test_apply_forwards_exact_acknowledgements_and_publishes(self) -> None:
        result, output, session = self.run_command(
            "--apply", "--ack-warning", "review", "--ack-warning", "config"
        )
        self.assertEqual(result, 0)
        self.assertIn(("prepare", ("review", "config")), session.calls)
        self.assertIn("publish", session.calls)
        self.assertNotIn("discard", session.calls)
        self.assertIn("Migration completed. Target Pack: next", output)

    def test_provenance_required_prints_requirements_and_discards(self) -> None:
        FakeMigrationSession.start_state = "provenance-required"

        def configure(session: FakeMigrationSession) -> None:
            session.candidates = (
                core.PackCopyMigrationRootCandidateView(
                    "mods/a.pw.toml", "modrinth:a", "modrinth", "a", None,
                    None, "both", "mods/a.pw.toml", "a.jar",
                ),
            )

        original_start = FakeMigrationSession.start

        def start(session: FakeMigrationSession) -> None:
            original_start(session)
            configure(session)

        with patch.object(FakeMigrationSession, "start", start), patch.object(
            core, "PackCopyMigrationSession", FakeMigrationSession
        ), redirect_stdout(StringIO()):
            result = packctl.cmd_migrate(migration_args())
        session = FakeMigrationSession.instances[-1]
        self.assertEqual(result, 2)
        self.assertEqual(session.calls, ["start", "discard"])

    def test_committed_cleanup_failure_retries_without_discard(self) -> None:
        FakeMigrationSession.publish_error = core.PackMigrationCleanupError("blocked")
        result, output, session = self.run_command("--apply", "--ack-warning", "review")
        self.assertEqual(result, 0)
        self.assertIn("retry-cleanup", session.calls)
        self.assertNotIn("discard", session.calls)
        self.assertIn("cleanup completed after an initial failure", output)

    def test_cleanup_still_pending_is_nonzero_and_never_discarded(self) -> None:
        FakeMigrationSession.publish_error = core.PackMigrationCleanupError("blocked")
        FakeMigrationSession.retry_error = core.PackMigrationCleanupError("still blocked")
        with patch.object(
            core, "PackCopyMigrationSession", FakeMigrationSession
        ), patch.object(
            core, "format_pack_copy_migration_preview", return_value=("PREVIEW",)
        ), redirect_stdout(StringIO()):
            with self.assertRaisesRegex(packctl.ConfigError, "published successfully.*still pending"):
                packctl.cmd_migrate(migration_args("--apply", "--ack-warning", "review"))
        session = FakeMigrationSession.instances[-1]
        self.assertNotIn("discard", session.calls)


if __name__ == "__main__":
    unittest.main()
