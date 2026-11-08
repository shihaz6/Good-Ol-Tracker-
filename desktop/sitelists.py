"""Site lists shared with the browser extension, read straight from its JS files so the
two never drift apart.

  extension/blocklist.js  sites whose content is never read (WhatsApp, banks, password
                          managers). Here: never sent to any AI service, not even by name.
  extension/adult.js      adult sites (focus-mode filter). Here: categorised as Entertainment.
"""

import re
from functools import lru_cache
from pathlib import Path

EXT_DIR = Path(__file__).resolve().parent.parent / "extension"


def _js_array(text: str, name: str) -> list:
    m = re.search(rf"const {name}\s*=\s*\[(.*?)\];", text, re.S)
    if not m:
        return []
    body = re.sub(r"//[^\n]*", "", m.group(1))
    return [s.lower() for s in re.findall(r"\"([^\"]+)\"", body)]


@lru_cache(maxsize=None)
def _lists():
    def read(name):
        try:
            return (EXT_DIR / name).read_text(encoding="utf-8")
        except OSError:
            return ""
    block, adult = read("blocklist.js"), read("adult.js")
    return (tuple(_js_array(block, "DEFAULT_BLOCKLIST")),
            tuple(_js_array(adult, "ADULT_DOMAINS")),
            tuple(_js_array(adult, "ADULT_WORDS")))


def _host(domain: str) -> str:
    return (domain or "").strip().lower().removeprefix("www.").rstrip(".")


def _in(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def never_send(domain: str) -> bool:
    """Same test as isBlocked() in blocklist.js: listed, or any part of the host contains "bank"."""
    host = _host(domain)
    if not host:
        return True
    if _in(host, _lists()[0]):
        return True
    return any("bank" in part for part in re.split(r"[.-]", host))


APP_WORDS = ("bank", "password", "keepass", "authenticator")


def never_send_app(exe: str) -> bool:
    """Desktop apps from the same companies as the never-read sites (Bitwarden.exe,
    a banking app, KeePass, ...): their window titles are never sent anywhere either."""
    name = (exe or "").lower()
    stems = {d.split(".")[0] for d in _lists()[0]}
    stems = {s for s in stems if len(s) >= 4 and s not in ("localhost",)}
    return any(w in name for w in (*APP_WORDS, *stems))


def is_adult(domain: str) -> bool:
    """Same test as isAdultHost() in adult.js."""
    host = _host(domain)
    if not host:
        return False
    _, domains, words = _lists()
    if _in(host, domains):
        return True
    return any(w in host for w in words) or bool(re.search(r"\.(xxx|porn|sex|adult)$", host))
