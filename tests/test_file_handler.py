"""Keep the newest delivered URLs when a bounded history rolls over."""

import tempfile
import unittest
from pathlib import Path

from src.utils.file_handler import load_lines, save_lines


class SeenHistoryTests(unittest.TestCase):
    def test_new_url_survives_cap_even_when_it_sorts_before_old_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.txt"
            old_urls = [f"https://z.example/{index:04}" for index in range(5000)]
            save_lines(path, old_urls)
            new_url = "https://a.example/new"
            save_lines(path, set(old_urls) | {new_url})
            saved = load_lines(path)
        self.assertEqual(len(saved), 5000)
        self.assertNotIn(old_urls[0], saved)
        self.assertEqual(saved, old_urls[1:] + [new_url])

    def test_multiple_rollovers_preserve_recency_and_remove_duplicate_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.txt"
            save_lines(path, ["old-z", "old-z", "middle-y", "recent-x"], cap=10)
            save_lines(path, {"old-z", "middle-y", "recent-x", "new-b"}, cap=3)
            self.assertEqual(load_lines(path), ["middle-y", "recent-x", "new-b"])
            save_lines(path, set(load_lines(path)) | {"new-a"}, cap=3)
            self.assertEqual(load_lines(path), ["recent-x", "new-b", "new-a"])

    def test_title_lists_keep_caller_order_and_do_not_deduplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "titles.txt"
            save_lines(path, ["z", "a", "a", "b"], cap=3)
            self.assertEqual(load_lines(path), ["a", "a", "b"])


if __name__ == "__main__":
    unittest.main()
