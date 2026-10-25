import unittest
from urllib.parse import unquote, urlsplit

from proxy_config import proxy_tiers, scrapingbee_proxy_url


class ProxyConfigTests(unittest.TestCase):
    def test_tiers_are_three_distinct_configs(self):
        tiers = proxy_tiers("x:y@123")
        self.assertEqual(len(tiers), 3)
        self.assertTrue(all(len(tier) == 1 for tier in tiers))
        passwords = [unquote(urlsplit(tier[0]).password) for tier in tiers]
        self.assertEqual(passwords, [
            "render_js=False",
            "render_js=False&premium_proxy=True",
            "render_js=True&premium_proxy=True",
        ])

    def test_special_characters_are_escaped(self):
        url = scrapingbee_proxy_url("my:key@abc")
        self.assertIn("my%3Akey%40abc", url)
        self.assertEqual(urlsplit(url).hostname, "proxy.scrapingbee.com")

    def test_missing_key_rejected(self):
        with self.assertRaises(ValueError):
            scrapingbee_proxy_url("")


if __name__ == "__main__":
    unittest.main()
