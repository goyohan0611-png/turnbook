"""Target-blind language adapter for Graph-MIND associative recall.

The planner sees one user question and no memory contents, expected source IDs, or
human-authored oracle cues.  Its only job is to normalize the question into a
small set of composable retrieval concepts.  Evidence selection remains the
responsibility of Graph-MIND.
"""
from __future__ import annotations

from urllib import error, request
import hashlib
import json
import os
import time


PLANNER_VERSION = "llm-concept-planner-v0.7.1"
DEFAULT_MODEL = "gpt-5.6-luna"
MINIMUM_CONCEPT_COVERAGE = 0.75
INSTRUCTIONS = """Convert one user question into compact concept cues for searching the
user's private long-term memory. You cannot see the memory store and must not guess the
answer. Use only concepts supported by the question. Normalize Korean descriptions into
short English technical concepts when helpful, while preserving any explicit product,
version, file, model, or proper name. For an indirect description, you may infer a standard
technical term that is directly entailed by the description. Never invent a concrete answer,
version, filename, date, person, or decision. Return 2 to 6 distinct cues for a memory-recall
question. Each cue should contain 1 to 6 content words. Avoid standalone generic cues such as
memory, project, task, work, result, information, or question. Use status=abstain only when the
input is not asking to recall prior information or has no identifiable retrieval subject."""

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["plan", "abstain"]},
        "concept_cues": {
            "type": "array",
            "maxItems": 6,
            "items": {"type": "string", "minLength": 2, "maxLength": 80},
        },
    },
    "required": ["status", "concept_cues"],
}


def digest(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_plan(value):
    if not isinstance(value, dict) or set(value) != {"status", "concept_cues"}:
        raise ValueError("INVALID_CONCEPT_PLAN_SCHEMA")
    status, cues = value["status"], value["concept_cues"]
    if status not in {"plan", "abstain"} or not isinstance(cues, list):
        raise ValueError("INVALID_CONCEPT_PLAN_SCHEMA")
    if status == "abstain":
        if cues:
            raise ValueError("ABSTAIN_PLAN_HAS_CUES")
        return {"status": status, "concept_cues": []}
    if not 2 <= len(cues) <= 6:
        raise ValueError("INVALID_CONCEPT_CUE_COUNT")
    normalized = []
    for cue in cues:
        if not isinstance(cue, str):
            raise ValueError("INVALID_CONCEPT_CUE")
        cue = " ".join(cue.split()).strip()
        if not 2 <= len(cue) <= 80:
            raise ValueError("INVALID_CONCEPT_CUE")
        if cue.casefold() not in {item.casefold() for item in normalized}:
            normalized.append(cue)
    if len(normalized) < 2:
        raise ValueError("INSUFFICIENT_DISTINCT_CONCEPT_CUES")
    return {"status": status, "concept_cues": normalized}


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


class OpenAIResponsesConceptPlanner:
    """Small stateless Responses API adapter; credentials never enter result files."""

    def __init__(self, model=DEFAULT_MODEL, *, api_key=None, timeout=60):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("CONCEPT_PLANNER_MODEL_REQUIRED")
        self.model = model.strip()
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY_REQUIRED")
        self.timeout = timeout

    def request_payload(self, question):
        if not isinstance(question, str) or not question.strip():
            raise ValueError("CONCEPT_PLAN_QUESTION_REQUIRED")
        return {
            "model": self.model,
            "store": False,
            "instructions": INSTRUCTIONS,
            "input": [{"role": "user", "content": question.strip()}],
            "max_output_tokens": 500,
            "reasoning": {"effort": "low"},
            "text": {
                "verbosity": "low",
                "format": {
                    "type": "json_schema",
                    "name": "turnbook_concept_plan",
                    "strict": True,
                    "schema": PLAN_SCHEMA,
                },
            },
        }

    def plan(self, question):
        body = self.request_payload(question)
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
            parsed = validate_plan(json.loads(_output_text(payload)))
        except (json.JSONDecodeError, ValueError, RuntimeError) as exc:
            # Invalid model output is a parser abstention, not a transport
            # failure. This preserves fail-closed behavior and lets the
            # benchmark account for the failure instead of silently stopping.
            parsed = {"status": "abstain", "concept_cues": []}
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
