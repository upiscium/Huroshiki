from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path, PurePosixPath
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl
import publish_target as target


def _legacy_publish_target(
    *,
    rsync_target: str = "publish.example.org:/srv/packs/demo",
    ssh_host: str = "restart.example.org",
    stack_dir: str = "/srv/restart/demo",
    service: str = "minecraft",
    server_id: str = target.LEGACY_SERVER_ID,
    remote_path: str | None = None,
) -> target.PublishRemoteTarget:
    return target.publish_remote_target_from_legacy_settings(
        rsync_target=rsync_target,
        ssh_host=ssh_host,
        stack_dir=stack_dir,
        service=service,
        server_id=server_id,
        remote_path=remote_path,
    )


class PublishTargetModelTest(unittest.TestCase):
    def test_dataclass_instances_are_frozen(self) -> None:
        endpoint = target.PublishSshEndpoint("example.org", 22, None)
        self.assertRaises(FrozenInstanceError, setattr, endpoint, "host", "other.example")

        restart = target.PublishRestartTarget(
            "compose",
            endpoint,
            target.validate_publish_remote_path("/srv/demo"),
            "minecraft",
        )
        self.assertRaises(FrozenInstanceError, setattr, restart, "service", "server")
        self.assertTrue(restart.enabled)

        publish = _legacy_publish_target()
        self.assertRaises(FrozenInstanceError, setattr, publish, "server_id", "other")

    def test_legacy_adapter_extracts_fields_without_storing_rsync_string(self) -> None:
        target_value = _legacy_publish_target(
            rsync_target="deploy@publish.example.org:/srv/packs/demo",
            remote_path="/srv/custom/root",
            ssh_host="operator@restart.example.org",
        )

        self.assertEqual(target_value.publication_endpoint.host, "publish.example.org")
        self.assertEqual(target_value.publication_endpoint.user, "deploy")
        self.assertEqual(target_value.publication_root, PurePosixPath("/srv/custom/root"))
        self.assertEqual(target_value.restart.endpoint.host, "restart.example.org")
        self.assertEqual(target_value.restart.endpoint.user, "operator")
        self.assertFalse(hasattr(target_value, "rsync_target"))
        self.assertNotIn("publish.example.org:/srv/packs/demo", repr(target_value))


class PublishPathValidationTest(unittest.TestCase):
    def test_validate_publish_remote_path_accepts_valid_absolute_paths(self) -> None:
        for value in (
            "/srv/minecraft/demo",
            "/minecraft/packs/demo/.well-known",
        ):
            with self.subTest(value=value):
                path = target.validate_publish_remote_path(value)
                self.assertIsInstance(path, PurePosixPath)
                self.assertEqual(path.as_posix(), value)

    def test_validate_publish_remote_path_rejects_invalid_inputs(self) -> None:
        cases = (
            "",
            "/",
            "relative/path",
            ".",
            "..",
            "/./bad",
            "/../bad",
            "/srv//demo",
            "/srv/demo/",
            " /srv/demo",
            "/srv/demo ",
            "/srv/\n/demo",
            "C:/srv/demo",
            "\\server\\share",
            "/tmp/\u0065\u0301",  # non-NFC
            "/" + ("a" * 4097),
            "/" + ("a" * 256),
        )
        for value in cases:
            with self.subTest(value=value):
                self.assertRaises(target.PublishTargetError, target.validate_publish_remote_path, value)

    def test_repeated_slash_is_rejected_even_after_initial_prefix(self) -> None:
        self.assertRaises(target.PublishTargetError, target.validate_publish_remote_path, "//double/slash")


