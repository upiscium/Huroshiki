from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "deploy" / "packwiz-web" / "default.conf"


class PackwizWebConfigTest(unittest.TestCase):
    def test_public_paths_resolve_through_active_generations(self) -> None:
        config = CONFIG.read_text(encoding="utf-8")

        expected_routes = (
            (
                "location ~ ^/(?<pack>[a-z0-9][a-z0-9._-]*)/client/current/"
                "(?<asset>.+)$",
                "alias /usr/share/nginx/html/$pack/client/current/$asset;",
            ),
            (
                "location ~ ^/(?<pack>[a-z0-9][a-z0-9._-]*)/current/"
                "(?<asset>.+)$",
                "alias /usr/share/nginx/html/$pack/current/$asset;",
            ),
            (
                "location ~ ^/(?<pack>[a-z0-9][a-z0-9._-]*)/client/"
                "(?<asset>.+)$",
                "alias /usr/share/nginx/html/$pack/client/current/$asset;",
            ),
            (
                "location ~ ^/(?<pack>[a-z0-9][a-z0-9._-]*)/server/"
                "(?<asset>.+)$",
                "alias /usr/share/nginx/html/$pack/current/$asset;",
            ),
            (
                "location ~ ^/(?<pack>[a-z0-9][a-z0-9._-]*)/(?<asset>.+)$",
                "alias /usr/share/nginx/html/$pack/current/$asset;",
            ),
        )

        for location, alias in expected_routes:
            with self.subTest(location=location):
                start = config.index(location)
                end = config.index("}", start)
                self.assertIn(alias, config[start:end])

        self.assertNotIn("try_files $uri", config)


if __name__ == "__main__":
    unittest.main()
