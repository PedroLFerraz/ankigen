"""Dedup stage: drop generated cards that repeat something already studied.

Three tiers:
  0. same command — a command card whose command, as typed, is already the
     answer of another card, however differently its task is worded
  1. fuzzy — near-identical question wording (cheap, always on)
  2. semantic — embedding similarity, catches rephrasings

A card is compared against its deck's existing notes, cards kept by earlier
runs that haven't been imported into Anki yet, and cards already kept earlier
in this same run.

Embeddings are cached in the warehouse by content hash, so after the first run
only new or edited notes are embedded. Only decks the profile targets are ever
embedded — a 5,000-card language deck that isn't being generated for costs nothing.
"""
from __future__ import annotations

import difflib
import json
import logging
from datetime import date
from functools import lru_cache

import numpy as np

from ankigen import llm
from ankigen.config import ensure_free, settings
from ankigen.ingest import content_hash, strip_html
from ankigen.targeting import in_deck

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 0.85
# How far below the duplicate threshold a card is still worth flagging.
# Measured on four runs of real cards with nomic-embed-text: genuine
# rewordings score 0.90+, cards sharing only a topic sit at 0.65-0.85, and
# the interesting judgement calls cluster in the 0.10 below the threshold.
NEAR_DUP_MARGIN = 0.10
NEAR_DUP = "near-dup"


# How many nearest-by-meaning notes to also compare word by word.
FUZZY_SHORTLIST = 20


class EmbeddingsUnavailable(RuntimeError):
    """Semantic dedup was asked for and could not run."""


def _where(card: dict, match: dict) -> str:
    """The matched card's text, naming its deck when it is a different one.

    Worth saying: "you already have this in DS::SQL" is a different message
    from "you already have this here", and only one of them is surprising.
    """
    deck = match.get("deck") or ""
    elsewhere = f" in {deck}" if deck and deck != card.get("deck") else ""
    return f"{elsewhere}: {match['front'][:80]}"
BATCH = 64
_ARTICLES = ("the ", "a ", "an ", "la ", "le ", "el ", "der ", "die ", "das ")


def fuzzy_ratio(a: str, b: str) -> float:
    a, b = a.lower().strip(), b.lower().strip()
    for art in _ARTICLES:
        a = a.removeprefix(art)
        b = b.removeprefix(art)
    return difflib.SequenceMatcher(None, a, b).ratio()


def command_key(text: str) -> str:
    """A command as typed, for spotting the same one under another task.

    `curl -I https://example.com` came back on the networking day as "show
    only the status line and headers" after the Linux day had it as "show
    only the response headers": worded apart enough to pass as new by
    meaning, and the reverse card, which shows the command alone, twice.
    """
    return " ".join(strip_html(text or "").replace("`", " ").split())


def _command_of(card: dict) -> str:
    """The command a generated command card answers with; "" for other kinds."""
    if card.get("card_type") != "command":
        return ""
    fields = json.loads(card.get("fields_json") or "{}")
    return command_key(fields.get("Command", ""))


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


@lru_cache(maxsize=2)
def _fastembed(model: str):
    """The ONNX model, loaded once. First use downloads ~50MB and takes a few
    seconds; after that it embeds about a thousand texts a second."""
    try:
        from fastembed import TextEmbedding
    except ImportError as e:                      # pragma: no cover - env-specific
        raise RuntimeError(
            "EMBEDDING_PROVIDER=fastembed needs the fastembed package: pip install fastembed"
        ) from e
    return TextEmbedding(model)


