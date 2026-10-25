"""Behavior checks for the authenticated smoke HTTP boundary."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("page_smoke", Path(__file__).with_name("page_smoke.py"))
page_smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(page_smoke)


class FetchSchemeTests(unittest.TestCase):
    def test_rejects_local_file_urls_before_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "synthetic.txt"
            artifact.write_text("synthetic smoke fixture", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "only http and https"):
                page_smoke._fetch(artifact.as_uri(), "synthetic-session", "sessionid")


if __name__ == "__main__":
    unittest.main()
