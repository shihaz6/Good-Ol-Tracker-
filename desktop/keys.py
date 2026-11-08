"""API keys: environment variable first, else data/<name>_key.txt (gitignored).

    GROQ_API_KEY   or  data/groq_key.txt
    VOYAGE_API_KEY or  data/voyage_key.txt
"""

import os

import db

USER_AGENT = "GoodOlTracker/0.5 (+local personal tracker)"


def load_key(name: str) -> str | None:
    env = os.environ.get(f"{name.upper()}_API_KEY", "").strip()
    if env:
        return env
    path = db.DATA_DIR / f"{name.lower()}_key.txt"
    try:
        key = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    return key or None
