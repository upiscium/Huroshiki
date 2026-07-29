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
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

import huroshiki_core as core
import packctl
import url_artifacts
from template_import import template_candidate


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
        (self.pack_root / "pack.local.yaml").write_text(
            "url_allow_private_networks: true\n", encoding="utf-8"
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
                allow_private_networks=True,
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
            f"url_max_jar_size_bytes: {len(payload)}\n"
            "url_allow_private_networks: true\n",
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
            f"url_max_jar_size_bytes: {len(payload)}\n"
            "url_allow_private_networks: true\n",
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

    def test_closure_resolver_passes_url_policy_to_downloader(self) -> None:
        artifact = core.UrlArtifact(
            "Private MOD",
            "private_mod",
            "1.0.0",
            "private.jar",
            "https://mods.example/private.jar",
            "00",
            ("neoforge",),
        )
        with patch.object(
            core, "download_url_artifact", return_value=artifact
        ) as download:
            closure = core.resolve_mod_closure(
                provider="url",
                selector="https://mods.example/private.jar",
                minecraft="1.21.1",
                loader="neoforge",
                loader_version="21.1.234",
                url_max_jar_size_bytes=1234,
                url_allow_private_networks=True,
            )
        self.assertEqual(closure.root_identity, ("url", "private_mod"))
        self.assertEqual(download.call_args.args[4], 1234)
        self.assertTrue(download.call_args.kwargs["allow_private_networks"])

    def test_import_private_network_rejection_is_candidate_local(self) -> None:
        payload = self.jar_bytes()
        with serve_bytes(payload) as url:
            candidate = template_candidate(
                "base",
                name="Private",
                provider="url",
                project_id="private",
                side="both",
                url=url,
                url_allow_private_networks=False,
            )
            results = core.verify_import_candidates(
                (candidate,),
                minecraft="1.21.1",
                loader="neoforge",
                loader_version="21.1.234",
                cancel_event=threading.Event(),
                deadline=time.monotonic() + 30,
            )
        self.assertFalse(results[0].succeeded)
        self.assertIn("private", results[0].error.lower())

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
                allow_private_networks=True,
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

        with patch.object(url_artifacts, "MAX_ZIP_ENTRIES", 2), patch.object(
            url_artifacts.zipfile, "ZipFile", wraps=zipfile.ZipFile
        ) as zip_file:
            with self.assertRaisesRegex(core.HuroshikiError, "more than 2 entries"):
                core.parse_jar_identity(path, "fabric")
        zip_file.assert_not_called()

    def test_jar_identity_rejects_zip64_sentinel_before_open(self) -> None:
        path = self.root / "zip64.jar"
        write_fabric_jar(path, "1.0.0")
        data = bytearray(path.read_bytes())
        eocd = data.rfind(b"PK\x05\x06")
        struct.pack_into("<H", data, eocd + 10, 0xFFFF)
        path.write_bytes(data)

        with patch.object(
            url_artifacts.zipfile, "ZipFile", wraps=zipfile.ZipFile
        ) as zip_file:
            with self.assertRaisesRegex(core.HuroshikiError, "ZIP64"):
                core.parse_jar_identity(path, "fabric")
        zip_file.assert_not_called()

    def test_jar_identity_accepts_archive_comment(self) -> None:
        path = self.root / "commented.jar"
        write_fabric_jar(path, "1.0.0")
        with zipfile.ZipFile(path, "a") as jar:
            jar.comment = b"valid archive comment"

        mod_id, _name, version, loaders = core.parse_jar_identity(path, "fabric")

        self.assertEqual((mod_id, version, loaders), ("private_mod", "1.0.0", ("fabric",)))

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

    def test_cancellation_bounds_blocked_opener(self) -> None:
        self._assert_blocked_opener_is_bounded(cancel=True)

    def test_deadline_bounds_blocked_opener(self) -> None:
        self._assert_blocked_opener_is_bounded(cancel=False)

    def _assert_blocked_opener_is_bounded(self, *, cancel: bool) -> None:
        entered = threading.Event()
        release = threading.Event()
        cancel_event = threading.Event()

        def blocked_open(*args, **kwargs):
            entered.set()
            release.wait()
            return LateResponse()

        class LateResponse:
            def close(self) -> None:
                pass

        def cancel_after_open() -> None:
            entered.wait(1)
            cancel_event.set()

        canceller = threading.Thread(target=cancel_after_open)
        if cancel:
            canceller.start()
        try:
            with patch.object(
                url_artifacts, "_open_validated_url", side_effect=blocked_open
            ):
                with self.assertRaisesRegex(
                    core.HuroshikiError,
                    "cancelled" if cancel else "deadline exceeded",
                ):
                    core.download_url_artifact(
                        "https://example.invalid/private.jar",
                        cancel_event,
                        self.root / "blocked-open-logs",
                        "neoforge",
                        total_timeout_seconds=10 if cancel else 0.02,
                    )
        finally:
            release.set()
            if cancel:
                canceller.join(1)

    def test_abandoned_opener_closes_late_response(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        cancel_event = threading.Event()

        class LateResponse:
            def close(self) -> None:
                closed.set()

        def blocked_open(*args, **kwargs):
            entered.set()
            release.wait()
            return LateResponse()

        def cancel_after_open() -> None:
            entered.wait(1)
            cancel_event.set()

        canceller = threading.Thread(target=cancel_after_open)
        canceller.start()
        with patch.object(
            url_artifacts, "_open_validated_url", side_effect=blocked_open
        ):
            with self.assertRaisesRegex(core.HuroshikiError, "cancelled"):
                core.download_url_artifact(
                    "https://example.invalid/private.jar",
                    cancel_event,
                    self.root / "late-open-logs",
                    "neoforge",
                    total_timeout_seconds=10,
                )
            release.set()
            self.assertTrue(closed.wait(1))
        canceller.join(1)

    def test_successful_mocked_open_transfers_response_ownership(self) -> None:
        payload = self.jar_bytes()

        class Response:
            headers = {"Content-Length": str(len(payload))}
            chunked = False
            length = len(payload)

            def __init__(self) -> None:
                self.offset = 0
                self.closed = False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

            def read(self, size: int) -> bytes:
                chunk = payload[self.offset : self.offset + size]
                self.offset += len(chunk)
                return chunk

            def close(self) -> None:
                self.closed = True

        response = Response()
        with patch.object(
            url_artifacts, "_open_validated_url", return_value=response
        ):
            artifact = core.download_url_artifact(
                "https://example.invalid/private.jar",
                threading.Event(),
                self.root / "successful-open-logs",
                "neoforge",
            )

        self.assertEqual(artifact.mod_id, "private_mod")
        self.assertTrue(response.closed)

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

        with patch.object(
            url_artifacts, "_open_validated_url", return_value=BlockingResponse()
        ):
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
                f"url_max_jar_size_bytes: {jar_path.stat().st_size}\n"
                "url_allow_private_networks: true\n",
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

    def test_rejects_private_resolution_by_default_and_allows_explicit_opt_in(self) -> None:
        with patch.object(
            url_artifacts.socket,
            "getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 80))],
        ):
            with self.assertRaisesRegex(core.HuroshikiError, "prohibited address"):
                url_artifacts._approved_addresses(
                    "example.test", 80, allow_private_networks=False
                )
            self.assertEqual(
                url_artifacts._approved_addresses(
                    "example.test", 80, allow_private_networks=True
                ),
                ("127.0.0.1",),
            )

    def test_nat64_prefixes_require_private_network_opt_in(self) -> None:
        for address in (
            "64:ff9b::a9fe:a9fe",
            "64:ff9b:1::7f00:1",
        ):
            with self.subTest(address=address), patch.object(
                url_artifacts.socket,
                "getaddrinfo",
                return_value=[(10, 1, 6, "", (address, 80, 0, 0))],
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "prohibited address"):
                    url_artifacts._approved_addresses(
                        "example.test", 80, allow_private_networks=False
                    )
                self.assertEqual(
                    url_artifacts._approved_addresses(
                        "example.test", 80, allow_private_networks=True
                    ),
                    (address,),
                )

    def test_private_opt_in_still_rejects_non_unicast_nonsense(self) -> None:
        for address in ("::", "ff02::1", "2001:db8::1", "240.0.0.1"):
            with self.subTest(address=address), patch.object(
                url_artifacts.socket,
                "getaddrinfo",
                return_value=[(10, 1, 6, "", (address, 80, 0, 0))],
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "prohibited address"):
                    url_artifacts._approved_addresses(
                        "example.test", 80, allow_private_networks=True
                    )

    def test_public_redirect_to_private_resolution_is_rejected(self) -> None:
        class RedirectResponse:
            status = 302
            headers = {"Location": "http://internal.test/private.jar"}

            def close(self) -> None:
                pass

        class Connection:
            def __init__(self, *args) -> None:
                pass

            def request(self, *args, **kwargs) -> None:
                pass

            def getresponse(self):
                return RedirectResponse()

            def close(self) -> None:
                pass

        def resolve(hostname, port, *, allow_private_networks):
            if hostname == "public.test":
                return ("203.0.113.10",)
            raise core.HuroshikiError("prohibited address 127.0.0.1")

        with patch.object(
            url_artifacts, "_approved_addresses", side_effect=resolve
        ), patch.object(url_artifacts, "_PinnedHTTPConnection", Connection):
            with self.assertRaisesRegex(core.HuroshikiError, "prohibited address"):
                url_artifacts._open_validated_url(
                    "http://public.test/public.jar",
                    timeout=1,
                    allow_private_networks=False,
                )

    def test_percent_decoded_traversal_and_windows_device_filenames_are_rejected(self) -> None:
        for url in (
            "https://example.test/%2e%2e%2fescape.jar",
            "https://example.test/CON.jar",
            "https://example.test/CONIN$.jar",
            "https://example.test/conout$.JAR",
            "https://example.test/COM%C2%B9.jar",
            "https://example.test/LPT%C2%B3.backup.jar",
        ):
            with self.subTest(url=url), self.assertRaises(core.HuroshikiError):
                core.validate_public_url(url)

    def test_direct_url_add_rejects_portable_filename_collision(self) -> None:
        existing = self.pack_root / "source/mods/existing.pw.toml"
        existing.write_text(
            'name = "Existing"\nfilename = "Same.jar"\nside = "both"\n'
            '[download]\nurl = "https://example.test/existing.jar"\n',
            encoding="utf-8",
        )
        artifact = core.UrlArtifact(
            "Incoming",
            "incoming",
            "1.0",
            "same.jar",
            "https://example.test/same.jar",
            "00",
            ("neoforge",),
        )

        with self.assertRaisesRegex(core.HuroshikiError, "Portable filename collision"):
            core.write_url_metadata(
                self.pack_root / "source",
                Path("mods/incoming.pw.toml"),
                artifact,
                "both",
            )
        self.assertFalse((self.pack_root / "source/mods/incoming.pw.toml").exists())

    def _assert_real_stream_interrupts(self, *, trickle: bool, cancel: bool) -> None:
        entered = threading.Event()
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Length", "1000000")
                self.end_headers()
                entered.set()
                try:
                    if trickle:
                        while not release.wait(0.05):
                            self.wfile.write(b"x")
                            self.wfile.flush()
                    else:
                        release.wait(5)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, format: str, *args) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cancel_event = threading.Event()
        canceller = None
        if cancel:
            def cancel_after_read_starts() -> None:
                entered.wait(1)
                cancel_event.set()

            canceller = threading.Thread(target=cancel_after_read_starts)
            canceller.start()
        started = time.monotonic()
        named_temporary_file = tempfile.NamedTemporaryFile

        def temporary_file(**kwargs):
            return named_temporary_file(dir=self.downloads, **kwargs)

        try:
            with patch.object(
                core.tempfile, "NamedTemporaryFile", side_effect=temporary_file
            ):
                with self.assertRaisesRegex(
                    core.HuroshikiError,
                    "cancelled" if cancel else "deadline exceeded",
                ):
                    core.download_url_artifact(
                        f"http://127.0.0.1:{server.server_port}/private.jar",
                        cancel_event,
                        self.root / "real-stream-logs",
                        "neoforge",
                        total_timeout_seconds=5 if cancel else 0.2,
                        allow_private_networks=True,
                    )
            self.assertLess(time.monotonic() - started, 0.8)
            self.assert_downloads_cleaned()
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(1)
            if canceller is not None:
                canceller.join(1)

    def test_real_stalled_read_is_interrupted_by_cancellation(self) -> None:
        self._assert_real_stream_interrupts(trickle=False, cancel=True)

    def test_real_trickling_read_is_interrupted_by_total_deadline(self) -> None:
        self._assert_real_stream_interrupts(trickle=True, cancel=False)


if __name__ == "__main__":
    unittest.main()
