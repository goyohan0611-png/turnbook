"""Source-bound alias/synonym extraction for Graph-MIND grounded concept bridge v0.7.2.

At ingestion each memory record is shown ONLY its own source text.  The model returns
short Korean/English alias cues for concepts that already appear in that text, so a later
paraphrased recall grounds against the same engram signature.  The model never sees the
benchmark questions, expected sources, or any other memory, and must not introduce a fact,
version, name, date, or decision that is absent from the record.  This keeps the bridge
source-bound and free of development-set contamination.

The extractor mirrors llm_concept_planner: strict JSON-schema Structured Outputs,
store=false, fail-closed on invalid output.  Credentials never enter result files.
"""
from __future__ import annotations

from urllib import error, request
import json
import os
import time

from llm_concept_planner import DEFAULT_MODEL, digest  # reuse frozen model + hashing


EXTRACTOR_VERSION = "ingestion-synonyms-v0.7.2"
MAX_SOURCE_CHARS = 4000

INSTRUCTIONS = """You are given the full source text of one item in a user's private long-term
memory. Produce alias cues: short alternative surface forms for concepts that ALREADY APPEAR in
this text, so the item can be retrieved when the user later paraphrases it. Give both Korean and
English forms and well-known technical synonyms for the same concept (for example latency and
속도, associative recall and 연상 검색, executor and 실행기). Only restate, translate, or give a
standard synonym for a concept that is present in the text. Never introduce a new fact, version,
number, filename, date, person, decision, or claim that is not in the text. Do not copy long
verbatim spans; each cue is a compact concept of 1 to 6 content words. Return 3 to 12 distinct
cues. Use status=abstain only when the text has no substantive recallable concept."""

ALIAS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["plan", "abstain"]},
        "alias_cues": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "string", "minLength": 2, "maxLength": 80},
        },
    },
    "required": ["status", "alias_cues"],
}


def validate_aliases(value):
    if not isinstance(value, dict) or set(value) != {"status", "alias_cues"}:
        raise ValueError("INVALID_ALIAS_SCHEMA")
    status, cues = value["status"], value["alias_cues"]
    if status not in {"plan", "abstain"} or not isinstance(cues, list):
        raise ValueError("INVALID_ALIAS_SCHEMA")
    if status == "abstain":
        if cues:
            raise ValueError("ABSTAIN_ALIAS_HAS_CUES")
        return {"status": status, "alias_cues": []}
    normalized = []
    for cue in cues:
        if not isinstance(cue, str):
            raise ValueError("INVALID_ALIAS_CUE")
        cue = " ".join(cue.split()).strip()
        if not 2 <= len(cue) <= 80:
            raise ValueError("INVALID_ALIAS_CUE")
        if cue.casefold() not in {item.casefold() for item in normalized}:
            normalized.append(cue)
    if len(normalized) < 3:
        raise ValueError("INSUFFICIENT_DISTINCT_ALIAS_CUES")
    return {"status": status, "alias_cues": normalized}


def _output_text(payload):
    texts = []
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
    if not texts:
        raise RuntimeError("OPENAI_RESPONSES_OUTPUT_TEXT_MISSING")
    return "".join(texts)


class OpenAIResponsesAliasExtractor:
    """Stateless Responses API adapter; credentials never enter result files."""

    def __init__(self, model=DEFAULT_MODEL, *, api_key=None, timeout=60):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("ALIAS_EXTRACTOR_MODEL_REQUIRED")
        self.model = model.strip()
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY_REQUIRED")
        self.timeout = timeout

    def request_payload(self, source_text):
        if not isinstance(source_text, str) or not source_text.strip():
            raise ValueError("ALIAS_SOURCE_TEXT_REQUIRED")
        return {
            "model": self.model,
            "store": False,
            "instructions": INSTRUCTIONS,
            "input": [{"role": "user", "content": source_text.strip()[:MAX_SOURCE_CHARS]}],
            "max_output_tokens": 500,
            "reasoning": {"effort": "low"},
            "text": {
                "verbosity": "low",
                "format": {
                    "type": "json_schema",
                    "name": "turnbook_source_aliases",
                    "strict": True,
                    "schema": ALIAS_SCHEMA,
                },
            },
        }

    def extract(self, source_text):
        body = self.request_payload(source_text)
        message = request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"},
        )
        started = time.perf_counter()
        try:
            with request.urlopen(message, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            code = "unknown"
            try:
                raw = json.loads(exc.read().decode("utf-8"))
                code = raw.get("error", {}).get("code") or raw.get("error", {}).get("type") or code
            except Exception:
                pass
            raise RuntimeError(f"OPENAI_RESPONSES_HTTP_ERROR:{exc.code}:{code}") from None
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError("OPENAI_RESPONSES_TRANSPORT_ERROR:" + type(exc).__name__) from None
        seconds = time.perf_counter() - started
        usage = payload.get("usage") or {}
        validation_error = None
        try:
            parsed = validate_aliases(json.loads(_output_text(payload)))
        except (json.JSONDecodeError, ValueError, RuntimeError) as exc:
            # Invalid model output is a fail-closed abstention, not a crash: the engram is
            # simply indexed without synonym enrichment.
            parsed = {"status": "abstain", "alias_cues": []}
            validation_error = "INVALID_MODEL_OUTPUT:" + str(exc)
        return {
            **parsed,
            "model": payload.get("model", self.model),
            "response_id": payload.get("id"),
            "latency_ms": round(seconds * 1000, 3),
            "usage": {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
            "validation_error": validation_error,
        }
