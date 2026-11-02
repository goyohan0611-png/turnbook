"""Separate source observation time, state valid time and knowledge availability.

Completeness and persistence are trusted backend policies, never plan assertions.
Answers concern a certified stored state model, not unobserved physical reality.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import math

from turnbook.clock import timestamp  # noqa: F401  (re-exported for the research modules)
from executable_memory import TypedMemoryIndex
from memory_contract import strict_object
from state_relation_contract import SINGLE_STATE_RELATIONS, VERSION as STATE_CONTRACT_VERSION, single_state_relation

STATE_QUERY_SCHEMA = strict_object({
    "mode": {"type": "string", "enum": ["LATEST_OBSERVATION", "STATE_AS_OF", "CURRENT_STATE"]},
    "memory_id": {"type": "string"}, "entity_id": {"type": "string"},
    "relation": {"type": "string", "enum": list(SINGLE_STATE_RELATIONS)},
    "source_scope": {"type": "string", "enum": ["USER_MEMORY", "ASSISTANT_HISTORY", "BOTH"]},
    "clock": {"type": "string", "enum": ["UTC", "SOURCE_LOCAL"]}, "as_of": {"type": "string"},
    "knowledge_cutoff": {"type": ["string", "null"]},
    "max_observation_age_seconds": {"type": ["number", "null"], "minimum": 0}})


@dataclass(frozen=True)
class StateScopeCertificate:
    memory_id: str
    entity_id: str
    relation: str
    source_scope: str
    knowledge_through: str
    history_complete: bool = False
    carry_forward: bool = False


def observation_timestamp(event, recorded, clock):
    """Shared validation for full execution and the derived temporal index."""
    supported = event.get("observation_time_verified") is True or (
        event.get("semantic_review", {}).get("time_kind") == "CURRENT_OBSERVATION")
    if not supported:
        raise ValueError("OBSERVATION_TIME_NOT_VERIFIED")
    observed = timestamp(event.get("observed_at"), clock)
    if observed > recorded:
        raise ValueError("OBSERVATION_AFTER_RECORDING")
    return observed


def execute_state_query(query, index: TypedMemoryIndex, certificate=None):
    """An explicitly single-valued state query over relation and ID postings.

    Latest observation returns OBSERVED and never implies continued validity.
    STATE_AS_OF/CURRENT_STATE require a backend-certified history and explicit
    valid intervals or verified state changes plus a carry-forward policy.
    """
    audit, evidence = {}, []

    def result(status, reason, value=None, basis=None, **extra):
        return {"result": {"status": status, "value": value, "reason": reason,
                           "basis": basis, "evidence_event_ids": list(dict.fromkeys(evidence)),
                           "clock": query.get("clock"), **extra}, "audit": audit}

    def fail(reason, **extra):
        return result("UNKNOWN", reason, **extra)

    try:
        mode = query["mode"]
        clock = query["clock"]
        memory_id, entity_id = query["memory_id"], query["entity_id"]
        scope, relation = query["source_scope"], query["relation"]
        if mode not in {"LATEST_OBSERVATION", "STATE_AS_OF", "CURRENT_STATE"}:
            return fail("INVALID_STATE_MODE")
        if not memory_id or not entity_id or not single_state_relation(relation):
            return fail("UNSUPPORTED_STATE_KEY")
        if scope not in {"USER_MEMORY", "ASSISTANT_HISTORY", "BOTH"}:
            return fail("INVALID_SOURCE_SCOPE")
        as_of = timestamp(query["as_of"], clock)
        cutoff = timestamp(query.get("knowledge_cutoff") or query["as_of"], clock)
        if mode == "CURRENT_STATE" and cutoff < as_of:
            return fail("CURRENT_KNOWLEDGE_CUTOFF_BEFORE_AS_OF")
        max_age = query.get("max_observation_age_seconds")
        if max_age is not None and (isinstance(max_age, bool) or not isinstance(max_age, (int, float))
                                    or not math.isfinite(max_age) or max_age < 0):
            return fail("INVALID_OBSERVATION_AGE_POLICY")
    except (KeyError, ValueError) as error:
        return fail(str(error))

    # No value/lexical filter can hide competing locations from the state history.
    rows, audit = index.candidates({"relations": [relation], "source_scope": scope,
        "participant_bindings": [{"role": "SUBJECT", "entity_ids": [entity_id]}]})
    candidates = []
    audit.update(plans_excluded=0, future_recordings_excluded=0, foreign_memory_excluded=0)
    for event in rows:
        if event.get("memory_id") != memory_id:
            audit["foreign_memory_excluded"] += 1
            continue
        if event.get("fact_mode") == "PLANNED":
            audit["plans_excluded"] += 1
            continue
        if event.get("fact_mode") != "ASSERTED":
            return fail("STATE_FACT_MODE_UNRESOLVED")
        if relation != 'LOCATED_AT' and event.get('state_contract', {}).get('version') != STATE_CONTRACT_VERSION:
            return fail('EXPLICIT_STATE_RELATION_CONTRACT_REQUIRED')
        if not event.get("evidence_id") or not event.get("object_text"):
            return fail("STATE_VALUE_OR_EVIDENCE_MISSING")
        try:
            recorded = timestamp(event.get("recorded_at") or event.get("session_timestamp"), clock)
        except ValueError as error:
            return fail("RECORDING_" + str(error))
        if recorded > cutoff:
            audit["future_recordings_excluded"] += 1
            continue
        candidates.append((event, recorded))

    observations, valid_records = [], []
    for event, recorded in candidates:
        kind = event.get("state_kind", "OBSERVATION")
        try:
            if kind == "OBSERVATION":
                observed = observation_timestamp(event, recorded, clock)
                if observed <= as_of:
                    observations.append((event, observed))
            elif kind in {"VALID_INTERVAL", "STATE_CHANGE"}:
                if event.get("state_time_verified") is not True:
                    return fail("VALID_TIME_NOT_VERIFIED")
                start = timestamp(event.get("state_valid_from"), clock)
                end = timestamp(event["state_valid_to"], clock) if event.get("state_valid_to") else None
                if end is not None and start >= end:
                    return fail("INVALID_VALID_TIME_INTERVAL")
                if kind == "STATE_CHANGE" and start > recorded:
                    return fail("STATE_CHANGE_AFTER_RECORDING")
                valid_records.append((event, kind, start, end))
            else:
                return fail("UNSUPPORTED_STATE_RECORD_KIND")
        except ValueError as error:
            return fail(str(error))

    last = None
    if observations:
        observed = max(when for _, when in observations)
        tied = [event for event, when in observations if when == observed]
        if len({event["object_text"] for event in tied}) != 1:
            evidence = [event["evidence_id"] for event in tied]
            return fail("CONFLICTING_OBSERVATIONS")
        age = (as_of - observed).total_seconds()
        last = {"value": tied[0]["object_text"], "observed_at": observed.isoformat(),
                "age_seconds": age, "freshness": "UNSPECIFIED" if max_age is None else
                "FRESH" if age <= max_age else "STALE",
                "evidence_event_ids": [event["evidence_id"] for event in tied],
                "continued_validity_established": False}
    if mode == "LATEST_OBSERVATION":
        if last is None:
            return fail("NO_OBSERVATION_AS_OF")
        evidence = last["evidence_event_ids"]
        return result("OBSERVED", "LAST_STORED_OBSERVATION", last["value"], "OBSERVATION",
                      observation=last, continued_validity_established=False)

    if not isinstance(certificate, StateScopeCertificate):
        return fail("STATE_SCOPE_NOT_CERTIFIED", last_observation=last)
    if getattr(index, "state_store_id", None) is not None and (
            getattr(certificate, "store_id", None) != index.state_store_id or
            getattr(certificate, "scope_revision", None) != index.state_scope_revision):
        return fail("STORE_SNAPSHOT_CERTIFICATE_MISMATCH", last_observation=last)
    if ((certificate.memory_id, certificate.entity_id, certificate.relation, certificate.source_scope)
            != (memory_id, entity_id, relation, scope) or certificate.history_complete is not True):
        return fail("STATE_SCOPE_CERTIFICATE_MISMATCH", last_observation=last)
    try:
        if timestamp(certificate.knowledge_through, clock) < cutoff:
            return fail("STATE_CERTIFICATE_TOO_OLD", last_observation=last)
    except ValueError as error:
        return fail(str(error))
    changes = [row for row in valid_records if row[1] == "STATE_CHANGE" and row[2] <= as_of]
    intervals = [row for row in valid_records if row[1] == "VALID_INTERVAL" and row[2] <= as_of
                 and (row[3] is None or as_of < row[3])]
    active = list(intervals)
    if changes:
        if certificate.carry_forward is not True:
            return fail("STATE_PERSISTENCE_NOT_CERTIFIED", last_observation=last)
        latest = max(row[2] for row in changes)
        active += [row for row in changes if row[2] == latest and (row[3] is None or as_of < row[3])]
    if not active:
        return fail("NO_CERTIFIED_VALID_STATE", last_observation=last)
    evidence = [row[0]["evidence_id"] for row in active]
    values = {row[0]["object_text"] for row in active}
    if len(values) != 1:
        return fail("CONFLICTING_VALID_STATES", last_observation=last)
    value = next(iter(values))
    # A contradictory dated observation cannot be dismissed by valid-time metadata.
    for event, observed in observations:
        covered = any(row[2] <= observed and (row[3] is None or observed < row[3]) for row in active)
        if covered and event["object_text"] != value:
            evidence.append(event["evidence_id"])
            return fail("OBSERVATION_CONTRADICTS_VALID_STATE", last_observation=last)
    return result("ANSWER", "CERTIFIED_STORED_STATE", value,
                  "VALID_INTERVAL" if not changes else "CERTIFIED_CARRY_FORWARD",
                  as_of=as_of.isoformat(), knowledge_cutoff=cutoff.isoformat(),
                  claim_scope="CERTIFIED_MEMORY_MODEL", last_observation=last)
