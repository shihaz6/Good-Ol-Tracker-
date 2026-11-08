"""AI categoriser: Voyage embeddings decide a page's category when the rules can't.

How it works
  1. Each category in categories.json has a few example descriptions ("descriptions").
     Those are embedded once and cached (prototype_vectors).
  2. Each scanned page (title, channel, description, headings, start of the text,
     max MAX_CHARS) is embedded once and cached (page_ai).
  3. A page gets the category whose descriptions are most similar, if the match is
     clear enough (MIN_SIM, MIN_MARGIN). Otherwise it stays undecided.

The rules still win: categorize.py only uses the AI category when site/app/channel
rules and keywords give no answer. Runs in the background (see tracker.py).
What leaves the PC: the page text described in (2), sent to api.voyageai.com.
"""

import hashlib
import json
import logging
import math
import time
import urllib.error
import urllib.request
from array import array

import keys

log = logging.getLogger("tracker")

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
MODELS = ["voyage-3.5-lite", "voyage-3-lite", "voyage-3.5"]  # first one that works is used
MAX_CHARS = 1500     # per page sent for embedding
BATCH = 32           # pages per API call
# Calibrated on voyage-3.5-lite (2026-11-08): correct matches scored 0.47-0.72 with a
# gap of >= 0.07 to the runner-up; vague pages (weather, vlogs) scored lower / closer.
MIN_SIM = 0.40       # best category must be at least this similar ...
MIN_MARGIN = 0.04    # ... and beat the runner-up by this much

status = {"last_run": None, "last_error": None, "model": None}  # shown on the dashboard


class AIError(Exception):
    pass


# --------------------------------------------------------------------- vectors

def pack(vec) -> bytes:
    return array("f", vec).tobytes()


def unpack(blob: bytes) -> list:
    a = array("f")
    a.frombytes(blob)
    return a.tolist()


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def page_text(page: dict) -> str:
    parts = [page.get("title"), page.get("author"), page.get("description"),
             (page.get("headings") or "").replace("\n", " · "), page.get("body")]
    return " \n".join(p.strip() for p in parts if p and p.strip())[:MAX_CHARS]


def classify(vec, prototypes: dict):
    """prototypes: {category: [vector, ...]} -> (category or None, best score)."""
    scores = sorted(
        ((max(cosine(vec, p) for p in vecs), cat) for cat, vecs in prototypes.items() if vecs),
        reverse=True,
    )
    if not scores:
        return None, 0.0
    best, cat = scores[0]
    runner_up = scores[1][0] if len(scores) > 1 else 0.0
    if best >= MIN_SIM and best - runner_up >= MIN_MARGIN:
        return cat, best
    return None, best


# --------------------------------------------------------------------- Voyage

def _post(key: str, model: str, texts: list) -> list:
    body = json.dumps({"input": texts, "model": model}).encode()
    req = urllib.request.Request(VOYAGE_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "User-Agent": keys.USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=60) as res:
        data = json.loads(res.read())
    return [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]