class Embedder:
    """Embeds text through the configured provider, caching in the warehouse."""

    def __init__(self, wh):
        self.wh = wh
        self.cfg = settings.resolve_embedding()
        self.model = self.cfg["model"] or ""
        self.threshold = self.cfg.get("threshold") or 0.90
        self.available = bool(self.cfg["provider"])
        self.error: str | None = None if self.available else "no embedding provider configured"
        self.embedded = 0

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        ensure_free(self.cfg["provider"], self.model, settings.allow_paid_models)
        if self.cfg["provider"] == "fastembed":
            return [v.tolist() for v in _fastembed(self.model).embed(texts)]
        client = llm._get_openai_client(self.cfg["base_url"], self.cfg["api_key"])
        result = client.embeddings.create(model=self.model, input=texts)
        return [d.embedding for d in result.data]

    def vectors(self, items: dict[str, str]) -> dict[str, np.ndarray]:
        """{content_hash: text} -> {content_hash: vector}. Cache first, provider for the rest."""
        if not self.available or not items:
            return {}
        hashes = list(items)
        cached = {
            r["content_hash"]: np.asarray(r["vector"], dtype=np.float32)
            for r in self.wh.query(
                "SELECT content_hash, vector FROM embedding_cache "
                "WHERE model = ? AND content_hash IN (SELECT UNNEST(?))",
                [self.model, hashes],
            )
        }
        missing = [h for h in hashes if h not in cached]
        try:
            for i in range(0, len(missing), BATCH):
                chunk = missing[i:i + BATCH]
                vecs = self._embed_batch([items[h] for h in chunk])
                self.wh.insert(
                    "embedding_cache", ("content_hash", "model", "vector"),
                    [(h, self.model, list(map(float, v))) for h, v in zip(chunk, vecs)],
                    or_ignore=True,
                )
                cached.update({h: np.asarray(v, dtype=np.float32) for h, v in zip(chunk, vecs)})
                self.embedded += len(chunk)
        except Exception as e:
            # Fuzzy matching still runs; say so once rather than per card.
            self.available = False
            self.error = str(e)
            logger.warning("Embeddings unavailable, using fuzzy matching only: %s", e)
            return {}
        return cached


def _normalise(rows: list[np.ndarray]) -> np.ndarray:
    m = np.vstack(rows)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


