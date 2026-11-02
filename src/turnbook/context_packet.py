"""Shared contract and bounded context builder for the personal Local Brain."""
from __future__ import annotations

from math import ceil
import json
import re

SCHEMA_VERSION = "universal-personal-memory-v0.5"

MEMORY_CLASSES = frozenset({
    "EPISODE", "FACT", "PREFERENCE", "DECISION", "TASK", "ARTIFACT",
    "RELATION", "STATE", "SUMMARY",
})
MODALITIES = frozenset({
    "CONVERSATION", "DOCUMENT", "CODE", "FILE", "WEB", "MANUAL", "SYSTEM",
    "OTHER",
})
RETENTION_CLASSES = frozenset({"EPISODIC", "DURABLE", "PINNED"})
PROFILE_FIELDS = frozenset({
    "memory_class", "modality", "retention", "importance", "confidence",
    "attributes", "related_memory_ids",
})


def _choice(value, choices, default, error):
    selected = default if value is None else value
    if not isinstance(selected, str) or selected.upper() not in choices:
        raise ValueError(error)
    return selected.upper()


def _probability(value, default, error):
    selected = default if value is None else value
    if isinstance(selected, bool) or not isinstance(selected, (int, float)):
        raise ValueError(error)
    selected = float(selected)
    if not 0.0 <= selected <= 1.0:
        raise ValueError(error)
    return selected


def _json_object(value, error):
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(error)
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(error) from exc
    return value


def _string_list(value, error):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip()
                                              for item in value):
        raise ValueError(error)
    return list(dict.fromkeys(item.strip() for item in value))


def infer_memory_class(memory_type):
    kind = str(memory_type or "").upper()
    for token, memory_class in (
        ("PREFERENCE", "PREFERENCE"), ("DECISION", "DECISION"),
        ("TASK", "TASK"), ("ACTION", "TASK"), ("FILE", "ARTIFACT"),
        ("DOCUMENT", "ARTIFACT"), ("CODE", "ARTIFACT"),
        ("RELATION", "RELATION"), ("STATE", "STATE"),
        ("SUMMARY", "SUMMARY"), ("FACT", "FACT"),
        ("PROJECT", "STATE"),
    ):
        if token in kind:
            return memory_class
    return "EPISODE"


def infer_modality(provenance):
    source = str((provenance or {}).get("source_type", "")).casefold()
    for token, modality in (
        ("conversation", "CONVERSATION"), ("codex", "CONVERSATION"),
        ("document", "DOCUMENT"), ("code", "CODE"), ("git", "CODE"),
        ("file", "FILE"), ("web", "WEB"), ("browser", "WEB"),
        ("manual", "MANUAL"), ("mcp", "MANUAL"),
        ("system", "SYSTEM"),
    ):
        if token in source:
            return modality
    return "OTHER"


def default_profile(memory_type, provenance=None):
    memory_class = infer_memory_class(memory_type)
    durable = memory_class in {"FACT", "PREFERENCE", "DECISION", "STATE", "SUMMARY"}
    return {
        "schema_version": SCHEMA_VERSION,
        "memory_class": memory_class,
        "modality": infer_modality(provenance),
        "retention": "DURABLE" if durable else "EPISODIC",
        "importance": 0.7 if durable else 0.5,
        "confidence": 1.0,
        "attributes": {},
        "related_memory_ids": [],
    }


def normalize_profile(memory):
    """Validate optional universal fields while supplying deterministic defaults."""
    defaults = default_profile(memory.get("memory_type"), memory.get("provenance"))
    return {
        "schema_version": SCHEMA_VERSION,
        "memory_class": _choice(memory.get("memory_class"), MEMORY_CLASSES,
                                defaults["memory_class"], "INVALID_MEMORY_CLASS"),
        "modality": _choice(memory.get("modality"), MODALITIES,
                            defaults["modality"], "INVALID_MEMORY_MODALITY"),
        "retention": _choice(memory.get("retention"), RETENTION_CLASSES,
                             defaults["retention"], "INVALID_MEMORY_RETENTION"),
        "importance": _probability(memory.get("importance"), defaults["importance"],
                                   "INVALID_MEMORY_IMPORTANCE"),
        "confidence": _probability(memory.get("confidence"), defaults["confidence"],
                                   "INVALID_MEMORY_CONFIDENCE"),
        "attributes": _json_object(memory.get("attributes"),
                                   "INVALID_MEMORY_ATTRIBUTES"),
        "related_memory_ids": _string_list(memory.get("related_memory_ids"),
                                           "INVALID_RELATED_MEMORY_IDS"),
    }


_EXECUTE_CUES = re.compile(
    r"(?:몇\s*(?:개|번|명)|한\s*번도|없(?:었|는|나)|당시|그때|현재\s*상태|"
    r"언제부터|언제까지|기준으로|count|how many|as of|at that time|never|none)",
    re.IGNORECASE,
)
_SEARCH_CUES = re.compile(
    r"(?:기억|전에|예전에|어제|지난|이전|아까|저번|우리|내가|내\s|나의|"
    r"했던|하던|수정|작성|결정|취향|약속|대화|세션|프로젝트|문서|파일|작업|"
    r"remember|previous|earlier|yesterday|last\s|my\s|we\s|our\s|resume)",
    re.IGNORECASE,
)


