from __future__ import annotations

import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

import huroshiki_core as core
import packctl
import publish_orchestration as publish


def result(status: str) -> publish.PackPublishResult:
    values = dict(
        pack_id="demo", target_side="server", manifest_digest="m" * 64,
        target_config_digest="t" * 64, generation_id="g" * 32,
        publication_succeeded=status != "publication_failed",
        remote_verified=status != "publication_failed",
        activated=status != "publication_failed",
        restart_attempted=status in {"published", "restart_failed", "restart_uncertain"},
        restart_succeeded=status == "published",
        restart_status=("succeeded" if status == "published" else
                        "failed" if status == "restart_failed" else
                        "uncertain" if status == "restart_uncertain" else "not_started"),
        final_status=status,
    )
    return publish.PackPublishResult(**values)


class PublishFormattingTest(unittest.TestCase):
    def test_plan_preview_contains_both_variants_and_one_safe_restart_target(self) -> None:
        endpoint = SimpleNamespace(user="publisher", host="example.org", port=22)
        restart = SimpleNamespace(endpoint=endpoint, stack_dir="/srv/mc", service="minecraft")
        server_target = SimpleNamespace(
            publication_endpoint=endpoint, publication_root="/srv/packs",
            restart=restart,
        )
        client_target = SimpleNamespace(publication_endpoint=endpoint, publication_root="/srv/packs/client")
        def manifest(side, path, size):
            return SimpleNamespace(
                target_side=side,
                manifest_digest=(side[0] * 64),
                files=(SimpleNamespace(relative_path=path, size=size, source_kind="file"),),
                total_bytes=size, warnings=(),
            )
        plan = SimpleNamespace(
            pack_id="demo", client_manifest=manifest("client", "mods/c.jar", 3),
            server_manifest=manifest("server", "mods/s.jar", 7),
            client_target=client_target, server_target=server_target,
            client_generation_id="c" * 32, server_generation_id="s" * 32,
        )
        lines = core.format_pack_publish_plan(plan)
        self.assertEqual(lines, core.format_pack_publish_plan(plan))
        self.assertIn("  Manifest digest: " + "c" * 64, lines)
        self.assertIn("  Manifest digest: " + "s" * 64, lines)
        self.assertIn("  Generation: " + "c" * 32, lines)
        self.assertIn("  Generation: " + "s" * 32, lines)
        self.assertIn("  Canonical publication root: /srv/packs/client", lines)
        self.assertIn("  Canonical publication root: /srv/packs", lines)
        self.assertIn("  Files: 1 (3 bytes)", lines)
        self.assertIn("  Files: 1 (7 bytes)", lines)
        self.assertEqual(sum(line == "Restart:" for line in lines), 1)

    def test_formatter_distinguishes_all_terminal_publication_states(self) -> None:
        for status in ("published", "restart_failed", "restart_not_started", "restart_uncertain", "publication_failed"):
            with self.subTest(status=status):
                lines = core.format_pack_publish_result(result(status))
                self.assertIn(f"Publication status: {status}", lines)
                self.assertNotEqual(lines[0], "Publication did not complete")
        formatted = core.format_pack_publish_result(result("cleanup_pending"))
        self.assertIn("Cleanup pending", formatted[0])
        self.assertTrue(any(line.startswith("Client transfer: ") for line in formatted))
        self.assertTrue(
            any(line.startswith("Server active verification: ") for line in formatted)
        )
        self.assertTrue(any(line.startswith("Restart: ") for line in formatted))


