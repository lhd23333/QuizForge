"""设置页与关于页的 GitHub 开源入口回归。"""

import unittest

from app import app
import config


class OpenSourceLinksTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_settings_has_repository_and_releases_links(self):
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn(config.GITHUB_REPOSITORY_URL, html)
        self.assertIn(config.GITHUB_RELEASES_URL, html)
        self.assertNotIn("邀请码", html)

    def test_about_has_open_source_license_and_releases_link(self):
        html = self.client.get("/about").get_data(as_text=True)
        self.assertIn("GPL-3.0-or-later", html)
        self.assertIn(config.GITHUB_RELEASES_URL, html)

    def test_storefront_endpoint_is_removed(self):
        self.assertEqual(self.client.get("/api/storefront").status_code, 404)


if __name__ == "__main__":
    unittest.main()