def embed(texts: list, key: str, model: str | None = None):
    """-> (model_used, vectors). Tries MODELS in order if the account lacks one."""
    for m in ([model] if model else MODELS):
        try:
            return m, _post(key, m, texts)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            if exc.code in (401, 403):
                raise AIError("Voyage rejected the API key (check data/voyage_key.txt)") from exc
            if exc.code == 429:
                raise AIError("Voyage rate limit reached; will retry later") from exc
            if exc.code == 400 and "model" in detail.lower() and not model:
                log.warning("voyage model %s unavailable, trying next: %s", m, detail)
                continue
            raise AIError(f"Voyage HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise AIError(f"Can't reach Voyage: {exc}") from exc
    raise AIError("None of the Voyage models are available to this key")


# --------------------------------------------------------------------- running

def rules_hash(rules, model: str) -> str:
    return sha(model + json.dumps(rules.descriptions, sort_keys=True))


def load_prototypes(conn, rules, key, model, embed_fn=embed):
    texts = [(cat, t) for cat, items in rules.descriptions.items() for t in items]
    cached = {h: v for h, v in conn.execute(
        "SELECT text_hash, vector FROM prototype_vectors WHERE model = ?", (model,))}
    missing = [t for _, t in texts if sha(t) not in cached]
    if missing:
        _, vecs = embed_fn(missing, key, model)
        rows = [(sha(t), model, pack(v)) for t, v in zip(missing, vecs)]
        conn.executemany("INSERT OR REPLACE INTO prototype_vectors VALUES (?, ?, ?)", rows)
        conn.commit()
        cached.update({h: b for h, _, b in rows})
    protos = {}
    for cat, t in texts:
        protos.setdefault(cat, []).append(unpack(cached[sha(t)]))
    return protos


def run_batch(conn, rules, limit: int = BATCH * 2, embed_fn=embed) -> dict:
    """Categorise pages that are new, changed, or were judged against old descriptions."""
    key = keys.load_key("voyage")
    if not key:
        return {"status": "no_key"}
    if not rules.descriptions:
        return {"status": "no_descriptions"}

    # Find the model once (first call), then stick to it.
    model = status["model"]
    if model is None:
        model, _ = embed_fn(["warm-up"], key, None)
        status["model"] = model
    rh = rules_hash(rules, model)
    protos = load_prototypes(conn, rules, key, model, embed_fn)

    rows = conn.execute(
        """SELECT p.url, p.title, p.author, p.description, p.headings, p.body, p.scanned_ts,
                  a.text_hash, a.vector, a.rules_hash, a.updated_ts, a.model
           FROM pages p LEFT JOIN page_ai a ON a.url = p.url
           WHERE a.url IS NULL OR a.rules_hash IS NOT ? OR a.model IS NOT ?
              OR (p.scanned_ts > a.updated_ts AND p.body IS NOT NULL)
           ORDER BY p.scanned_ts DESC LIMIT ?""",
        (rh, model, limit)).fetchall()

    now = time.time()
    to_embed, reclassify, out = [], [], []
    for url, title, author, desc, heads, body, scanned, thash, vec, _, _, amodel in rows:
        text = page_text({"title": title, "author": author, "description": desc,
                          "headings": heads, "body": body})
        if not text:  # nothing to judge; record it so it isn't retried forever
            out.append((url, model, sha(""), b"", None, 0.0, rh, now))
        elif vec and amodel == model and (thash == sha(text) or body is None):
            reclassify.append((url, thash, unpack(vec)))  # text unchanged (or purged): no API call
        else:
            to_embed.append((url, text))

    for url, thash, vec in reclassify:
        cat, score = classify(vec, protos)
        out.append((url, model, thash, pack(vec), cat, score, rh, now))
    for i in range(0, len(to_embed), BATCH):
        chunk = to_embed[i:i + BATCH]
        _, vecs = embed_fn([t for _, t in chunk], key, model)
        for (url, text), vec in zip(chunk, vecs):
            cat, score = classify(vec, protos)
            out.append((url, model, sha(text), pack(vec), cat, score, rh, now))
    conn.executemany("INSERT OR REPLACE INTO page_ai VALUES (?, ?, ?, ?, ?, ?, ?, ?)", out)
    conn.commit()
    return {"status": "ok", "embedded": len(to_embed), "reclassified": len(reclassify), "model": model}


def counts(conn) -> dict:
    done, decided = conn.execute(
        "SELECT COUNT(*), COUNT(category) FROM page_ai").fetchone()
    pending = conn.execute(
        "SELECT COUNT(*) FROM pages p LEFT JOIN page_ai a ON a.url = p.url WHERE a.url IS NULL").fetchone()[0]
    return {"pages_done": done, "pages_categorised": decided, "pages_pending": pending}
