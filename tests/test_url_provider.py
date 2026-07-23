from __future__ import annotations

from contextlib import contextmanager
from functools import partial
from http.server import (
    BaseHTTPRequestHandler,
    SimpleHTTPRequestHandler,
    ThreadingHTTPServer,
)
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import huroshiki_core as core
import packctl
import url_artifacts


PACK_TOML = '''name = "Demo"
author = "tester"
version = "0.1.0"
pack-format = "packwiz:1.1.0"
[index]
file = "index.toml"
hash-format = "sha256"
hash = "placeholder"
[versions]
minecraft = "1.21.1"
neoforge = "21.1.234"
'''


def write_neoforge_jar(path: Path, version: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = f'''modLoader="javafml"
loaderVersion="[4,)"
license="MIT"
[[mods]]
modId="private_mod"
version="{version}"
displayName="Private MOD"
'''
    with zipfile.ZipFile(path, "w") as jar:
        jar.writestr("META-INF/neoforge.mods.toml", metadata)


def write_fabric_jar(path: Path, version: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as jar:
        jar.writestr(
            "fabric.mod.json",
            '{"id":"private_mod","name":"Private MOD","version":"'
            + version
            + '"}',
        )


@contextmanager
def serve(directory: Path):
    handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


AUTO_CONTENT_LENGTH = object()


@contextmanager
def serve_bytes(payload: bytes, content_length=AUTO_CONTENT_LENGTH):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self) -> None:
            self.send_response(200)
            if content_length is AUTO_CONTENT_LENGTH:
                self.send_header("Content-Length", str(len(payload)))
            elif content_length is not None:
                self.send_header("Content-Length", str(content_length))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except BrokenPipeError:
                pass

        def log_message(self, format: str, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/private-mod.jar"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class UrlProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.pack_root = self.packs / "demo"
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        source = self.pack_root / "source"
        (source / "mods").mkdir(parents=True)
        (source / "pack.toml").write_text(PACK_TOML, encoding="utf-8")
        (source / "index.toml").write_text(
            'hash-format = "sha256"\n', encoding="utf-8"
        )
        (self.pack_root / "pack.yaml").write_text(
            '''id: demo
display_name: Demo
enabled: true
minecraft: 1.21.1
loader: neoforge
loader_version: 21.1.234
''',
            encoding="utf-8",
        )
        self.patches = [
            patch.object(core, "ROOT", self.root),
            patch.object(core, "PACKS", self.packs),
            patch.object(core, "TEMPLATES", self.templates),
            patch.object(core, "STATE_ROOT", self.root / ".huroshiki"),
            patch.object(
                core,
                "TRANSACTION_ROOT",
                self.root / ".huroshiki" / "transactions",
            ),
            patch.object(core, "LOG_ROOT", self.root / ".huroshiki" / "logs"),
            patch.object(packctl, "ROOT", self.root),
            patch.object(packctl, "PACKS", self.packs),
            patch.object(packctl, "TEMPLATES", self.templates),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def jar_bytes(self) -> bytes:
        path = self.root / "payload.jar"
        write_neoforge_jar(path, "1.0.0")
        return path.read_bytes()

    def download_response(
        self,
        payload: bytes,
        *,
        content_length=AUTO_CONTENT_LENGTH,
        limit: int,
        cancel_event: threading.Event | None = None,
    ) -> core.UrlArtifact:
        named_temporary_file = tempfile.NamedTemporaryFile

        def temporary_file(**kwargs):
            return named_temporary_file(dir=self.downloads, **kwargs)

        with serve_bytes(payload, content_length) as url, patch.object(
            core.tempfile,
            "NamedTemporaryFile",
            side_effect=temporary_file,
        ):
            return core.download_url_artifact(
                url,
                cancel_event or threading.Event(),
                self.root / "http-logs",
                "neoforge",
                limit,
            )

    def assert_downloads_cleaned(self) -> None:
        self.assertEqual(list(self.downloads.iterdir()), [])

    def test_download_accepts_valid_content_length(self) -> None:
        payload = self.jar_bytes()

        artifact = self.download_response(payload, limit=len(payload))

        self.assertEqual(artifact.mod_id, "private_mod")
        self.assert_downloads_cleaned()

    def test_download_rejects_oversized_content_length_before_body(self) -> None:
        with self.assertRaisesRegex(
            core.HuroshikiError,
            r"limit of 64 bytes: declared size is 65 bytes",
        ):
            self.download_response(b"", content_length=65, limit=64)

        self.assert_downloads_cleaned()

    def test_download_accepts_missing_content_length(self) -> None:
        payload = self.jar_bytes()

        artifact = self.download_response(
            payload,
            content_length=None,
            limit=len(payload),
        )

        self.assertEqual(artifact.mod_id, "private_mod")
        self.assert_downloads_cleaned()

    def test_download_enforces_bytes_with_malformed_content_length(self) -> None:
        with self.assertRaisesRegex(
            core.HuroshikiError,
            r"limit of 32 bytes: received 33 bytes$",
        ):
            self.download_response(
                b"x" * 64,
                content_length="invalid",
                limit=32,
            )

        self.assert_downloads_cleaned()

    def test_download_detects_understated_content_length(self) -> None:
        with self.assertRaisesRegex(
            core.HuroshikiError,
            r"limit of 32 bytes: received 33 bytes \(declared 8 bytes\)",
        ):
            self.download_response(b"x" * 64, content_length=8, limit=32)

        self.assert_downloads_cleaned()

    def test_download_stops_stream_without_content_length_at_limit(self) -> None:
        with self.assertRaisesRegex(
            core.HuroshikiError,
            r"limit of 32 bytes: received 33 bytes$",
        ):
            self.download_response(b"x" * 4096, content_length=None, limit=32)

        self.assert_downloads_cleaned()

    def test_cancelled_download_cleans_temporary_file(self) -> None:
        cancelled = threading.Event()
        cancelled.set()

        with self.assertRaisesRegex(core.HuroshikiError, "download cancelled"):
            self.download_response(
                self.jar_bytes(),
                limit=core.DEFAULT_URL_MAX_JAR_SIZE_BYTES,
                cancel_event=cancelled,
            )

        self.assert_downloads_cleaned()

    def test_pack_local_config_overrides_committed_url_limit(self) -> None:
        payload = self.jar_bytes()
        with (self.pack_root / "pack.yaml").open("a", encoding="utf-8") as config:
            config.write("url_max_jar_size_bytes: 1\n")
        (self.pack_root / "pack.local.yaml").write_text(
            f"url_max_jar_size_bytes: {len(payload)}\n",
            encoding="utf-8",
        )

        with serve_bytes(payload) as url:
            transaction = core.PackTransaction.create(core.project_key("pack", "demo"))
            result = transaction.begin_add(
                "url", url, client=True, server=True
            ).run()

        self.assertTrue(result.success, result.message)

    def test_template_local_config_controls_resolver_url_limit(self) -> None:
        payload = self.jar_bytes()
        template_root = self.templates / "private"
        template_root.mkdir(parents=True)
        (template_root / "template.yaml").write_text(
            "id: private\n"
            "display_name: Private\n"
            "enabled: true\n"
            "minecraft: 1.21.1\n"
            "loader: neoforge\n"
            "reference_loader_version: 21.1.234\n"
            "url_max_jar_size_bytes: 1\n"
            "mods: []\n",
            encoding="utf-8",
        )
        (template_root / "template.local.yaml").write_text(
            f"url_max_jar_size_bytes: {len(payload)}\n",
            encoding="utf-8",
        )

        with serve_bytes(payload) as url:
            transaction = core.PackTransaction.create(
                core.project_key("template", "private")
            )
            result = transaction.begin_add(
                "url", url, client=True, server=True
            ).run()

        self.assertTrue(result.success, result.message)

    def test_url_add_and_new_url_replace_same_metadata(self) -> None:
        public = self.root / "public" / "private-mod"
        write_neoforge_jar(public / "1.0.0" / "private-mod-1.0.0.jar", "1.0.0")
        write_neoforge_jar(public / "1.1.0" / "private-mod-1.1.0.jar", "1.1.0")

        with serve(self.root / "public") as base_url:
            transaction = core.PackTransaction.create(core.project_key("pack", "demo"))
            first_url = f"{base_url}/private-mod/1.0.0/private-mod-1.0.0.jar"
            first = transaction.begin_add(
                "url", first_url, client=True, server=False
            ).run()
            self.assertTrue(first.success)
            staged = transaction.staged_mods()
            self.assertEqual(len(staged), 1)
            self.assertEqual(staged[0].project_id, "private_mod")
            self.assertEqual(staged[0].provider, "URL")
            self.assertEqual(staged[0].source_url, first_url)
            self.assertTrue(staged[0].client)
            self.assertFalse(staged[0].server)

            second_url = f"{base_url}/private-mod/1.1.0/private-mod-1.1.0.jar"
            second = transaction.begin_add(
                "url", second_url, client=True, server=True
            ).run()
            self.assertTrue(second.success)
            staged = transaction.staged_mods()
            self.assertEqual(len(staged), 1)
            self.assertEqual(staged[0].relative_path, Path("mods/private_mod.pw.toml"))
            self.assertEqual(staged[0].filename, "private-mod-1.1.0.jar")
            self.assertEqual(staged[0].source_url, second_url)
            self.assertTrue(staged[0].server)

    def test_url_add_rejects_jar_for_wrong_loader(self) -> None:
        public = self.root / "public" / "private-mod.jar"
        write_fabric_jar(public, "1.0.0")

        with serve(self.root / "public") as base_url:
            transaction = core.PackTransaction.create(core.project_key("pack", "demo"))
            result = transaction.begin_add(
                "url", f"{base_url}/private-mod.jar", client=True, server=True
            ).run()

        self.assertFalse(result.success)
        self.assertIn("supports fabric, not neoforge", result.message)
        self.assertEqual(transaction.staged_mods(), [])

    def test_url_add_accepts_multi_loader_jar_and_preserves_identity(self) -> None:
        public = self.root / "public" / "private-mod.jar"
        write_neoforge_jar(public, "1.0.0")
        with zipfile.ZipFile(public, "a") as jar:
            jar.writestr(
                "fabric.mod.json",
                '{"id":"fabric_alias","name":"Fabric Alias","version":"1.0.0"}',
            )

        with serve(self.root / "public") as base_url:
            transaction = core.PackTransaction.create(core.project_key("pack", "demo"))
            result = transaction.begin_add(
                "url", f"{base_url}/private-mod.jar", client=True, server=True
            ).run()

        self.assertTrue(result.success)
        staged = transaction.staged_mods()
        self.assertEqual(staged[0].project_id, "private_mod")
        self.assertEqual(staged[0].name, "Private MOD")

    def test_multi_loader_jar_uses_target_loader_identity(self) -> None:
        public = self.root / "public" / "private-mod.jar"
        write_neoforge_jar(public, "2.0.0")
        with zipfile.ZipFile(public, "a") as jar:
            jar.writestr(
                "fabric.mod.json",
                '{"id":"fabric_id","name":"Fabric Name","version":"1.0.0"}',
            )
        with serve(self.root / "public") as base_url:
            artifact = core.download_url_artifact(
                f"{base_url}/private-mod.jar",
                threading.Event(),
                self.root / "logs",
                "fabric",
            )

        self.assertEqual(artifact.mod_id, "fabric_id")
        self.assertEqual(artifact.name, "Fabric Name")
        self.assertEqual(artifact.version, "1.0.0")
        self.assertEqual(artifact.loaders, ("neoforge", "fabric"))

    def test_url_add_rejects_jar_without_mod_metadata(self) -> None:
        public = self.root / "public" / "library.jar"
        public.parent.mkdir(parents=True)
        with zipfile.ZipFile(public, "w") as jar:
            jar.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")

        with serve(self.root / "public") as base_url:
            transaction = core.PackTransaction.create(core.project_key("pack", "demo"))
            result = transaction.begin_add(
                "url", f"{base_url}/library.jar", client=True, server=True
            ).run()

        self.assertFalse(result.success)
        self.assertIn("does not contain recognized mod metadata", result.message)
        self.assertEqual(transaction.staged_mods(), [])

    def test_jar_identity_rejects_excessive_archive_entries(self) -> None:
        path = self.root / "many.jar"
        with zipfile.ZipFile(path, "w") as jar:
            jar.writestr("fabric.mod.json", '{"id":"demo"}')
            jar.writestr("one", "")
            jar.writestr("two", "")

        with patch.object(url_artifacts, "MAX_ZIP_ENTRIES", 2):
            with self.assertRaisesRegex(core.HuroshikiError, "more than 2 entries"):
                core.parse_jar_identity(path, "fabric")

    def test_jar_identity_rejects_oversized_recognized_metadata(self) -> None:
        path = self.root / "metadata-bomb.jar"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as jar:
            jar.writestr("fabric.mod.json", b" " * 1024)

        with patch.object(url_artifacts, "MAX_METADATA_ENTRY_SIZE_BYTES", 32):
            with self.assertRaisesRegex(core.HuroshikiError, "exceeds 32 bytes"):
                core.parse_jar_identity(path, "fabric")

    def test_cancellation_closes_blocked_response_and_bounds_worker(self) -> None:
        self._assert_blocked_response_is_closed(cancel=True)

    def test_deadline_closes_blocked_response_and_bounds_worker(self) -> None:
        self._assert_blocked_response_is_closed(cancel=False)

    def _assert_blocked_response_is_closed(self, *, cancel: bool) -> None:
        entered = threading.Event()
        closed = threading.Event()
        cancel_event = threading.Event()
        errors: list[BaseException] = []

        class BlockingResponse:
            headers: dict[str, str] = {}
            chunked = False
            length = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

            def read(self, size: int) -> bytes:
                entered.set()
                closed.wait()
                raise ValueError("response closed")

            def close(self) -> None:
                closed.set()

        def download() -> None:
            try:
                core.download_url_artifact(
                    "https://example.invalid/private.jar",
                    cancel_event,
                    self.root / "blocked-logs",
                    "neoforge",
                    total_timeout_seconds=10 if cancel else 0.02,
                )
            except BaseException as error:
                errors.append(error)

        with patch.object(url_artifacts, "urlopen", return_value=BlockingResponse()):
            worker = threading.Thread(target=download)
            worker.start()
            self.assertTrue(entered.wait(1))
            if cancel:
                cancel_event.set()
            worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertTrue(closed.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIn(
            "cancelled" if cancel else "deadline exceeded",
            str(errors[0]),
        )

    def test_url_template_manifest_is_supported(self) -> None:
        template_root = self.templates / "private"
        template_root.mkdir(parents=True)
        (template_root / "template.yaml").write_text(
            '''id: private
display_name: Private
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Private MOD
    provider: url
    project_id: private_mod
    url: https://mods.example.invalid/private-mod-1.0.0.jar
    side: both
''',
            encoding="utf-8",
        )
        mods = packctl.template_mods("private")
        self.assertEqual(mods[0]["provider"], "url")
        self.assertEqual(mods[0]["project_id"], "private_mod")
        self.assertEqual(
            mods[0]["url"],
            "https://mods.example.invalid/private-mod-1.0.0.jar",
        )
        listed = core.list_mods(core.project_key("template", "private"))
        self.assertEqual(listed[0].provider, "URL")
        self.assertEqual(listed[0].project_id, "private_mod")

    def test_create_pack_from_url_template_installs_available_jar(self) -> None:
        public = self.root / "public" / "private-mod"
        jar_path = public / "1.0.0" / "private-mod-1.0.0.jar"
        write_neoforge_jar(jar_path, "1.0.0")

        with serve(self.root / "public") as base_url:
            template_root = self.templates / "private"
            template_root.mkdir(parents=True)
            public_url = (
                f"{base_url}/private-mod/1.0.0/private-mod-1.0.0.jar"
            )
            template_yaml = (
                "id: private\n"
                "display_name: Private\n"
                "enabled: true\n"
                "minecraft: 1.21.1\n"
                "loader: neoforge\n"
                "reference_loader_version: 21.1.234\n"
                "mods:\n"
                "  - name: Private MOD\n"
                "    provider: url\n"
                "    project_id: private_mod\n"
                f"    url: {public_url}\n"
                "    side: server\n"
            )
            (template_root / "template.yaml").write_text(
                template_yaml,
                encoding="utf-8",
            )
            with (template_root / "template.yaml").open(
                "a", encoding="utf-8"
            ) as config:
                config.write("url_max_jar_size_bytes: 1\n")
            (template_root / "template.local.yaml").write_text(
                f"url_max_jar_size_bytes: {jar_path.stat().st_size}\n",
                encoding="utf-8",
            )

            def fake_create(*args):
                generated = self.packs / "generated"
                source = generated / "source"
                (source / "mods").mkdir(parents=True)
                (source / "pack.toml").write_text(
                    PACK_TOML, encoding="utf-8"
                )
                (source / "index.toml").write_text(
                    'hash-format = "sha256"\n', encoding="utf-8"
                )
                (generated / "pack.yaml").write_text(
                    "id: generated\n"
                    "display_name: Generated\n"
                    "enabled: true\n"
                    "minecraft: 1.21.1\n"
                    "loader: neoforge\n"
                    "loader_version: 21.1.999\n",
                    encoding="utf-8",
                )
                return 0

            real_run = core.subprocess.run

            def fake_run(command, **kwargs):
                if command[-1] == "refresh":
                    return core.subprocess.CompletedProcess(command, 0, "", "")
                return real_run(command, **kwargs)

            with patch.object(
                core, "create_project", side_effect=fake_create
            ), patch.object(
                core.subprocess, "run", side_effect=fake_run
            ), patch.object(
                core,
                "download_url_artifact",
                wraps=core.download_url_artifact,
            ) as download:
                report = core.create_pack_from_template(
                    template_id="private",
                    project_id="generated",
                    display_name="Generated",
                    minecraft="1.21.1",
                    loader="neoforge",
                    loader_version="21.1.999",
                )

            self.assertEqual(report.installed, ("Private MOD",))
            self.assertEqual(report.failed, ())
            self.assertEqual(download.call_args.args[4], jar_path.stat().st_size)
            installed = core.read_mod(
                self.packs / "generated" / "source",
                Path("mods/private_mod.pw.toml"),
            )
            self.assertFalse(installed.client)
            self.assertTrue(installed.server)
            self.assertEqual(installed.source_url, public_url)

    def test_rejects_non_http_or_non_jar_url(self) -> None:
        with self.assertRaises(core.HuroshikiError):
            core.normalize_add_selector("url", "file:///tmp/private.jar")
        with self.assertRaises(core.HuroshikiError):
            core.normalize_add_selector("url", "https://example.invalid/private.zip")


if __name__ == "__main__":
    unittest.main()
