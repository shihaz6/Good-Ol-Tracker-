"""Change categories.json from the dashboard.

Edits are made in place in the file's text (your layout, groups and _help stay as they
are), then the result is checked by loading it exactly as the categoriser would. Only a
valid file is ever written. Every change can be undone from the dashboard; undo refuses
if you edited the file by hand since, so it never throws away your own edits.
"""

import hashlib
import json
import os
import re
import threading
import time

import categorize
import db

SECTIONS = ("apps", "sites", "channels", "pages")
MAX_HISTORY = 30
MAX_CHANGES = 25
_lock = threading.Lock()


class RuleError(ValueError):
    pass


def _history_path():
    return db.DATA_DIR / "rules_history.json"


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ validation

_DOMAIN = re.compile(r"^(?=.{1,253}$)[a-z0-9-]+(\.[a-z0-9-]+)*$")


def normalize_key(section: str, key) -> str:
    if not isinstance(key, str) or not key.strip():
        raise RuleError("name is empty")
    key = key.strip()
    if len(key) > 2000 or any(ord(c) < 32 for c in key):
        raise RuleError("name is too long or has odd characters")
    if section == "sites":
        key = key.lower()
        key = re.sub(r"^[a-z]+://", "", key).split("/")[0].split(":")[0]
        if key.startswith("www."):
            key = key[4:]
        if not _DOMAIN.match(key):
            raise RuleError(f"not a site name: {key!r} (use e.g. example.com)")
    elif section == "pages":
        if not key.startswith(("http://", "https://")):
            raise RuleError("page rules need a full http(s) URL")
        key = db.canonical_url(key)
    elif len(key) > 200:
        raise RuleError("name is too long")
    return key


def _check_category(section: str, category, rules) -> str:
    if section == "sites" and category == categorize.CONTENT:
        return category
    if category is None:
        return None  # remove the rule
    if category not in rules.categories:
        raise RuleError(f"unknown category: {category!r}")
    return category


# ------------------------------------------------------------------ text editing

