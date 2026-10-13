"""Ranked search over operations (and any other endpoint-like records).

A small in-memory search engine, so it works the same on PostgreSQL and SQLite and can be tested offline:

* **Tokenizing** splits ``camelCase``, ``snake_case``, paths and punctuation, drops stop words and applies a
  light stemmer (``tickets`` / ``ticketing`` → ``ticket``).
* **BM25 per field** with weights: name and title count most, then path and tags, then parameter names, then
  the description.
* **Expansion**: synonyms (remove → delete, client → company, machine → device …), prefixes (``tick`` →
  ``ticket``) and typos (``tikcet`` → ``ticket``, via rapidfuzz) — each worth less than an exact hit.
* **Intent**: a read verb ("list", "show") lifts read-only operations; a write verb ("create", "remove") lifts
  writes.
* Results that cover all query words beat ones that match a single word many times; pinned operations get a
  small lift and deprecated ones are pushed down.

The operations index is cached per process and rebuilt when any connection or operation changes.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz, process

FIELD_WEIGHTS: dict[str, float] = {"name": 3.0, "title": 2.5, "target": 2.0, "tags": 1.8, "params": 1.3,
                                   "description": 1.0, "connection": 1.2}
_K1, _B = 1.2, 0.75
_STOP = {"a", "an", "the", "of", "for", "to", "by", "in", "on", "and", "or", "with", "from", "all", "api", "is",
         "be", "this", "that", "my", "me", "i", "it", "its", "at", "as", "via", "using", "v1", "v2", "v3", "v4"}
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SPLIT = re.compile(r"[^0-9a-zA-Z]+")

READ_VERBS = {"get", "list", "read", "fetch", "show", "find", "search", "query", "retrieve", "view", "lookup",
              "count", "describe", "export", "download", "check"}
WRITE_VERBS = {"create", "add", "new", "insert", "post", "make", "register", "update", "edit", "modify", "change",
               "patch", "put", "set", "rename", "assign", "move", "delete", "remove", "destroy", "drop", "purge",
               "deactivate", "disable", "enable", "close", "resolve", "reset", "run", "execute", "send", "upload",
               "merge", "approve", "cancel"}
_SYNONYM_GROUPS = [
    {"get", "list", "read", "fetch", "show", "find", "search", "query", "retrieve", "view", "lookup"},
    {"create", "add", "new", "insert", "post", "make", "register"},
    {"update", "edit", "modify", "change", "patch", "put", "set"},
    {"delete", "remove", "destroy", "drop", "purge"},
    {"ticket", "incident", "case", "issue"},  # not "request": too many APIs have X-Request-Id etc.
    {"device", "computer", "endpoint", "machine", "agent", "asset", "host", "workstation", "configuration"},
    {"company", "client", "customer", "account", "organization", "org", "tenant", "site"},
    {"user", "person", "contact", "member", "people", "employee"},
    {"invoice", "bill", "billing"},
    {"alert", "alarm", "threat", "detection"},
    {"agreement", "contract", "subscription"},
    {"note", "comment"},
    {"time", "timesheet", "entry"},
    {"script", "automation", "job", "task"},
]
SYNONYMS: dict[str, set[str]] = defaultdict(set)
for _group in _SYNONYM_GROUPS:
    for _w in _group:
        SYNONYMS[_w] |= _group - {_w}


def stem(word: str) -> str:
    if len(word) <= 3 or word.isdigit():
        return word
    for suffix, repl in (("ies", "y"), ("sses", "ss"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            base = word[: -len(suffix)] + repl
            if suffix == "es" and not base.endswith(("ch", "sh", "x", "z", "ss")):
                base = word[:-1]  # "notes" → "note", "boxes" → "box"
            if suffix == "s" and word.endswith("ss"):
                return word
            return base
    return word


def tokenize(text: Any) -> list[str]:
    if text is None:
        return []
    if isinstance(text, (list, tuple, set)):
        text = " ".join(map(str, text))
    out = []
    for part in _SPLIT.split(_CAMEL.sub(" ", str(text))):
        w = part.lower()
        if w and w not in _STOP and len(w) > 1:
            out.append(stem(w))
    return out


@dataclass
class _Doc:
    record: dict[str, Any]
    fields: dict[str, list[str]]
    lengths: dict[str, int] = field(default_factory=dict)


class Index:
    """BM25 over named fields. ``records`` are dicts; ``fields`` maps field name → record key(s) to read."""

    def __init__(self, records: list[dict[str, Any]], fields: dict[str, list[str]] | None = None,
                 weights: dict[str, float] | None = None) -> None:
        self.weights = weights or FIELD_WEIGHTS
        fields = fields or {f: [f] for f in self.weights}
        self.docs: list[_Doc] = []
        self.postings: dict[str, dict[int, dict[str, int]]] = defaultdict(lambda: defaultdict(dict))
        totals: dict[str, int] = defaultdict(int)
        for i, rec in enumerate(records):
            toks = {f: [t for key in keys for t in tokenize(rec.get(key))] for f, keys in fields.items()}
            doc = _Doc(rec, toks, {f: len(v) for f, v in toks.items()})
            self.docs.append(doc)
            for f, words in toks.items():
                totals[f] += len(words)
                for w in words:
                    self.postings[w][i][f] = self.postings[w][i].get(f, 0) + 1
        n = max(1, len(self.docs))
        self.avg = {f: max(1.0, totals[f] / n) for f in fields}
        self.vocab = list(self.postings.keys())
        self._by_prefix: dict[str, list[str]] = defaultdict(list)
        for w in self.vocab:
            if len(w) >= 3:
                self._by_prefix[w[:3]].append(w)

    def __len__(self) -> int:
        return len(self.docs)

    def _idf(self, term: str) -> float:
        df = len(self.postings.get(term, {}))
        return math.log(1 + (len(self.docs) - df + 0.5) / (df + 0.5)) if df else 0.0

    def expand(self, term: str) -> dict[str, float]:
        """term → {index term: weight} — exact 1.0, synonym 0.6, prefix 0.7, typo ≤ 0.5."""
        out: dict[str, float] = {}
        if term in self.postings:
            out[term] = 1.0
        for syn in SYNONYMS.get(term, ()):
            s = stem(syn)
            if s in self.postings:
                out.setdefault(s, 0.6)
        if len(term) >= 3:
            for w in self._by_prefix.get(term[:3], []):
                if w != term and w.startswith(term):
                    out.setdefault(w, 0.7)
        if not out and len(term) >= 4 and self.vocab:
            for w, score, _ in process.extract(term, self.vocab, scorer=fuzz.ratio, limit=3, score_cutoff=80):
                out.setdefault(w, 0.5 * score / 100)
        return out

    def search(self, query: str, limit: int = 20, predicate: Any = None) -> list[tuple[float, dict[str, Any], list[str]]]:
        terms = list(dict.fromkeys(tokenize(query)))
        if not terms:
            return []
        raw_words = {w.lower() for w in _SPLIT.split(query) if w}
        wants_read = bool(raw_words & READ_VERBS) and not raw_words & WRITE_VERBS
        wants_write = bool(raw_words & WRITE_VERBS)
        scores: dict[int, float] = defaultdict(float)
        matched: dict[int, set[str]] = defaultdict(set)
        for term in terms:
            for index_term, weight in self.expand(term).items():
                idf = self._idf(index_term)
                for doc_id, per_field in self.postings[index_term].items():
                    doc = self.docs[doc_id]
                    s = 0.0
                    for f, tf in per_field.items():
                        norm = 1 - _B + _B * doc.lengths.get(f, 0) / self.avg.get(f, 1)
                        s += self.weights.get(f, 1.0) * tf * (_K1 + 1) / (tf + _K1 * norm)
                    scores[doc_id] += idf * s * weight
                    matched[doc_id].add(term)
        normalized_query = "_".join(terms)
        results = []
        for doc_id, score in scores.items():
            rec = self.docs[doc_id].record
            if predicate and not predicate(rec):
                continue
            coverage = len(matched[doc_id]) / len(terms)
            score *= 0.4 + 0.6 * coverage ** 2
            if "_".join(tokenize(rec.get("name"))) == normalized_query:
                score *= 1.6
            if rec.get("read_only") is not None:
                if wants_read and rec["read_only"]:
                    score *= 1.25
                elif wants_write and not rec["read_only"]:
                    score *= 1.25
                elif wants_read and not rec["read_only"]:
                    score *= 0.8
            if rec.get("pinned"):
                score *= 1.1
            if rec.get("deprecated"):
                score *= 0.7
            results.append((round(score, 4), rec, sorted(matched[doc_id])))
        results.sort(key=lambda r: (-r[0], str(r[1].get("name", ""))))
        return results[: max(1, limit)]


# ── Operations index (all connections) ────────────────────────────────────

_cache: dict[str, Any] = {"key": None, "index": None}
_lock = asyncio.Lock()


def _param_names(schema: Any) -> list[str]:
    props = (schema or {}).get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return []
    names = list(props.keys())
    body = props.get("body")
    if isinstance(body, dict) and isinstance(body.get("properties"), dict):
        names += list(body["properties"].keys())[:40]
    return names[:80]


async def _version_key() -> tuple[Any, ...]:
    from sqlalchemy import func, select

    from app.db.repository import get_session_factory
    from app.db.schema import Connection, Operation

    async with get_session_factory()() as db:
        conns = (await db.execute(select(func.count(), func.max(Connection.updated_at),
                                         func.max(Connection.imported_at)).select_from(Connection))).one()
        ops = (await db.execute(select(func.count()).select_from(Operation))).scalar()
    return (*conns, ops)


async def operations_index() -> Index:
    key = await _version_key()
    if _cache["key"] == key and _cache["index"] is not None:
        return _cache["index"]
    async with _lock:
        if _cache["key"] == key and _cache["index"] is not None:
            return _cache["index"]
        from sqlalchemy import select

        from app.db.repository import get_session_factory
        from app.db.schema import Connection, Operation

        async with get_session_factory()() as db:
            conns = {c.id: c for c in (await db.execute(select(Connection))).scalars()}
            rows = (await db.execute(select(
                Operation.connection_id, Operation.name, Operation.title, Operation.method, Operation.target,
                Operation.tags, Operation.description, Operation.input_schema, Operation.read_only, Operation.pinned,
                Operation.deprecated, Operation.enabled))).all()
        records = []
        for r in rows:
            c = conns.get(r.connection_id)
            if c is None:
                continue
            records.append({
                "connection": c.slug, "connection_name": c.name, "kind": c.kind, "connection_enabled": c.enabled,
                "name": r.name, "title": r.title, "method": r.method, "target": r.target, "tags": r.tags or [],
                "description": (r.description or "")[:1500], "params": _param_names(r.input_schema),
                "read_only": r.read_only, "pinned": r.pinned, "deprecated": r.deprecated, "enabled": r.enabled,
            })
        index = Index(records, fields={"name": ["name"], "title": ["title"], "target": ["target"], "tags": ["tags"],
                                       "params": ["params"], "description": ["description"],
                                       "connection": ["connection_name"]})
        _cache.update(key=key, index=index)
        return index


def invalidate() -> None:
    _cache.update(key=None, index=None)


async def search_operations(q: str, *, connection: str | None = None, kind: str | None = None,
                            read_only: bool | None = None, pinned: bool | None = None,
                            include_disabled: bool = False, limit: int = 20,
                            allowed: Any = None) -> list[dict[str, Any]]:
    """Ranked operations across all connections. ``allowed(connection_slug) -> bool`` hides what the caller
    may not use."""
    index = await operations_index()
    connections = {c.strip() for c in (connection or "").split(",") if c.strip()}

    def keep(rec: dict[str, Any]) -> bool:
        if connections and rec["connection"] not in connections:
            return False
        if kind and rec["kind"] != kind:
            return False
        if read_only is not None and rec["read_only"] != read_only:
            return False
        if pinned is not None and rec["pinned"] != pinned:
            return False
        if not include_disabled and not (rec["enabled"] and rec["connection_enabled"]):
            return False
        return not (allowed and not allowed(rec["connection"]))

    hits = index.search(q, limit=limit, predicate=keep)
    return [{"score": score, "matched": words,
             **{k: rec[k] for k in ("connection", "connection_name", "kind", "name", "title", "method", "target",
                                    "read_only", "pinned", "deprecated", "enabled", "tags")},
             "description": rec["description"][:300]} for score, rec, words in hits]
