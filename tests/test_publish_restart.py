from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests.test_pack_publish_manifest import PackPublishManifestTest

import packctl
import pack_publish
import publish_restart as restart
import publish_target
from process_runner import BoundedProcessResult


class PublishRestartTest(PackPublishManifestTest):
    def setUp(self) -> None:
        super().setUp()
        self.target = publish_target.publish_remote_target_from_legacy_settings(
            rsync_target=f"publisher@publish.example:{self.root / 'configured'}",
            ssh_host="minecraft@game.example",
            stack_dir="/srv/minecraft",
            service="minecraft",
            remote_path=str(self.root / "remote"),
        )
        self.settings = packctl.DeploymentSettings(
            f"publisher@publish.example:{self.root / 'configured'}",
            "minecraft@game.example",
            "/srv/minecraft",
            "minecraft",
        )

    def _bound(
        self,
        *,
        status: str = "succeeded",
        returncode: int = 0,
        process_returncode: int = 0,
        stderr: str = "",
        **flags,
    ):
        def run(command, *, stdin, **kwargs):
            request = json.loads(stdin)
            response = {
                "schema": restart._PROTOCOL_SCHEMA,
                "version": restart._PROTOCOL_VERSION,
                "request": "restart",
                "operation_id": request["operation_id"],
                "manifest_digest": request["manifest_digest"],
                "target_config_digest": request["target_config_digest"],
                "generation_id": request["generation_id"],
                "status": status,
                "returncode": returncode,
            }
            values = {
                "returncode": process_returncode,
                "stdout": json.dumps(response) + "\n",
                "stderr": stderr,
                "cancelled": False,
                "timed_out": False,
                **flags,
            }
            return BoundedProcessResult(**values)

        return run

    @staticmethod
    def _unsafe_target(target, *, restart_changes=None, **changes):
        restart_target = target.restart
        if restart_changes:
            restart_copy = object.__new__(publish_target.PublishRestartTarget)
            for name in ("mode", "endpoint", "stack_dir", "service", "enabled"):
                object.__setattr__(
                    restart_copy,
                    name,
                    restart_changes.get(name, getattr(restart_target, name)),
                )
            restart_target = restart_copy
        target_copy = object.__new__(publish_target.PublishRemoteTarget)
        for name in (
            "server_id",
            "publication_endpoint",
            "publication_root",
            "restart",
            "config_digest",
        ):
            value = restart_target if name == "restart" else changes.get(
                name, getattr(target, name)
            )
            object.__setattr__(target_copy, name, value)
        return target_copy

    def _inputs(self):
        manifest = pack_publish.plan_pack_publish_manifest("demo")
        generation = restart.compute_publish_generation_id(manifest, self.target)
        activated = restart.PublishActivatedGeneration(
            manifest.manifest_digest,
            self.target.config_digest,
            generation,
            self.target.publication_root / "generations" / generation,
            self.target.publication_root / "current",
            None,
            False,
        )
        return manifest, activated

    def _run(self, runner, *, target=None, **kwargs):
        manifest, activated = self._inputs()
        with patch.object(packctl, "deployment_settings", return_value=self.settings), patch.object(
            restart, "run_bounded_process", side_effect=runner
        ) as process:
            result = restart.restart_activated_publish(
                activated, manifest, target or self.target, **kwargs
            )
        return result, process

    def test_success_binding_and_restart_endpoint_argv(self) -> None:
        commands = []

        def runner(command, **kwargs):
            commands.append((list(command), kwargs))
            return self._bound()(command, **kwargs)

        result, process = self._run(runner)
        self.assertEqual(result.status, "succeeded")
        process.assert_called_once()
        command, kwargs = commands[0]
        self.assertEqual(command[:9], ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-p", "22", "--"])
        self.assertEqual(command[9], "minecraft@game.example")
        self.assertNotIn("publisher@publish.example", command)
        self.assertEqual(kwargs["cwd"], self.root)
        self.assertIsNotNone(kwargs["stdin"])

    def test_binding_errors_and_invalid_types_do_not_run(self) -> None:
        manifest, activated = self._inputs()
        cases = [
            (replace(activated, manifest_digest="0" * 64), "manifest"),
            (replace(activated, target_config_digest="0" * 64), "target"),
            (replace(activated, generation_id="v1-" + "f" * 64), "ID"),
            (replace(activated, generation_path=self.root / "wrong"), "canonical"),
            (replace(activated, current_path=self.root / "wrong-current"), "current"),
            (object(), "requires"),
        ]
        for value, text in cases:
            with self.subTest(text=text), patch.object(packctl, "deployment_settings", return_value=self.settings), patch.object(
                restart, "run_bounded_process"
            ) as run:
                with self.assertRaises(restart.PublishRestartError):
                    restart.restart_activated_publish(value, manifest, self.target)  # type: ignore[arg-type]
                run.assert_not_called()

    def test_malformed_target_and_restart_mode_do_not_run(self) -> None:
        manifest, activated = self._inputs()
        cases = (
            object(),
            self._unsafe_target(
                self.target, restart_changes={"mode": "systemd"}
            ),
            self._unsafe_target(
                self.target, restart_changes={"service": "bad service"}
            ),
        )
        for target in cases:
            with self.subTest(target=target), patch.object(
                restart, "run_bounded_process"
            ) as run:
                with self.assertRaises(restart.PublishRestartError):
                    restart.restart_activated_publish(
                        activated, manifest, target  # type: ignore[arg-type]
                    )
                run.assert_not_called()

    def test_malformed_manifest_does_not_run(self) -> None:
        manifest, activated = self._inputs()
        with patch.object(restart, "run_bounded_process") as run:
            with self.assertRaises(restart.PublishRestartError):
                restart.restart_activated_publish(
                    activated, object(), self.target  # type: ignore[arg-type]
                )
        run.assert_not_called()

    def test_stale_deployment_fields_are_rejected_without_runner(self) -> None:
        for field, value in (
            ("rsync_target", f"other@publish.example:{self.root / 'configured'}"),
            ("ssh_host", "other@game.example"),
            ("stack_dir", "/srv/other"),
            ("service", "other-service"),
        ):
            manifest, activated = self._inputs()
            stale = replace(self.settings, **{field: value})
            with self.subTest(field=field), patch.object(
                packctl, "deployment_settings", return_value=stale
            ), patch.object(restart, "run_bounded_process") as run:
                with self.assertRaisesRegex(restart.PublishRestartError, "stale"):
                    restart.restart_activated_publish(
                        activated, manifest, self.target
                    )
                run.assert_not_called()

    def test_stack_and_service_are_protocol_only_and_command_is_hardened(self) -> None:
        stack_dir = "/srv/minecraft;literal-$HOME"
        target = publish_target.publish_remote_target_from_legacy_settings(
            rsync_target=f"publisher@publish.example:{self.root / 'configured'}",
            ssh_host="minecraft@game.example",
            stack_dir=stack_dir,
            service="minecraft-server",
            remote_path=str(self.root / "remote"),
        )
        settings = replace(
            self.settings, stack_dir=stack_dir, service="minecraft-server"
        )
        manifest = pack_publish.plan_pack_publish_manifest("demo")
        generation = restart.compute_publish_generation_id(manifest, target)
        activated = restart.PublishActivatedGeneration(
            manifest.manifest_digest,
            target.config_digest,
            generation,
            target.publication_root / "generations" / generation,
            target.publication_root / "current",
            None,
            False,
        )
        observed = {}

        def runner(command, *, stdin, **kwargs):
            observed["command"] = command
            observed["request"] = json.loads(stdin)
            return self._bound()(command, stdin=stdin, **kwargs)

        with patch.object(
            packctl, "deployment_settings", return_value=settings
        ), patch.object(restart, "run_bounded_process", side_effect=runner):
            restart.restart_activated_publish(activated, manifest, target)
        command_text = "\n".join(observed["command"])
        self.assertNotIn(stack_dir, command_text)
        self.assertNotIn("minecraft-server", command_text)
        self.assertNotIn("StrictHostKeyChecking=no", observed["command"])
        self.assertNotIn("sh", observed["command"][:1])
        self.assertNotIn("bash", observed["command"][:1])
        self.assertEqual(observed["request"]["stack_dir"], stack_dir)
        self.assertEqual(observed["request"]["service"], "minecraft-server")

    def test_missing_or_malformed_response_is_uncertain_and_not_retried(self) -> None:
        for stdout in ("", "noise\n", "{}\n", "{}\n{}\n"):
            with self.subTest(stdout=stdout):
                result = BoundedProcessResult(0, stdout, "", False, False)
                manifest, activated = self._inputs()
                with patch.object(packctl, "deployment_settings", return_value=self.settings), patch.object(
                    restart, "run_bounded_process", return_value=result
                ) as run:
                    with self.assertRaises(restart.PublishRestartIntegrityError) as error:
                        restart.restart_activated_publish(activated, manifest, self.target)
                self.assertEqual(error.exception.result.status, "uncertain")
                run.assert_called_once()

    def test_response_binding_mismatch_is_uncertain(self) -> None:
        manifest, activated = self._inputs()

        def runner(command, *, stdin, **kwargs):
            request = json.loads(stdin)
            response = {
                "schema": restart._PROTOCOL_SCHEMA, "version": 1, "request": "restart",
                "operation_id": request["operation_id"], "manifest_digest": "0" * 64,
                "target_config_digest": request["target_config_digest"],
                "generation_id": request["generation_id"], "status": "succeeded", "returncode": 0,
            }
            return BoundedProcessResult(0, json.dumps(response) + "\n", "", False, False)

        with patch.object(packctl, "deployment_settings", return_value=self.settings), patch.object(
            restart, "run_bounded_process", side_effect=runner
        ) as run:
            with self.assertRaises(restart.PublishRestartIntegrityError):
                restart.restart_activated_publish(activated, manifest, self.target)
        run.assert_called_once()

    def test_success_failed_uncertain_response_bindings(self) -> None:
        for status, code, expected in (("succeeded", 0, "succeeded"), ("failed", 3, "failed"), ("uncertain", 0, "uncertain")):
            with self.subTest(status=status):
                if status == "uncertain":
                    with self.assertRaises(restart.PublishRestartIntegrityError):
                        self._run(self._bound(status=status, returncode=code))
                else:
                    result, _ = self._run(self._bound(status=status, returncode=code))
                    self.assertEqual(result.status, expected)

    def test_process_lifecycle_and_output_flags_have_priority(self) -> None:
        for field in (
            "output_limit_exceeded",
            "termination_incomplete",
            "orphaned_descendants",
        ):
            with self.subTest(field=field):
                runner = self._bound(**{field: True})
                with self.assertRaises(restart.PublishRestartIntegrityError) as error:
                    self._run(runner)
                self.assertEqual(error.exception.result.status, "uncertain")

        for field in ("cancelled", "timed_out"):
            with self.subTest(field=field):
                runner = self._bound(process_group=1234, **{field: True})
                with self.assertRaises(restart.PublishRestartIntegrityError) as error:
                    self._run(runner)
                self.assertEqual(error.exception.result.status, "uncertain")

        for flags, message in (
            (
                {"cancelled": True, "termination_incomplete": True},
                "termination was incomplete",
            ),
            (
                {"timed_out": True, "orphaned_descendants": True},
                "background processes",
            ),
        ):
            with self.subTest(flags=flags):
                with self.assertRaisesRegex(
                    restart.PublishRestartIntegrityError, message
                ):
                    self._run(self._bound(**flags))

    def test_runner_prelaunch_cancel_and_deadline_are_not_attempts(self) -> None:
        for result, error_type in (
            (
                BoundedProcessResult(None, "", "", True, False),
                restart.PublishRestartCancelled,
            ),
            (
                BoundedProcessResult(None, "", "", False, True),
                restart.PublishRestartDeadlineExceeded,
            ),
        ):
            with self.subTest(error=error_type), patch.object(
                packctl, "deployment_settings", return_value=self.settings
            ), patch.object(
                restart, "run_bounded_process", return_value=result
            ) as run:
                manifest, activated = self._inputs()
                with self.assertRaises(error_type) as error:
                    restart.restart_activated_publish(
                        activated, manifest, self.target
                    )
            self.assertFalse(hasattr(error.exception, "result"))
            run.assert_called_once()

    def test_connection_failure_stderr_and_nonzero_are_uncertain(self) -> None:
        for runner in (
            self._bound(process_returncode=255, stderr="connection lost"),
            self._bound(stderr="x" * 60000, output_limit_exceeded=True),
            self._bound(status="failed", returncode=3, process_returncode=1),
        ):
            with self.assertRaises(restart.PublishRestartIntegrityError) as error:
                self._run(runner)
            self.assertEqual(error.exception.result.status, "uncertain")

    def test_cancel_and_deadline_before_launch(self) -> None:
        for kwargs, error_type in (
            ({"cancel_event": threading.Event()}, restart.PublishRestartCancelled),
            ({"deadline": time.monotonic() - 1}, restart.PublishRestartDeadlineExceeded),
        ):
            if "cancel_event" in kwargs:
                kwargs["cancel_event"].set()
            with self.subTest(error=error_type), patch.object(restart, "run_bounded_process") as run:
                with self.assertRaises(error_type):
                    self._run(self._bound(), **kwargs)
            run.assert_not_called()

    def test_restart_never_touches_publication_paths(self) -> None:
        manifest, activated = self._inputs()
        root = Path(self.target.publication_root)
        root.mkdir(parents=True)
        current = root / "current"
        current.write_bytes(b"preserve")
        marker = root / "unrelated"
        marker.write_bytes(b"also preserve")
        with patch.object(packctl, "deployment_settings", return_value=self.settings), patch.object(
            restart, "run_bounded_process", side_effect=self._bound()
        ):
            restart.restart_activated_publish(activated, manifest, self.target)
        self.assertEqual(current.read_bytes(), b"preserve")
        self.assertEqual(marker.read_bytes(), b"also preserve")
        self.assertFalse((root / "generations").exists())

    def test_failed_and_uncertain_restart_leave_activation_untouched(self) -> None:
        manifest, activated = self._inputs()
        root = Path(self.target.publication_root)
        generation = root / "generations" / activated.generation_id
        generation.mkdir(parents=True)
        current = root / "current"
        current.symlink_to(generation)
        before = os.readlink(current)
        with patch.object(
            packctl, "deployment_settings", return_value=self.settings
        ), patch.object(
            restart,
            "run_bounded_process",
            side_effect=self._bound(status="failed", returncode=4),
        ):
            result = restart.restart_activated_publish(
                activated, manifest, self.target
            )
        self.assertEqual(result.status, "failed")
        self.assertEqual(os.readlink(current), before)
        with patch.object(
            packctl, "deployment_settings", return_value=self.settings
        ), patch.object(
            restart, "run_bounded_process", return_value=BoundedProcessResult(
                255, "", "lost", False, False
            )
        ):
            with self.assertRaises(restart.PublishRestartIntegrityError):
                restart.restart_activated_publish(
                    activated, manifest, self.target
                )
        self.assertEqual(os.readlink(current), before)
        self.assertTrue(generation.is_dir())

    def test_progress_exceptions_result_fields_and_no_sensitive_data(self) -> None:
        phases = []

        def progress(phase):
            phases.append(phase)
            raise RuntimeError("ignored")

        result, _ = self._run(self._bound(), progress=progress)
        self.assertEqual(phases, ["validating", "restarting", "succeeded"])
        self.assertTrue(result.attempted)
        self.assertTrue(result.succeeded)
        self.assertEqual(result.remote_returncode, 0)
        self.assertEqual(
            set(result.__dataclass_fields__),
            {
                "manifest_digest",
                "target_config_digest",
                "generation_id",
                "attempted",
                "succeeded",
                "status",
                "remote_returncode",
            },
        )

    def test_remote_helper_exact_docker_argv_cwd_and_devnull(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stack = root / "stack"
            stack.mkdir()
            record = root / "record"
            docker = root / "docker"
            docker.write_text(
                '''#!/bin/sh
printf '%s\n' "$PWD" > "$RECORD"
printf '%s\n' "$@" >> "$RECORD"
exit 0
'''
            )
            docker.chmod(0o755)
            payload = {
                "schema": restart._PROTOCOL_SCHEMA, "version": 1, "operation_id": "a" * 32,
                "manifest_digest": "b" * 64, "target_config_digest": "c" * 64,
                "generation_id": "v1-" + "d" * 64, "stack_dir": str(stack),
                "service": "minecraft", "mode": "compose",
            }
            env = {"PATH": str(root) + os.pathsep + os.environ["PATH"], "RECORD": str(record)}
            completed = subprocess.run(
                [sys.executable, "-c", restart._REMOTE_HELPER_SCRIPT],
                input=json.dumps(payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, check=False,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(record.read_text().splitlines(), [str(stack), "compose", "restart", "minecraft"])
            response = json.loads(completed.stdout)
            self.assertEqual(response["status"], "succeeded")

    def test_remote_helper_rejects_bad_stack_or_service_without_docker(self) -> None:
        for field, value in (("stack_dir", "/tmp/a/../b"), ("service", "bad service")):
            with self.subTest(field=field):
                payload = {"schema": restart._PROTOCOL_SCHEMA, "version": 1, "operation_id": "a" * 32,
                           "manifest_digest": "b" * 64, "target_config_digest": "c" * 64,
                           "generation_id": "v1-" + "d" * 64, "stack_dir": "/tmp/stack", "service": "minecraft", "mode": "compose"}
                payload[field] = value
                completed = subprocess.run([sys.executable, "-c", restart._REMOTE_HELPER_SCRIPT], input=json.dumps(payload).encode(), stdout=subprocess.PIPE, check=False)
                self.assertEqual(completed.returncode, 0)
                self.assertEqual(json.loads(completed.stdout)["status"], "failed")

    def test_remote_helper_uses_bounded_timeout_and_devnull_streams(self) -> None:
        source = restart._REMOTE_HELPER_SCRIPT
        self.assertIn("timeout=DOCKER_TIMEOUT", source)
        self.assertIn("stdin=subprocess.DEVNULL", source)
        self.assertIn("stdout=subprocess.DEVNULL", source)
        self.assertIn("stderr=subprocess.DEVNULL", source)
        self.assertIn("shell=False", source)
        self.assertNotIn("capture_output", source)

    def test_remote_helper_reports_docker_failure_and_timeout_as_protocol_status(self) -> None:
        source = restart._REMOTE_HELPER_SCRIPT
        self.assertIn('send(request, "uncertain", -1)', source)
        self.assertIn('send(request, "failed", 127)', source)
        self.assertIn('send(request, "failed", returncode)', source)


if __name__ == "__main__":
    unittest.main()
