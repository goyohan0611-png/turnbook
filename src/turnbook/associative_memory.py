"""Bounded sparse associative recall over Turnbook's evidence vault."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import hashlib
import json
import math
import re
import sqlite3
import unicodedata

from turnbook.packing import row_text
from turnbook.paths import use_wal

INDEX_VERSION = "engram-associative-layer-v0.6"
STOP_TERMS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "is", "it", "of", "on", "or", "that", "the", "this", "to", "was", "were",
    "with", "것", "그", "내", "나", "및", "수", "이", "저", "좀", "한", "할",
})


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _text(value, error):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(error)
    return value.strip()


def _terms(value):
    normalized = unicodedata.normalize("NFKC", value).casefold()
    found = re.findall(r"[^\W_]{2,}", normalized, re.UNICODE)
    return list(dict.fromkeys(term for term in found if term not in STOP_TERMS))


def _unit(value, error):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(error)
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise ValueError(error)
    return value


class AssociativeMemoryIndex:
    """Associative index; evidence remains owned by the source memory tables."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Claude, Codex and the capture service each open this file from their own process.
        # WAL lets them read while one writes; the timeout makes a writer wait its turn
        # instead of failing with "database is locked".
        self.db = sqlite3.connect(str(self.path), timeout=30)
        use_wal(self.db)
        self.db.row_factory = sqlite3.Row
        self.closed = False
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS associative_meta(
          key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS associative_engrams(
          revision INTEGER PRIMARY KEY AUTOINCREMENT,
          engram_id TEXT NOT NULL UNIQUE,
          source_kind TEXT NOT NULL,
          source_id TEXT NOT NULL,
          scope TEXT NOT NULL,
          happened_at TEXT NOT NULL,
          strength REAL NOT NULL,
          access_count INTEGER NOT NULL DEFAULT 0,
          last_activated_at TEXT,
          signature_json TEXT NOT NULL,
          fingerprint TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS associative_scope_revision
          ON associative_engrams(scope,revision DESC);
        CREATE TABLE IF NOT EXISTS associative_terms(
          term TEXT NOT NULL,
          engram_id TEXT NOT NULL,
          PRIMARY KEY(term,engram_id),
          FOREIGN KEY(engram_id) REFERENCES associative_engrams(engram_id));
        CREATE INDEX IF NOT EXISTS associative_term_engram
          ON associative_terms(engram_id,term);
        CREATE TABLE IF NOT EXISTS associative_edges(
          source_engram_id TEXT NOT NULL,
          target_engram_id TEXT NOT NULL,
          relation TEXT NOT NULL,
          weight REAL NOT NULL,
          evidence_count INTEGER NOT NULL,
          reinforced_at TEXT NOT NULL,
          PRIMARY KEY(source_engram_id,target_engram_id,relation),
          FOREIGN KEY(source_engram_id) REFERENCES associative_engrams(engram_id),
          FOREIGN KEY(target_engram_id) REFERENCES associative_engrams(engram_id));
        CREATE INDEX IF NOT EXISTS associative_edge_source
          ON associative_edges(source_engram_id,weight DESC);
        CREATE TABLE IF NOT EXISTS associative_pending_edges(
          source_engram_id TEXT NOT NULL,
          target_engram_id TEXT NOT NULL,
          relation TEXT NOT NULL,
          weight REAL NOT NULL,
          reinforced_at TEXT NOT NULL,
          PRIMARY KEY(source_engram_id,target_engram_id,relation));
        CREATE VIRTUAL TABLE IF NOT EXISTS associative_engrams_fts USING fts5(
          engram_id UNINDEXED, text, tokenize='unicode61');
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO associative_meta VALUES('version',?)",
                            (INDEX_VERSION,))
        version = self.db.execute(
            "SELECT value FROM associative_meta WHERE key='version'").fetchone()[0]
        if version != INDEX_VERSION:
            self.close()
            raise ValueError("ASSOCIATIVE_INDEX_VERSION_CHANGED")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self.db.close()

    def _link(self, source, target, relation, weight, when):
        if source == target:
            return
        self.db.execute("""INSERT INTO associative_edges(
          source_engram_id,target_engram_id,relation,weight,evidence_count,reinforced_at)
          VALUES(?,?,?,?,1,?)
          ON CONFLICT(source_engram_id,target_engram_id,relation) DO UPDATE SET
            weight=MAX(weight,excluded.weight),
            evidence_count=evidence_count+1,
            reinforced_at=excluded.reinforced_at""",
            (source, target, relation, weight, when))

    def _resolve_pending(self):
        rows = self.db.execute("""SELECT p.* FROM associative_pending_edges p
          JOIN associative_engrams source ON source.engram_id=p.source_engram_id
          JOIN associative_engrams target ON target.engram_id=p.target_engram_id""").fetchall()
        for row in rows:
            self._link(row["source_engram_id"], row["target_engram_id"],
                       row["relation"], row["weight"], row["reinforced_at"])
            self._link(row["target_engram_id"], row["source_engram_id"],
                       row["relation"], row["weight"], row["reinforced_at"])
            self.db.execute("""DELETE FROM associative_pending_edges
              WHERE source_engram_id=? AND target_engram_id=? AND relation=?""",
              (row["source_engram_id"], row["target_engram_id"], row["relation"]))

    def add_engram(self, *, engram_id, source_kind, source_id, scope, happened_at,
                   text, cues=None, strength=0.5, related_engram_ids=None,
                   max_automatic_links=12):
        normalized = {
            "engram_id": _text(engram_id, "ENGRAM_ID_REQUIRED"),
            "source_kind": _text(source_kind, "ENGRAM_SOURCE_KIND_REQUIRED").upper(),
            "source_id": _text(source_id, "ENGRAM_SOURCE_ID_REQUIRED"),
            "scope": _text(scope, "ENGRAM_SCOPE_REQUIRED"),
            "happened_at": _text(happened_at, "ENGRAM_TIME_REQUIRED"),
            "text": _text(text, "ENGRAM_TEXT_REQUIRED"),
            "strength": _unit(strength, "INVALID_ENGRAM_STRENGTH"),
        }
        cue_values = cues or []
        if not isinstance(cue_values, list) or not all(isinstance(x, str) for x in cue_values):
            raise ValueError("INVALID_ENGRAM_CUES")
        related = related_engram_ids or []
        if not isinstance(related, list) or not all(isinstance(x, str) and x.strip()
                                                    for x in related):
            raise ValueError("INVALID_RELATED_ENGRAM_IDS")
        if type(max_automatic_links) is not int or not 0 <= max_automatic_links <= 50:
            raise ValueError("INVALID_AUTOMATIC_LINK_LIMIT")
        signature = _terms(" ".join([normalized["text"], *cue_values]))
        if not signature:
            raise ValueError("ENGRAM_HAS_NO_SEARCHABLE_SIGNATURE")
        fingerprint_value = {**normalized, "signature": signature,
                             "related_engram_ids": sorted(set(related))}
        fingerprint = hashlib.sha256(
            _canonical(fingerprint_value).encode("utf-8")).hexdigest()
        existing = self.db.execute(
            "SELECT revision,fingerprint FROM associative_engrams WHERE engram_id=?",
            (normalized["engram_id"],)).fetchone()
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise ValueError("ENGRAM_ID_COLLISION")
            return {"engram_id": normalized["engram_id"],
                    "revision": existing["revision"], "disposition": "ALREADY_INDEXED"}

        placeholders = ",".join("?" for _ in signature)
        candidates = []
        if max_automatic_links:
            candidates = self.db.execute(f"""SELECT e.engram_id,COUNT(*) AS overlap
              FROM associative_terms t JOIN associative_engrams e
                ON e.engram_id=t.engram_id
              WHERE t.term IN ({placeholders}) AND e.scope=?
              GROUP BY e.engram_id ORDER BY overlap DESC,e.revision DESC LIMIT ?""",
              [*signature, normalized["scope"], max_automatic_links]).fetchall()
        previous = self.db.execute("""SELECT engram_id FROM associative_engrams
          WHERE scope=? ORDER BY happened_at DESC,revision DESC LIMIT 1""",
          (normalized["scope"],)).fetchone()
        now = datetime.now().isoformat(timespec="microseconds")
        with self.db:
            cursor = self.db.execute("""INSERT INTO associative_engrams(
              engram_id,source_kind,source_id,scope,happened_at,strength,signature_json,
              fingerprint) VALUES(?,?,?,?,?,?,?,?)""", (
                normalized["engram_id"], normalized["source_kind"],
                normalized["source_id"], normalized["scope"], normalized["happened_at"],
                normalized["strength"], _canonical(signature), fingerprint))
            self.db.execute("INSERT INTO associative_engrams_fts(engram_id,text) VALUES(?,?)",
                            (normalized["engram_id"], normalized["text"]))
            self.db.executemany("INSERT INTO associative_terms(term,engram_id) VALUES(?,?)",
                                [(term, normalized["engram_id"]) for term in signature])
            for candidate in candidates:
                weight = min(0.75, 0.2 + 0.1 * candidate["overlap"])
                self._link(normalized["engram_id"], candidate["engram_id"],
                           "SHARED_CUE", weight, now)
                self._link(candidate["engram_id"], normalized["engram_id"],
                           "SHARED_CUE", weight, now)
            if previous:
                self._link(normalized["engram_id"], previous["engram_id"],
                           "TEMPORAL_ADJACENT", 0.3, now)
                self._link(previous["engram_id"], normalized["engram_id"],
                           "TEMPORAL_ADJACENT", 0.3, now)
            for target in sorted(set(related)):
                if self.db.execute("SELECT 1 FROM associative_engrams WHERE engram_id=?",
                                   (target,)).fetchone():
                    self._link(normalized["engram_id"], target, "EXPLICIT", 1.0, now)
                    self._link(target, normalized["engram_id"], "EXPLICIT", 1.0, now)
                else:
                    self.db.execute("""INSERT OR IGNORE INTO associative_pending_edges(
                      source_engram_id,target_engram_id,relation,weight,reinforced_at)
                      VALUES(?,?,?,?,?)""", (normalized["engram_id"], target,
                                             "EXPLICIT", 1.0, now))
            self._resolve_pending()
        return {"engram_id": normalized["engram_id"], "revision": cursor.lastrowid,
                "disposition": "INDEXED", "automatic_links": len(candidates),
                "temporal_link": bool(previous)}

    def _table_exists(self, table):
        return bool(self.db.execute("SELECT 1 FROM sqlite_master WHERE name=?",
                                    (table,)).fetchone())

    def sync_sources(self, *, limit=None):
        """Incrementally index curated memories, conversations and development events."""
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 100000):
            raise ValueError("INVALID_ENGRAM_SYNC_LIMIT")
        remaining = limit
        counts = {"LOCAL_MEMORY": 0, "CONVERSATION": 0}

        def allowed():
            return remaining is None or sum(counts.values()) < remaining

        def left():
            # in SQL, not after fetchall(): an imported history fetched every unindexed row at once
            return -1 if remaining is None else max(0, remaining - sum(counts.values()))

        if self._table_exists("local_brain_memories"):
            rows = self.db.execute("""SELECT m.*,p.importance,p.related_memory_ids_json
              FROM local_brain_memories m LEFT JOIN local_brain_memory_profiles p
              ON p.memory_id=m.memory_id
              WHERE NOT EXISTS(SELECT 1 FROM associative_engrams a
                WHERE a.engram_id='memory:' || m.memory_id)
              ORDER BY m.revision LIMIT ?""", (left(),)).fetchall()
            for row in rows:
                if not allowed():
                    break
                related = json.loads(row["related_memory_ids_json"]) \
                    if row["related_memory_ids_json"] else []
                related = [self._source_to_engram(item) for item in related]
                result = self.add_engram(engram_id="memory:" + row["memory_id"],
                    source_kind="LOCAL_MEMORY", source_id=row["memory_id"],
                    scope=row["scope"], happened_at=row["effective_at"],
                    text=row["title"] + "\n" + row["content"],
                    cues=json.loads(row["tags_json"]) + json.loads(row["entities_json"])
                         + [row["memory_type"]],
                    strength=float(row["importance"] or 0.5),
                    related_engram_ids=related)
                if result["disposition"] == "INDEXED":
                    counts["LOCAL_MEMORY"] += 1
        if allowed() and self._table_exists("conversation_turns"):
            rows = self.db.execute("""SELECT t.* FROM conversation_turns t
              WHERE NOT EXISTS(SELECT 1 FROM associative_engrams a
                WHERE a.engram_id='conversation:' || t.turn_id)
              ORDER BY t.revision LIMIT ?""", (left(),)).fetchall()
            for row in rows:
                if not allowed():
                    break
                result = self.add_engram(engram_id="conversation:" + row["turn_id"],
                    source_kind="CONVERSATION", source_id=row["turn_id"],
                    scope=row["scope"], happened_at=row["happened_at"],
                    text=row_text(row),
                    cues=[row["client"], row["role"], row["client_session_id"]],
                    strength=0.4)
                if result["disposition"] == "INDEXED":
                    counts["CONVERSATION"] += 1
        return {"status": "ENGRAMS_INDEXED" if sum(counts.values()) else "NO_NEW_ENGRAMS",
                "indexed": counts, "indexed_total": sum(counts.values())}

    @staticmethod
    def _source_to_engram(source_id):
        if source_id.startswith("conversation-turn:"):
            return "conversation:" + source_id.removeprefix("conversation-turn:")
        if source_id.startswith(("memory:", "conversation:", "development:")):
            return source_id
        return "memory:" + source_id

    def activate(self, query, *, scopes=None, concept_cues=None, seed_limit=12, hops=2,
                 fanout=12, max_nodes=40, limit=8, minimum_concept_coverage=0.0,
                 coverage_mode="single"):
        query = _text(query, "ASSOCIATIVE_QUERY_REQUIRED")
        cue_list = concept_cues or []
        if not isinstance(cue_list, list) or not all(isinstance(x, str) and x.strip()
                                                     for x in cue_list):
            raise ValueError("INVALID_ASSOCIATIVE_CONCEPT_CUES")
        scope_list = scopes or []
        if not isinstance(scope_list, list) or not all(isinstance(x, str) and x.strip()
                                                       for x in scope_list):
            raise ValueError("INVALID_ASSOCIATIVE_SCOPES")
        bounds = ((seed_limit, 1, 50, "INVALID_SEED_LIMIT"),
                  (hops, 0, 4, "INVALID_ASSOCIATIVE_HOPS"),
                  (fanout, 1, 30, "INVALID_ASSOCIATIVE_FANOUT"),
                  (max_nodes, 1, 100, "INVALID_ACTIVE_NODE_LIMIT"),
                  (limit, 1, 30, "INVALID_ASSOCIATIVE_RESULT_LIMIT"))
        for value, lower, upper, error in bounds:
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(error)
        minimum_concept_coverage = _unit(
            minimum_concept_coverage, "INVALID_MINIMUM_CONCEPT_COVERAGE")
        if coverage_mode not in {"single", "union"}:
            raise ValueError("INVALID_COVERAGE_MODE")
        terms = _terms(" ".join([query, *cue_list]))
        concept_terms = set(_terms(" ".join(cue_list)))
        if not terms:
            return {"status": "UNKNOWN", "reason": "NO_SEARCHABLE_QUERY_TERMS",
                    "results": [], "visited_nodes": 0, "traversed_edges": 0}
        expression = " OR ".join('"' + term.replace('"', '""') + '"'
                                 for term in terms)
        where, parameters = ["associative_engrams_fts MATCH ?"], [expression]
        if scope_list:
            where.append("e.scope IN (" + ",".join("?" for _ in scope_list) + ")")
            parameters.extend(scope_list)
        parameters.append(seed_limit)
        seeds = self.db.execute("""SELECT e.*,associative_engrams_fts.text,
          bm25(associative_engrams_fts) AS rank
          FROM associative_engrams_fts JOIN associative_engrams e
          ON e.engram_id=associative_engrams_fts.engram_id WHERE """ + " AND ".join(where)
          + " ORDER BY rank,e.revision DESC LIMIT ?", parameters).fetchall()
        if not seeds:
            return {"status": "UNKNOWN", "reason": "NO_ACTIVATION_SEED",
                    "results": [], "visited_nodes": 0, "traversed_edges": 0}
        activations, parents, rows = {}, {}, {}
        query_set = set(terms)
        maximum_query_coverage = 0.0
        maximum_concept_coverage = 0.0
        covered_concept_terms = set()  # union of matched cue terms across all seeds
        for row in seeds:
            signature = set(json.loads(row["signature_json"]))
            overlap = len(query_set & signature)
            maximum_query_coverage = max(
                maximum_query_coverage, overlap / max(1, len(query_set)))
            if concept_terms:
                maximum_concept_coverage = max(maximum_concept_coverage,
                    len(concept_terms & signature) / len(concept_terms))
                covered_concept_terms |= (concept_terms & signature)
            lexical = overlap / math.sqrt(max(1, len(query_set) * len(signature)))
            activation = min(1.0, 0.55 + lexical)
            activations[row["engram_id"]] = activation
            parents[row["engram_id"]] = None
            rows[row["engram_id"]] = row
        union_concept_coverage = (len(covered_concept_terms) / len(concept_terms)
                                  if concept_terms else 0.0)
        gate_coverage = (union_concept_coverage if coverage_mode == "union"
                         else maximum_concept_coverage)
        if (concept_terms and gate_coverage < minimum_concept_coverage):
            return {"status": "UNKNOWN", "reason": "INSUFFICIENT_CONCEPT_COVERAGE",
                    "index_version": INDEX_VERSION, "seed_count": len(seeds),
                    "concept_cues_used": cue_list, "coverage_mode": coverage_mode,
                    "maximum_query_coverage": round(maximum_query_coverage, 6),
                    "maximum_concept_coverage": round(maximum_concept_coverage, 6),
                    "union_concept_coverage": round(union_concept_coverage, 6),
                    "minimum_concept_coverage": minimum_concept_coverage,
                    "visited_nodes": len(seeds), "traversed_edges": 0,
                    "results": []}
        frontier = set(activations)
        traversed = 0
        allowed_scopes = set(scope_list)
        for hop in range(1, hops + 1):
            contributions = {}
            sources = sorted(frontier, key=lambda item: activations[item], reverse=True)
            placeholders = ",".join("?" for _ in sources)
            edges = self.db.execute(f"""SELECT edge.source_engram_id,
                edge.target_engram_id,edge.relation,edge.weight,target.*
              FROM associative_edges edge JOIN associative_engrams target
                ON target.engram_id=edge.target_engram_id
              WHERE edge.source_engram_id IN ({placeholders})
              ORDER BY edge.source_engram_id,edge.weight DESC""", sources).fetchall()
            per_source = {}
            for edge in edges:
                source = edge["source_engram_id"]
                per_source[source] = per_source.get(source, 0) + 1
                if per_source[source] > fanout:
                    continue
                if allowed_scopes and edge["scope"] not in allowed_scopes:
                    continue
                traversed += 1
                contribution = activations[source] * edge["weight"] * (0.65 ** hop)
                old = contributions.get(edge["target_engram_id"], 0.0)
                combined = 1.0 - (1.0 - old) * (1.0 - contribution)
                contributions[edge["target_engram_id"]] = combined
                rows[edge["target_engram_id"]] = edge
                if (edge["target_engram_id"] not in parents
                        or contribution > activations.get(edge["target_engram_id"], 0.0)):
                    parents[edge["target_engram_id"]] = {
                        "from": source, "relation": edge["relation"],
                        "weight": edge["weight"]}
            for target, value in contributions.items():
                activations[target] = max(activations.get(target, 0.0), value)
            top = sorted(activations, key=activations.get, reverse=True)[:max_nodes]
            activations = {key: activations[key] for key in top}
            frontier = set(contributions) & set(top)
            if not frontier:
                break
        ranked = sorted(activations,
                        key=lambda key: activations[key] * (0.5 + rows[key]["strength"]),
                        reverse=True)[:limit]
        text_placeholders = ",".join("?" for _ in ranked)
        text_rows = self.db.execute(f"""SELECT engram_id,text
          FROM associative_engrams_fts WHERE engram_id IN ({text_placeholders})""",
          ranked).fetchall()
        excerpts = {row["engram_id"]: row["text"][:500] for row in text_rows}
        results = []
        for engram_id in ranked:
            row = rows[engram_id]
            path, cursor, seen = [], engram_id, set()
            while parents.get(cursor) and cursor not in seen:
                seen.add(cursor)
                step = parents[cursor]
                path.append({"from": step["from"], "to": cursor,
                             "relation": step["relation"], "weight": step["weight"]})
                cursor = step["from"]
            path.reverse()
            results.append({"engram_id": engram_id, "source_kind": row["source_kind"],
                "source_id": row["source_id"], "scope": row["scope"],
                "happened_at": row["happened_at"],
                "activation": round(activations[engram_id], 6),
                "strength": row["strength"],
                "text_excerpt": excerpts.get(engram_id, ""),
                "activation_path": path})
        return {"status": "KNOWN", "reason": "ASSOCIATIVE_ACTIVATION_COMPLETED",
                "index_version": INDEX_VERSION, "seed_count": len(seeds),
                "concept_cues_used": cue_list, "coverage_mode": coverage_mode,
                "maximum_query_coverage": round(maximum_query_coverage, 6),
                "maximum_concept_coverage": round(maximum_concept_coverage, 6),
                "union_concept_coverage": round(union_concept_coverage, 6),
                "minimum_concept_coverage": minimum_concept_coverage,
                "visited_nodes": len(activations), "traversed_edges": traversed,
                "bounds": {"hops": hops, "fanout": fanout, "max_nodes": max_nodes},
                "results": results}