def _find_block(text: str, section: str):
    """(index of '{', index of matching '}') of a top-level "section": {...}, or None."""
    m = re.search(r'"%s"\s*:\s*\{' % re.escape(section), text)
    if not m:
        return None
    in_str = esc = False
    for i in range(m.end(), len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            return None  # nested objects: not one of ours
        elif ch == "}":
            return m.end() - 1, i
    return None


def _entry_regex(key: str):
    # "key": "value" with an optional trailing comma
    return re.compile(re.escape(json.dumps(key, ensure_ascii=False)) + r'\s*:\s*"(?:[^"\\]|\\.)*"')


def _edit_text(text: str, section: str, key: str, category, existing_key):
    """Return new text, or None if the targeted edit isn't possible (caller falls back)."""
    block = _find_block(text, section)
    if block is None:
        return None
    open_i, close_i = block
    inner = text[open_i + 1:close_i]
    entry = f"{json.dumps(key, ensure_ascii=False)}: {json.dumps(category, ensure_ascii=False)}"

    if existing_key is not None:
        rx = _entry_regex(existing_key)
        found = list(rx.finditer(inner))
        if len(found) != 1:
            return None
        m = found[0]
        if category is not None:
            inner = inner[:m.start()] + entry + inner[m.end():]
        else:  # remove the entry's line; if it was the last entry, drop the comma before it
            line_start = inner.rfind("\n", 0, m.start()) + 1
            nl = inner.find("\n", m.end())
            if nl < 0:
                return None
            line = inner[line_start:nl].strip()
            if line not in (m.group(0), m.group(0) + ","):
                return None  # shares its line with other entries: let the fallback handle it
            before, after = inner[:line_start], inner[nl + 1:]
            if not line.endswith(","):
                before = re.sub(r",(\s*)$", r"\1", before)
            inner = before + after
        return text[:open_i + 1] + inner + text[close_i:]

    if category is None:
        return text
    stripped = inner.rstrip()
    if not stripped.strip():
        indent = _indent_of(text, open_i) + "  "
        inner = f"\n{indent}{entry}\n{_indent_of(text, open_i)}"
    else:
        last_line = stripped.splitlines()[-1]
        indent = re.match(r"[ \t]*", last_line).group(0) or "    "
        inner = stripped + f",\n{indent}{entry}" + inner[len(stripped):]
    return text[:open_i + 1] + inner + text[close_i:]


def _indent_of(text: str, index: int) -> str:
    line_start = text.rfind("\n", 0, index) + 1
    return re.match(r"[ \t]*", text[line_start:]).group(0)


def _apply(text: str, section: str, key: str, category) -> str:
    raw = json.loads(text)
    current = raw.get(section) or {}
    if not isinstance(current, dict):
        raise RuleError(f'"{section}" in categories.json is not an object')
    # The categoriser matches apps/sites/channels case-insensitively: reuse an existing key.
    if section == "pages":
        existing = key if key in current else None
    else:
        existing = next((k for k in current if k.lower() == key.lower()), None)
    if section in raw:
        new = _edit_text(text, section, existing or key, category, existing)
    elif category is None:
        return text
    else:  # add the section at the end of the file
        end = text.rstrip().rfind("}")
        entry = f"{json.dumps(key, ensure_ascii=False)}: {json.dumps(category, ensure_ascii=False)}"
        new = text[:end].rstrip() + f',\n\n  "{section}": {{\n    {entry}\n  }}\n' + text[end:]
    expected = {k: v for k, v in current.items() if k != existing}
    if category is not None:
        expected[existing or key] = category
    if new is not None:
        try:
            if json.loads(new).get(section) == expected:
                return new
        except json.JSONDecodeError:
            pass
    # Fallback: rewrite the whole file (keeps all content, loses custom spacing).
    raw[section] = expected
    return json.dumps(raw, indent=2, ensure_ascii=False) + "\n"


def _describe(section, key, category):
    what = {"apps": "App", "sites": "Site", "channels": "Channel", "pages": "Page"}[section]
    target = "by page content" if category == categorize.CONTENT else category
    return f"{what} {key} → {target}" if category is not None else f"{what} {key}: rule removed"


# ------------------------------------------------------------------ public API

def _load_history():
    try:
        data = json.loads(_history_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_history(items):
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _history_path().write_text(json.dumps(items[-MAX_HISTORY:], ensure_ascii=False), encoding="utf-8")


def _read(path) -> str:
    with open(path, encoding="utf-8", newline="") as f:  # newline="": keep \r\n or \n exactly as is
        return f.read()


def _write_validated(path, text: str):
    """Write only if the categoriser accepts the new text. Atomic replace. Byte-exact."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    try:
        categorize.Rules(tmp)
    except (ValueError, KeyError, TypeError) as exc:
        tmp.unlink(missing_ok=True)
        raise RuleError(f"that change would break categories.json: {exc}") from exc
    os.replace(tmp, path)


def set_rules(changes, path=None) -> dict:
    """changes: [{"section", "key", "category"}] (category None removes the rule).
    Applied together as one undoable change."""
    path = path or categorize.RULES_PATH
    if not isinstance(changes, list) or not changes:
        raise RuleError("no changes given")
    if len(changes) > MAX_CHANGES:
        raise RuleError("too many changes at once")
    with _lock:
        before = _read(path)
        nl = "\r\n" if "\r\n" in before else "\n"
        rules = categorize.Rules(path)
        text, done = before.replace("\r\n", "\n"), []
        for ch in changes:
            if not isinstance(ch, dict):
                raise RuleError("bad change")
            section = ch.get("section")
            if section not in SECTIONS:
                raise RuleError(f"unknown rule type: {section!r}")
            key = normalize_key(section, ch.get("key"))
            category = _check_category(section, ch.get("category"), rules)
            text = _apply(text, section, key, category)
            done.append(_describe(section, key, category))
        text = text.replace("\n", nl)  # same line endings as the file had
        if text == before:
            return {"ok": True, "changed": [], "message": "Nothing to change"}
        _write_validated(path, text)
        history = _load_history()
        history.append({"ts": time.time(), "changes": done, "before": before, "after_sha": _sha(text)})
        _save_history(history)
    return {"ok": True, "changed": done}


def undo(path=None) -> dict:
    path = path or categorize.RULES_PATH
    with _lock:
        history = _load_history()
        if not history:
            raise RuleError("nothing to undo")
        last = history[-1]
        if _sha(_read(path)) != last.get("after_sha"):
            raise RuleError("categories.json was edited by hand since that change; undo it in the file instead")
        _write_validated(path, last["before"])
        history.pop()
        _save_history(history)
    return {"ok": True, "undone": last["changes"]}


def recent(limit: int = 10) -> list:
    return [{"ts": h["ts"], "changes": h["changes"]} for h in reversed(_load_history()[-limit:])]