class PublishCliOutcomeTest(unittest.TestCase):
    def plan(self):
        plan = MagicMock()
        plan.cancel_event = threading.Event()
        plan.deadline = 20.0
        plan.pack_id = "demo"
        return plan

    def test_published_and_failure_outcomes_are_returned(self) -> None:
        for status, expected in (("published", 0), ("publication_failed", 1), ("restart_failed", 1), ("restart_not_started", 1), ("restart_uncertain", 1)):
            with self.subTest(status=status):
                plan = self.plan()
                args = type("Args", (), {"pack": "demo", "yes": True, "preview": False})()
                error = publish.PackPublishExecutionError("failed", result=result(status), phase="x", primary_error=RuntimeError("x"))
                execute = MagicMock(return_value=result(status), side_effect=None)
                if status != "published":
                    execute.side_effect = error
                with patch.object(core, "plan_pack_publish", return_value=plan), patch.object(
                    core, "execute_pack_publish", execute
                ), patch.object(packctl, "_print_pack_publish_preview"), patch.object(
                    packctl, "_print_pack_publish_result"
                ):
                    self.assertEqual(packctl.cmd_publish(args), expected)

    def test_cleanup_retry_is_once_and_success_uses_retained_result(self) -> None:
        plan = self.plan()
        plan.result = result("published")
        args = type("Args", (), {"pack": "demo", "yes": True, "preview": False})()
        cleanup = publish.PackPublishCleanupError(
            "cleanup pending", result=result("published"), phase="cleanup",
            primary_error=None, plan=plan,
        )
        with patch.object(core, "plan_pack_publish", return_value=plan), patch.object(
            core, "execute_pack_publish", side_effect=cleanup
        ), patch.object(core, "retry_pack_publish_cleanup") as retry, patch.object(
            packctl, "_print_pack_publish_preview"
        ), patch.object(packctl, "_print_pack_publish_result"):
            self.assertEqual(packctl.cmd_publish(args), 0)
        retry.assert_called_once_with(plan)

        retry.reset_mock()
        retry.side_effect = publish.PackPublishCleanupError(
            "still pending", result=result("published"), phase="cleanup",
            primary_error=None, plan=plan,
        )
        with patch.object(core, "plan_pack_publish", return_value=plan), patch.object(
            core, "execute_pack_publish", side_effect=cleanup
        ), patch.object(core, "retry_pack_publish_cleanup", retry), patch.object(
            packctl, "_print_pack_publish_preview"
        ), patch.object(packctl, "_print_pack_publish_result"):
            self.assertNotEqual(packctl.cmd_publish(args), 0)
        retry.assert_called_once_with(plan)

    def test_preview_uses_core_formatter_and_includes_digest_authority(self) -> None:
        endpoint = SimpleNamespace(user="publisher", host="example.org", port=22)
        plan = SimpleNamespace(
            pack_id="demo",
            client_manifest=SimpleNamespace(
                files=(), total_bytes=0, warnings=(), manifest_digest="c" * 64,
            ),
            server_manifest=SimpleNamespace(
                files=(), total_bytes=0, warnings=(), manifest_digest="m" * 64,
            ),
            client_target=SimpleNamespace(
                publication_endpoint=endpoint, publication_root="/srv/packs/client",
            ),
            server_target=SimpleNamespace(
                publication_endpoint=endpoint,
                publication_root="/srv/packs",
                restart=SimpleNamespace(
                    endpoint=endpoint,
                    stack_dir="/srv/mc",
                    service="minecraft",
                ),
            ),
            client_generation_id="c" * 32,
            server_generation_id="g" * 32,
            cancel_event=threading.Event(),
            deadline=20.0,
        )
        args = type("Args", (), {"pack": "demo", "yes": False, "preview": True})()
        output = StringIO()
        with patch.object(core, "plan_pack_publish", return_value=plan), patch.object(
            core, "execute_pack_publish"
        ) as execute, redirect_stdout(output):
            self.assertEqual(packctl.cmd_publish(args), 0)
        execute.assert_not_called()
        self.assertEqual(output.getvalue().count("Publication endpoint: publisher@example.org:22"), 2)
        self.assertIn("Manifest digest: " + "c" * 64, output.getvalue())
        self.assertIn("Manifest digest: " + "m" * 64, output.getvalue())
        self.assertIn("Canonical publication root: /srv/packs/client", output.getvalue())
        self.assertIn("Restart:", output.getvalue())

    def test_keyboard_interrupt_returns_130_and_reports_retained_result(self) -> None:
        plan = self.plan()
        plan.result = result("restart_uncertain")
        args = type("Args", (), {"pack": "demo", "yes": True, "preview": False})()

        def planned(_pack, *, cancel_event):
            plan.cancel_event = cancel_event
            return plan

        error = StringIO()
        with patch.object(core, "plan_pack_publish", side_effect=planned), patch.object(
            core, "execute_pack_publish", side_effect=KeyboardInterrupt
        ) as execute, patch.object(packctl, "_print_pack_publish_preview"), patch.object(
            core, "retry_pack_publish_cleanup"
        ) as retry, redirect_stderr(error):
            self.assertEqual(packctl.cmd_publish(args), 130)
        execute.assert_called_once_with(
            plan,
            cancel_event=plan.cancel_event,
            deadline=plan.deadline,
        )
        retry.assert_not_called()
        self.assertTrue(plan.cancel_event.is_set())
        self.assertIn("restart outcome uncertain", error.getvalue())

    def test_keyboard_interrupt_during_cleanup_retry_remains_pending(self) -> None:
        plan = self.plan()
        plan.result = result("cleanup_pending")
        args = type("Args", (), {"pack": "demo", "yes": True, "preview": False})()
        cleanup = publish.PackPublishCleanupError(
            "cleanup pending",
            result=plan.result,
            phase="cleanup",
            primary_error=None,
            plan=plan,
        )
        error = StringIO()
        with patch.object(core, "plan_pack_publish", return_value=plan), patch.object(
            core, "execute_pack_publish", side_effect=cleanup
        ) as execute, patch.object(
            core, "retry_pack_publish_cleanup", side_effect=KeyboardInterrupt
        ) as retry, patch.object(
            packctl, "_print_pack_publish_preview"
        ), redirect_stderr(error):
            self.assertEqual(packctl.cmd_publish(args), 130)
        execute.assert_called_once()
        retry.assert_called_once_with(plan)
        self.assertTrue(plan.cancel_event.is_set())
        self.assertIn("cleanup remains pending", error.getvalue())
