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
    values = sorted(items) if isinstance(items, set) else list(items)
    content = "\n".join(values[-cap:])
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            if f.read() == content:
                return
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
