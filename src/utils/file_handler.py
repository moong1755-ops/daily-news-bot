"""File I/O utilities for news data persistence."""

import os
from pathlib import Path


DATA_DIR = Path(__file__).parent.parent.parent / "data"
SEEN_FILE = DATA_DIR / "seen_news.txt"
SEEN_TITLES_FILE = DATA_DIR / "seen_titles.txt"


def load_lines(path) -> list:
    """Load lines from a file."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


def save_lines(path, items, cap=5000):
    """Save items to file with optional cap on number of lines."""
    if isinstance(items, (set, frozenset)):
        # Existing file order records age; a set's alphabetic order does not.
        # Legacy files cannot recover older chronology, but keep new entries last.
        previous = list(dict.fromkeys(load_lines(path)))
        values = [value for value in previous if value in items]
        values.extend(sorted(items.difference(previous)))
    else:
        values = list(items)
    content = "\n".join(values[-cap:] if cap is not None else values)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            if f.read() == content:
                return
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