class PublishSshValidationTest(unittest.TestCase):
    def test_validate_publish_ssh_port_bounds_and_types(self) -> None:
        self.assertEqual(target.validate_publish_ssh_port(22), 22)

        invalid_ports = (
            0,
            65536,
            -1,
            True,
            False,
            "22",
            22.0,
        )
        for port in invalid_ports:
            with self.subTest(port=port):
                self.assertRaises(target.PublishTargetError, target.validate_publish_ssh_port, port)

    def test_parse_publish_ssh_endpoint_supports_dns_ipv4_ipv6_and_user(self) -> None:
        cases = (
            "example.org",
            "198.51.100.17",
            "[2001:db8::1]",
            "publisher@[2001:db8::1]",
            "publisher@example.org",
        )
        for value in cases:
            with self.subTest(value=value):
                endpoint = target.parse_publish_ssh_endpoint(value)
                self.assertIsInstance(endpoint, target.PublishSshEndpoint)
                self.assertEqual(endpoint.port, 22)

        endpoint = target.parse_publish_ssh_endpoint("publisher@publish.example.org", port=22022)
        self.assertEqual(endpoint.port, 22022)
        self.assertEqual(endpoint.user, "publisher")

    def test_parse_publish_ssh_endpoint_rejects_invalid_endpoint_port_and_user(self) -> None:
        for value in (None, 123, "", " host", "bad host", "a@b@c"):
            with self.subTest(value=value):
                self.assertRaises(target.PublishTargetError, target.parse_publish_ssh_endpoint, value)  # type: ignore[arg-type]

        with self.assertRaises(target.PublishTargetError):
            target.parse_publish_ssh_endpoint("bad:pass@host", port=22)

        with self.subTest(message="port out of range"):
            self.assertRaises(
                target.PublishTargetError,
                target.parse_publish_ssh_endpoint,
                "example.org",
                port=0,
            )

        with self.assertRaises(target.PublishTargetError):
            target.PublishSshEndpoint("publisher@example.org", 22, None)


class PublishRemoteTargetDigestTest(unittest.TestCase):
    def test_reserved_namespace_checks(self) -> None:
        for name in target.PUBLISH_RESERVED_NAMES:
            self.assertTrue(target.is_publish_reserved_child(name))
            self.assertTrue(target.is_publish_reserved_child(f"{target.PUBLISH_RESERVED_PREFIX}{name}"))

        self.assertTrue(target.is_publish_reserved_child(f"{target.PUBLISH_RESERVED_PREFIX}generation-0"))
        self.assertFalse(target.is_publish_reserved_child("demo-pack"))
        self.assertFalse(target.is_publish_reserved_child("currently"))

    def test_publish_remote_target_digest_is_deterministic_and_sensitive(self) -> None:
        base = _legacy_publish_target()
        again = _legacy_publish_target()
        self.assertEqual(base.config_digest, again.config_digest)

        no_raw_credentials = repr(base)
        self.assertNotIn("hunter2", no_raw_credentials)

        host_changed = _legacy_publish_target(
            rsync_target="other.example.org:/srv/packs/demo",
        )
        root_changed = _legacy_publish_target(remote_path="/srv/root-alt")
        restart_host_changed = _legacy_publish_target(ssh_host="another.example.org")
        restart_stack_changed = _legacy_publish_target(stack_dir="/srv/restart/other")
        restart_service_changed = _legacy_publish_target(service="minecraft-java")

        self.assertNotEqual(base.config_digest, host_changed.config_digest)
        self.assertNotEqual(base.config_digest, root_changed.config_digest)
        self.assertNotEqual(base.config_digest, restart_host_changed.config_digest)
        self.assertNotEqual(base.config_digest, restart_stack_changed.config_digest)
        self.assertNotEqual(base.config_digest, restart_service_changed.config_digest)

        publication_user_changed = _legacy_publish_target(
            rsync_target="publisher@publish.example.org:/srv/packs/demo",
        )
        restart_user_changed = _legacy_publish_target(
            ssh_host="operator@restart.example.org",
        )
        publication_port_changed = target.PublishSshEndpoint(
            base.publication_endpoint.host,
            22022,
            base.publication_endpoint.user,
        )
        restart_port_changed = target.PublishRestartTarget(
            mode=base.restart.mode,
            endpoint=target.PublishSshEndpoint(
                base.restart.endpoint.host,
                22022,
                base.restart.endpoint.user,
            ),
            stack_dir=base.restart.stack_dir,
            service=base.restart.service,
        )
        self.assertNotEqual(base.config_digest, publication_user_changed.config_digest)
        self.assertNotEqual(base.config_digest, restart_user_changed.config_digest)
        self.assertNotEqual(
            base.config_digest,
            target.compute_publish_remote_target_digest(
                server_id=base.server_id,
                publication_endpoint=publication_port_changed,
                publication_root=base.publication_root,
                restart=base.restart,
            ),
        )
        self.assertNotEqual(
            base.config_digest,
            target.compute_publish_remote_target_digest(
                server_id=base.server_id,
                publication_endpoint=base.publication_endpoint,
                publication_root=base.publication_root,
                restart=restart_port_changed,
            ),
        )

        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            original = Path.cwd()
            try:
                os.chdir(first)
                first_target = _legacy_publish_target()
                os.chdir(second)
                second_target = _legacy_publish_target()
            finally:
                os.chdir(original)

        self.assertEqual(first_target.config_digest, second_target.config_digest)

    def test_publish_remote_target_from_legacy_allows_restart_publication_host_mismatch(self) -> None:
        mismatched = _legacy_publish_target(
            rsync_target="publish.example.org:/srv/packs/demo",
            ssh_host="restart.example.org",
        )
        self.assertNotEqual(mismatched.publication_endpoint.host, mismatched.restart.endpoint.host)
        self.assertEqual(mismatched.publication_root, PurePosixPath("/srv/packs/demo"))


