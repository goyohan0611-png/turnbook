"""Recency questions, before and after `recent`: the failure that showed up in real use.

Asked "Turnbook, what was I just doing, what is left", the live store answered with notes from
two weeks earlier: word overlap ranked them first and the curated notes filled the packet before
any recent conversation fit. This rebuilds that situation in a small store and checks, for each
question, whether the item that actually answers it reaches the top three of the packet.

    python -m unittest test_recent_recall -v     # also prints the before/after table
"""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
import tempfile
import unittest

from mcp import Client

from turnbook.conversation_memory import ConversationMemoryStore
from turnbook.mcp_server import build_server, wait_for_index
from turnbook.local_brain import LocalBrainStore

NOW = "2026-10-06T10:00:00"


def memory(mid, text, when, entities=(), supersedes=None):
    return {"memory_id": mid, "scope": "global", "memory_type": "fact", "title": text[:40],
            "content": text, "effective_at": when, "known_at": when, "actor": "assistant",
            "tags": [], "entities": list(entities),
            "provenance": {"source_type": "test", "source_ref": "recent-recall"},
            "supersedes_memory_id": supersedes}


def turn(tid, role, text, when):
    return {"turn_id": tid, "client": "claude-code", "client_session_id": "s", "scope": "global",
            "role": role, "content_redacted": text, "raw_sha256": tid, "source_path": "p",
            "happened_at": when, "known_at": when, "source_ordinal": 1}


MEMORIES = [
    memory("handoff", "Turnbook 작업 인수인계: 지금 하던 작업, 남은 일, 다음 단계는 v0.7.2 "
                      "개념 연결이다. 먼저 인수인계 문서를 읽고 보고하라.",
           "2026-09-17T09:32:00", ["Turnbook"]),
    memory("plan-v072", "Turnbook 다음 작업은 grounded-concept-bridge v0.7.2",
           "2026-09-16T16:06:00", ["Turnbook"]),
    # the live store holds many older project notes full of the words such questions use
    *[memory(f"old-note-{i}", f"Turnbook 작업 메모 {i}: 지금 하던 작업과 남은 일, 다음 단계 정리",
             f"2026-09-{10 + i}T10:00:00", ["Turnbook"]) for i in range(6)],
    memory("kongi-adopt", "콩이를 입양했다", "2025-11-02T12:00:00", ["콩이"]),
    memory("kongi-jeju", "콩이를 데리고 제주 협재 해변에 갔다. 바다를 처음 봤다",
           "2026-09-26T15:00:00", ["콩이"]),
    memory("shared-done", "Turnbook 공유 폴더 구조 완료, 두 PC 테스트 통과",
           "2026-10-01T18:30:00", ["Turnbook"]),
    memory("lunch", "점심에 냉면을 먹었다", "2026-10-05T12:30:00"),
    memory("seoul", "사는 곳: 서울", "2025-03-01T09:00:00", ["사는 곳"]),
    memory("busan", "사는 곳: 부산", "2026-10-01T09:00:00", ["사는 곳"], supersedes="seoul"),
]
TURNS = [
    turn("t-old", "assistant", "다음 단계는 v0.7.2 개념 연결입니다", "2026-09-17T09:40:00"),
    turn("t-report", "user", "어제는 캡스톤 보고서 구성을 정리했어", "2026-10-05T20:00:00"),
    turn("t-now", "user", "최근 기록 우선 꺼내기부터 하자, 공유 폴더는 끝났으니까",
         "2026-10-06T09:50:00"),
]

# question, arguments the calling model would send, what must reach the top three
CASES = [
    ("지금 뭐 하고 있었지?", {"query": "지금 뭐 하고 있었지?"}, "t-now"),
    ("Turnbook 지금 하던 작업", {"query": "Turnbook 지금 하던 작업, 남은 일, 다음 단계"},
     "shared-done"),
    ("콩이 최근에 뭐 했지?", {"query": "콩이 최근에 뭐 했지?"}, "kongi-jeju"),
    ("어제 뭐 했지?", {"query": "어제 뭐 했지?", "since": "2026-10-05"}, "t-report"),
]


def ids(packet):
    return packet["ids"][:3]


class RecentRecallTests(unittest.IsolatedAsyncioTestCase):
    async def test_recency_questions_reach_the_top(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            with LocalBrainStore(db) as store:
                for item in MEMORIES:
                    store.remember(item)
            with ConversationMemoryStore(db) as store:
                for item in TURNS:
                    store.record(item)
            rows, after_hits = [], 0
            try:
                async with Client(build_server(db)) as client:
                    for label, arguments, wanted in CASES:
                        base = {**arguments, "as_of": NOW, "policy": "always"}
                        before = await client.call_tool("brain_context", base)
                        after = await client.call_tool("brain_context", {**base, "recent": True})
                        hit_before = wanted in ids(before.structured_content)
                        hit_after = wanted in ids(after.structured_content)
                        after_hits += hit_after
                        rows.append((label, hit_before, hit_after,
                                     ids(after.structured_content)))
                    # a plain question must not regress: relevance, and the current value only
                    plain = await client.call_tool("brain_recall", {"query": "사는 곳",
                                                                    "as_of": NOW})
                    current = [m["memory_id"] for m in plain.structured_content["matches"]]
            finally:
                wait_for_index()
            print("\n{:<28} {:>9} {:>9}   top three with recent".format(
                "question", "recent off", "recent on"))
            for label, hit_before, hit_after, top in rows:
                print(f"{label:<28} {'O' if hit_before else 'x':>9} {'O' if hit_after else 'x':>9}"
                      f"   {top}")
            self.assertEqual(after_hits, len(CASES), rows)
            self.assertEqual(current, ["busan"], "superseded Seoul stays out, without `recent`")


class UntilTests(unittest.TestCase):
    def test_a_later_turn_does_not_crowd_out_the_past(self):
        """until was applied after the limit: the newest turn took the one slot, then went."""
        import tempfile
        from turnbook.conversation_memory import ConversationMemoryStore
        with tempfile.TemporaryDirectory() as directory:
            with ConversationMemoryStore(Path(directory) / "b.sqlite") as store:
                for n, day in ((1, "2026-01-05"), (2, "2026-03-05")):
                    store.record({"turn_id": f"t{n}", "client": "codex", "client_session_id": "s",
                                  "scope": "global", "role": "user",
                                  "content_redacted": f"garden note {n}", "raw_sha256": f"h{n}",
                                  "source_path": "p", "happened_at": day + "T10:00:00",
                                  "known_at": day + "T10:00:00", "source_ordinal": n})
                cut = "2026-02-01T00:00:00"
                self.assertEqual([t["turn_id"] for t in store.latest(limit=1, until=cut)["turns"]],
                                 ["t1"])
                self.assertEqual([t["turn_id"] for t in
                                  store.search("garden", limit=1, until=cut)["turns"]], ["t1"])


if __name__ == "__main__":
    unittest.main()