def decide_recall(query, policy="auto"):
    if not isinstance(query, str) or not query.strip():
        raise ValueError("CONTEXT_QUERY_REQUIRED")
    if not isinstance(policy, str) or policy.casefold() not in {"auto", "always", "never"}:
        raise ValueError("INVALID_RECALL_POLICY")
    policy = policy.casefold()
    if policy == "never":
        return {"mode": "SKIP", "reason": "RECALL_DISABLED_BY_CALLER"}
    if policy == "always":
        return {"mode": "SEARCH", "reason": "RECALL_REQUIRED_BY_CALLER"}
    if _EXECUTE_CUES.search(query):
        return {"mode": "EXECUTE", "reason": "TEMPORAL_OR_COMPLETE_SCOPE_CUE"}
    if _SEARCH_CUES.search(query):
        return {"mode": "SEARCH", "reason": "PERSONAL_HISTORY_CUE"}
    return {"mode": "SKIP", "reason": "NO_PERSONAL_MEMORY_CUE"}


def _memory_item(memory):
    content = memory["content"]
    if memory.get("earlier"):          # a fact: its value now, then what it was before
        content += "\n(current since " + memory["effective_at"][:10] + "; before: " + "; ".join(
            f"{e['effective_at'][:10]} {e['content']}" for e in memory["earlier"]) + ")"
    return {
        "id": memory["memory_id"], "source_kind": "CURATED_MEMORY",
        "memory_class": memory.get("memory_class", "EPISODE"),
        "modality": memory.get("modality", "OTHER"),
        "scope": memory["scope"], "title": memory["title"],
        "content": content, "effective_at": memory["effective_at"],
        "known_at": memory["known_at"], "provenance": memory["provenance"],
        "confidence": memory.get("confidence", 1.0),
        "importance": memory.get("importance", 0.5),
    }


def _conversation_item(turn):
    if turn["role"] == "document":     # a section of a project document (documents.py)
        title = ("document section, replaced on " + turn["replaced_at"][:10] + " by a newer text"
                 if turn.get("replaced_at") else "document section")
    else:
        title = turn["role"] + " conversation turn"
    return {
        "id": turn["turn_id"], "source_kind": "CONVERSATION_TURN",
        "memory_class": "EPISODE", "modality": "CONVERSATION",
        "scope": turn["scope"], "title": title,
        "content": turn["content"], "effective_at": turn["happened_at"],
        "known_at": turn["happened_at"],
        "provenance": {"source_type": turn["client"],
                       "source_ref": turn["source_path"] + ":" +
                                     str(turn["source_ordinal"])},
        "confidence": 1.0, "importance": 0.35,
    }


NOTE_TURNS = 10      # other turns beside a note (phase 6 eval: research/consolidation_eval.py)


def build_context_pack(recall_result, *, max_chars=6000, order="relevance", short_sources=False):
    """Create a bounded, source-labelled packet instead of replaying full history.

    order="time" lays memories and conversation turns out together, newest first. With the
    default, curated memories go first and can use up the budget before any recent turn fits,
    which is exactly wrong for "what was I just doing"."""
    if type(max_chars) is not int or not 256 <= max_chars <= 200000:
        raise ValueError("INVALID_CONTEXT_CHAR_BUDGET")
    memories = [_memory_item(item) for item in recall_result.get("matches", [])]
    # A note the model wrote after answering from old turns cites them: the note stands in for
    # those turns, and with a note in the packet fewer other turns are needed.
    noted = {t for m in recall_result.get("matches", [])
             if isinstance(refs := (m.get("attributes") or {}).get("from_turns"), list)
             for t in refs if isinstance(t, str)}
    turns = [_conversation_item(item)
             for item in recall_result.get("conversation_turns", [])
             if item["turn_id"] not in noted]
    if noted:
        turns = turns[:NOTE_TURNS]
    candidates = memories + turns
    if order == "time":
        candidates.sort(key=lambda item: item.get("effective_at") or "", reverse=True)
    else:
        # Memories first, but only up to a quarter of the budget, then turns, then the rest of
        # the memories. Uncapped, 30 short facts took half the packet and pushed out the turns
        # that answered (facts layer eval: turns 30 -> 16, two preference answers lost).
        first, share = [], 0
        for item in memories:
            share += len(item["content"]) + len(item["title"]) + 120
            if share > max_chars // 4:
                break
            first.append(item)
        candidates = first + turns + memories[len(first):]
    sections, selected, used = [], [], 0
    for item in candidates:
        separator = 2 if sections else 0
        # a saved fact's title is often its first words: printed twice, it took a tenth of a
        # 1,200-character hook packet
        title = "" if item["content"].startswith(item["title"]) else item["title"]
        header = (f"[{item['source_kind']} | {item['memory_class']} | {item['scope']} | "
                  f"{item['effective_at']}] {title}".rstrip() + "\n")
        source = item["provenance"]
        reference = str(source.get("source_ref", "unknown"))
        if short_sources:     # the file's name, not its path: ~100 characters an item in the hook
            reference = reference.replace("\\", "/").rsplit("/", 1)[-1]
        footer = f"\nSource: {source.get('source_type', 'unknown')} / {reference}"
        available = max_chars - used - separator
        if available <= len(header) + len(footer) + 16:
            continue
        body = item["content"]
        truncated = False
        if len(header) + len(body) + len(footer) > available:
            keep = max(0, available - len(header) - len(footer) - 15)
            body = body[:keep] + "…[TRUNCATED]"
            truncated = True
        section = header + body + footer
        sections.append(section)
        selected.append({**item, "content": body, "truncated": truncated})
        used += separator + len(section)
        if truncated:
            break
    context = "\n\n".join(sections)
    return {
        "status": "KNOWN" if selected else "UNKNOWN",
        "schema_version": SCHEMA_VERSION,
        "scope_completeness": recall_result.get("scope_completeness", "UNATTESTED"),
        "char_budget": max_chars, "used_chars": len(context),
        "estimated_tokens": ceil(len(context) / 4) if context else 0,
        "selected_items": len(selected),
        "omitted_items": max(0, len(candidates) - len(selected)),
        "items": selected, "context": context,
    }