def run(wh, run_date: date, use_embeddings: bool = True, refill: bool = False) -> dict:
    """Dedup the day's cards that passed verification.

    With `refill`, only the refill stage's cards, checked against today's
    kept cards as well, and written next to the day's other results.
    """
    candidates = wh.query(
        f"""SELECT g.card_uid, g.deck, g.front, g.back, g.card_type, g.fields_json
           FROM generated_cards g
           JOIN verified_cards v USING (run_date, card_uid)
           WHERE g.run_date = ? AND v.passed {"AND g.refill" if refill else ""}
           ORDER BY g.request_id, g.card_uid""",
        [run_date],
    )
    target_decks = sorted({c["deck"] for c in candidates})

    notes = wh.query("SELECT deck, front, back, content_hash FROM raw_notes WHERE run_date = ?", [run_date])
    prior = wh.query(
        "SELECT deck, front, back, card_type, fields_json FROM card_outcomes "
        "WHERE outcome = 'kept' AND run_date < ?",
        [run_date],
    )
    # One pool for the whole collection, not one per deck. A fact you already
    # have a card for in DS::SQL is not new knowledge because this run asked
    # for it under Data Platform — you would simply be shown it twice.
    everything = [dict(n) for n in notes]
    if refill:
        prior += wh.query(
            "SELECT deck, front, back, card_type, fields_json FROM card_outcomes "
            "WHERE outcome = 'kept' AND run_date = ? AND NOT refill",
            [run_date],
        )
    everything += [{**p, "content_hash": content_hash(p["front"], p["back"])} for p in prior]

    # Every answer that is a command, by the command: a note's answer as the
    # collection has it, a generated card's from its Command field (its `back`
    # carries the note too). Across the whole collection, like the rest.
    commands: dict[str, dict] = {}
    for n in everything:
        key = _command_of(n) or command_key(n["back"])
        if key:
            commands.setdefault(key, n)

    # Fuzzy-only runs stay inside the deck: difflib against a whole collection
    # is thousands of comparisons per card, and without embeddings there is no
    # cheap way to shortlist what is worth comparing.
    existing_by_deck = {
        deck: [n for n in everything if in_deck(n["deck"], deck)] for deck in target_decks
    }

    embedder = Embedder(wh) if use_embeddings else None
    threshold = embedder.threshold if embedder else 0.90
    vectors: dict[str, np.ndarray] = {}
    if embedder and embedder.available:
        texts = {n["content_hash"]: f"{n['front']} {n['back']}" for n in everything}
        texts.update({content_hash(c["front"], c["back"]): f"{c['front']} {c['back']}" for c in candidates})
        vectors = embedder.vectors(texts)

    if embedder and not embedder.available:
        # Checked after embedding, not before: the provider fails mid-pass, as
        # when a per-minute embedding quota ran out 64 texts in. Fuzzy matching
        # alone catches rewordings of one sentence and nothing else, so falling
        # back to it quietly reports "no duplicates" and overwrites good
        # results with that claim. That happened, and it looked like success.
        raise EmbeddingsUnavailable(
            f"Embeddings are configured but unavailable ({embedder.error}). "
            "Fix the provider, or pass use_embeddings=False to accept "
            "fuzzy-only matching for this run."
        )

    rows = []
    # Semantic search runs over the whole collection at once, so the pool is
    # normalised once here rather than rebuilt for every card.
    searchable = [n for n in everything if n["content_hash"] in vectors] if vectors else []
    matrix = _normalise([vectors[n["content_hash"]] for n in searchable]) if searchable else None

    for deck in target_decks:
        fuzzy_pool = list(existing_by_deck[deck])
        for card in (c for c in candidates if c["deck"] == deck):
            is_dup, reason, best = False, "", 0.0
            card_vec = vectors.get(content_hash(card["front"], card["back"]))
            command = _command_of(card)

            if command and command in commands:
                is_dup, best = True, 1.0
                reason = f"same command{_where(card, commands[command])}"
            elif matrix is not None and card_vec is not None and card_vec.shape[0] == matrix.shape[1]:
                sims = matrix @ (card_vec / (np.linalg.norm(card_vec) or 1.0))
                # difflib over a whole collection is far too slow, so the
                # nearest few by meaning are the only ones worth comparing
                # word by word.
                shortlist = np.argsort(sims)[::-1][:FUZZY_SHORTLIST]
                for idx in shortlist:
                    n = searchable[int(idx)]
                    ratio = fuzzy_ratio(card["front"], n["front"])
                    if ratio >= FUZZY_THRESHOLD:
                        is_dup, reason, best = True, f"fuzzy {ratio:.2f}{_where(card, n)}", ratio
                        break
                if not is_dup:
                    j = int(shortlist[0])
                    best = float(sims[j])
                    if best >= threshold:
                        is_dup = True
                        reason = f"semantic {best:.2f}{_where(card, searchable[j])}"
                    elif best >= threshold - NEAR_DUP_MARGIN:
                        # Measured on real pairs, this band holds both genuine
                        # rewordings and cards that merely share a topic — the
                        # control plane's components score 0.62 against the
                        # node's. Dropping them silently loses good cards, so
                        # they ship with a tag and you decide in one click.
                        reason = f"near-dup {best:.2f}{_where(card, searchable[j])}"
            else:
                for n in fuzzy_pool:
                    ratio = fuzzy_ratio(card["front"], n["front"])
                    if ratio >= FUZZY_THRESHOLD:
                        is_dup, reason, best = True, f"fuzzy {ratio:.2f}{_where(card, n)}", ratio
                        break

            rows.append((run_date, card["card_uid"], is_dup, reason or None, best or None))
            if not is_dup:
                # Later cards in this run must not duplicate this one either.
                twin = {"deck": card["deck"], "front": card["front"], "back": card["back"],
                        "content_hash": content_hash(card["front"], card["back"])}
                fuzzy_pool.append(twin)
                if command:
                    commands.setdefault(command, twin)
                if matrix is not None and card_vec is not None and card_vec.shape[0] == matrix.shape[1]:
                    searchable.append(twin)
                    matrix = np.vstack([matrix, card_vec / (np.linalg.norm(card_vec) or 1.0)])

    columns = ("run_date", "card_uid", "is_dup", "reason", "similarity")
    if refill:
        with wh.transaction() as con:
            con.execute(
                "DELETE FROM dedup_results WHERE run_date = ? AND card_uid IN "
                "(SELECT card_uid FROM generated_cards WHERE run_date = ? AND refill)",
                [run_date, run_date],
            )
            wh.insert("dedup_results", columns, rows, con=con)
    else:
        wh.replace_partition("dedup_results", run_date, columns, rows)
    dups = sum(1 for r in rows if r[2])
    return {
        "checked": len(rows),
        "duplicates": dups,
        "kept": len(rows) - dups,
        "near_dups": sum(1 for r in rows if not r[2] and (r[3] or "").startswith(NEAR_DUP)),
        "semantic": bool(embedder and embedder.available),
        "threshold": threshold,
        "embedded_now": embedder.embedded if embedder else 0,
        "embedding_error": embedder.error if embedder else "disabled",
    }