class PublishTargetOperationalSafetyTest(unittest.TestCase):
    def test_publish_remote_target_and_parser_do_not_execute_subprocess_or_network(self) -> None:
        with patch("socket.getaddrinfo") as getaddrinfo, patch.object(subprocess, "run") as run:
            _ = _legacy_publish_target()
            target.parse_publish_ssh_endpoint("example.org")
            getaddrinfo.assert_not_called()
            run.assert_not_called()

    def test_credentials_are_not_leaked_in_errors_or_digest(self) -> None:
        leak = "hunter2"
        with self.assertRaises(target.PublishTargetError) as err:
            _legacy_publish_target(
                rsync_target=f"user:{leak}@publish.example.org:/srv/packs/demo",
                ssh_host="restart.example.org",
            )

        self.assertNotIn(leak, str(err.exception))

        created = _legacy_publish_target()
        for needle in ("publish.example.org", "/srv/packs/demo", "hunter2", "rsync_target"):
            self.assertNotIn(needle, created.config_digest)

    def test_restart_requires_valid_stack_and_compose_service(self) -> None:
        with self.assertRaises(target.PublishTargetError):
            _legacy_publish_target(stack_dir="relative/stack")

        with self.assertRaises(target.PublishTargetError):
            _legacy_publish_target(stack_dir="/")

        endpoint = target.PublishSshEndpoint("example.org", 22, None)
        with self.assertRaisesRegex(target.PublishTargetError, "restart\\.stack_dir"):
            target.PublishRestartTarget(
                "compose",
                endpoint,
                "/",
                "minecraft",
            )

        with self.assertRaises(target.PublishTargetError):
            _legacy_publish_target(service="bad service")

        with self.assertRaises(target.PublishTargetError):
            target.PublishRestartTarget(
                "compose",
                endpoint,
                target.validate_publish_remote_path("/srv/restart/demo"),
                "minecraft",
                enabled=False,
            )


class PublishTargetCoreIntegrationTest(unittest.TestCase):
    @patch.object(core.packctl, "deployment_settings")
    def test_core_resolver_maps_effective_legacy_settings(self, deployment_settings) -> None:
        deployment_settings.return_value = packctl.DeploymentSettings(
            "publisher@[2001:db8::1]:/srv/packs/demo",
            "restart.example.org",
            "/srv/restart/demo",
            "minecraft",
        )

        resolved = core.resolve_publish_remote_target(
            "demo",
            remote_path="/srv/packs/override",
        )

        deployment_settings.assert_called_once_with("demo")
        self.assertEqual(resolved.publication_endpoint.host, "2001:db8::1")
        self.assertEqual(resolved.publication_endpoint.user, "publisher")
        self.assertEqual(resolved.publication_root, PurePosixPath("/srv/packs/override"))
        self.assertTrue(resolved.restart.enabled)

    def test_core_resolver_rejects_unimplemented_named_server_profiles(self) -> None:
        with self.assertRaisesRegex(core.HuroshikiError, "not implemented"):
            core.resolve_publish_remote_target("demo", server_id="production")


if __name__ == "__main__":
    unittest.main()
