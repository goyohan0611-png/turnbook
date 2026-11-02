import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
import tempfile
import unittest

from turnbook.associative_memory import AssociativeMemoryIndex
from turnbook.conversation_memory import ConversationMemoryStore
from turnbook.local_brain import LocalBrainStore


T1 = "2026-09-16T10:00:00"


class AssociativeMemoryTests(unittest.TestCase):
    def test_explicit_link_resolves_when_target_arrives_later(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brain.sqlite"
            with AssociativeMemoryIndex(path) as index:
                index.add_engram(engram_id="fact", source_kind="FIXTURE", source_id="f",
                    scope="global", happened_at=T1, text="durable preference",
                    related_engram_ids=["episode"])
                index.add_engram(engram_id="episode", source_kind="FIXTURE", source_id="e",
                    scope="global", happened_at=T1, text="original conversation evidence")
                edge = index.db.execute("""SELECT relation FROM associative_edges
                  WHERE source_engram_id='fact' AND target_engram_id='episode'""").fetchone()
                pending = index.db.execute(
                    "SELECT COUNT(*) FROM associative_pending_edges").fetchone()[0]
            self.assertEqual(edge["relation"], "EXPLICIT")
            self.assertEqual(pending, 0)

    def test_spreading_activation_finds_indirect_related_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brain.sqlite"
            with AssociativeMemoryIndex(path) as index:
                index.add_engram(engram_id="a", source_kind="FIXTURE", source_id="a",
                    scope="global", happened_at=T1,
                    text="memory error in the personal brain", cues=["stale"], strength=0.8)
                index.add_engram(engram_id="b", source_kind="FIXTURE", source_id="b",
                    scope="global", happened_at=T1,
                    text="stale state fixed with checkpoint", cues=["stale"], strength=0.8)
                index.add_engram(engram_id="c", source_kind="FIXTURE", source_id="c",
                    scope="global", happened_at=T1,
                    text="checkpoint design documented in architecture note",
                    cues=["checkpoint"], strength=0.8)
                result = index.activate("personal brain memory error", hops=2, limit=3)
            ids = [item["engram_id"] for item in result["results"]]
            self.assertEqual(ids[0], "a")
            self.assertIn("c", ids)
            c = next(item for item in result["results"] if item["engram_id"] == "c")
            self.assertEqual(len(c["activation_path"]), 2)

    def test_activation_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brain.sqlite"
            with AssociativeMemoryIndex(path) as index:
                for number in range(30):
                    index.add_engram(engram_id=f"e{number}", source_kind="FIXTURE",
                        source_id=str(number), scope="global", happened_at=T1,
                        text=f"shared memory topic item {number}", cues=["shared"],
                        max_automatic_links=8)
                result = index.activate("shared memory", seed_limit=5, hops=4,
                                        fanout=3, max_nodes=7, limit=5)
            self.assertLessEqual(result["seed_count"], 5)
            self.assertLessEqual(result["visited_nodes"], 7)
            self.assertLessEqual(len(result["results"]), 5)

    def test_external_language_adapter_can_supply_composable_concept_cues(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brain.sqlite"
            with AssociativeMemoryIndex(path) as index:
                index.add_engram(engram_id="architecture", source_kind="FIXTURE",
                    source_id="architecture", scope="global", happened_at=T1,
                    text="bounded associative layer with deterministic executor evidence vault")
                index.add_engram(engram_id="generic-name", source_kind="FIXTURE",
                    source_id="generic-name", scope="global", happened_at=T1,
                    text="document name field")
                raw = index.activate("사람 뇌 같은 조합", limit=3)
                planned = index.activate("사람 뇌 같은 조합",
                    concept_cues=["associative", "deterministic executor", "evidence vault"],
                    limit=3, minimum_concept_coverage=0.75)
                withheld = index.activate("내 반려동물 이름이 뭐였지",
                    concept_cues=["pet name"], limit=3,
                    minimum_concept_coverage=0.75)
            self.assertEqual(raw["status"], "UNKNOWN")
            self.assertEqual(planned["results"][0]["engram_id"], "architecture")
            self.assertEqual(len(planned["concept_cues_used"]), 3)
            self.assertEqual(withheld["status"], "UNKNOWN")
            self.assertEqual(withheld["reason"], "INSUFFICIENT_CONCEPT_COVERAGE")

    def test_sync_preserves_sources_as_evidence_pointers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brain.sqlite"
            with LocalBrainStore(path) as brain:
                brain.remember({"memory_id": "preference", "scope": "global",
                    "memory_type": "PREFERENCE", "title": "answer style",
                    "content": "The user prefers concise answers.",
                    "effective_at": T1, "known_at": T1, "actor": "user",
                    "tags": ["style"], "entities": ["user"],
                    "provenance": {"source_type": "fixture", "source_ref": "turn:1"},
                    "supersedes_memory_id": None, "memory_class": "PREFERENCE",
                    "importance": 0.9})
            with ConversationMemoryStore(path) as conversations:
                conversations.record({"turn_id": "turn-2", "client": "other-agent",
                    "client_session_id": "session-1", "scope": "global", "cwd": None,
                    "happened_at": T1, "known_at": T1, "role": "user",
                    "content_redacted": "Please remember my concise answer style.",
                    "raw_sha256": "abc", "redaction_count": 0,
                    "source_path": "fixture.jsonl", "source_ordinal": 1})
            with AssociativeMemoryIndex(path) as index:
                synced = index.sync_sources()
                result = index.activate("concise answer style", limit=5)
                again = index.sync_sources()
            self.assertEqual(synced["indexed_total"], 2)
            self.assertEqual(again["status"], "NO_NEW_ENGRAMS")
            self.assertEqual({item["source_kind"] for item in result["results"]},
                             {"LOCAL_MEMORY", "CONVERSATION"})


if __name__ == "__main__":
    unittest.main()
