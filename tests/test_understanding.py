"""The brain's current understanding (understanding.py): turns handed out in fixed batches, lines
saved all or nothing with the progress, a lost reply answered again, two agents never both
saving from one point, understood facts kept apart from saved ones, forgetting followed."""
import _isolate  # noqa: F401
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from turnbook.context_packet import build_context_pack
from turnbook.conversation_memory import ConversationMemoryStore
from turnbook.local_brain import LocalBrainStore, current_facts
from turnbook.understanding import Understanding


def turn(store, n, text, when, scope="global", role="user"):
    store.record({"turn_id": f"t{n}", "client": "claude-code", "client_session_id": "s",
                  "scope": scope, "role": role, "content_redacted": text, "raw_sha256": f"h{n}",
                  "source_path": "p", "source_ordinal": n, "happened_at": when,
                  "known_at": when})


class UnderstandingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Path(self.folder.name) / "brain.sqlite"
        with ConversationMemoryStore(self.db) as store:
            turn(store, 1, "My old sneakers are under the bed and they smell",
                 "2023-05-25T10:00:00")
            turn(store, 2, "I'll put the old sneakers in a shoe rack in the closet next week",
                 "2023-05-29T10:00:00")
            turn(store, 3, "Project note for the kiosk", "2023-05-29T11:00:00",
                 scope="project:kiosk")

    def tearDown(self):
        self.folder.cleanup()

    def lines(self):
        return [{"kind": "value", "subject": "me", "attribute": "old sneaker storage",
                 "value": "under the bed", "status": "fact", "date": "2023-05-25",
                 "sources": ["t1"]},
                {"kind": "value", "subject": "me", "attribute": "old sneaker storage plan",
                 "value": "shoe rack in the closet", "status": "plan", "date": None,
                 "sources": ["t2"]}]

    def test_a_batch_is_folded_in_once_and_the_progress_moves(self):
        with Understanding(self.db) as u:
            batch = u.next()
            self.assertEqual([t["ref"] for t in batch["turns"]], ["t1", "t2"])  # global only
            self.assertIn("guide", batch)
            saved = u.save(batch["batch"], self.lines())
            self.assertEqual(saved["status"], "OK")
            self.assertEqual(len(saved["saved"]), 2)
            self.assertEqual(u.next()["status"], "UP_TO_DATE")
            self.assertEqual(u.next("project:kiosk")["turns"][0]["text"],
                             "Project note for the kiosk")
            state = u.state("global")
        texts = sorted(m["content"] for m in state)
        self.assertEqual(texts, ["old sneaker storage plan: shoe rack in the closet (plan)",
                                 "old sneaker storage: under the bed"])
        self.assertTrue(all(m["attributes"]["from_turns"] for m in state))

    def test_nothing_worth_keeping_still_marks_the_turns_done(self):
        with Understanding(self.db) as u:
            self.assertEqual(u.save(u.next()["batch"], [])["saved"], [])
            self.assertEqual(u.next()["status"], "UP_TO_DATE")

    def test_a_lost_reply_is_answered_again_and_other_lines_conflict(self):
        with Understanding(self.db) as u:
            batch = u.next()["batch"]
            first = u.save(batch, self.lines())
            self.assertEqual(u.save(batch, self.lines()), first)
            self.assertEqual(u.save(batch, self.lines()[:1])["reason"],
                             "BATCH_SAVED_WITH_OTHER_LINES")
            self.assertEqual(len(u.state("global")), 2)

    def test_two_agents_from_one_point_only_one_saves(self):
        with Understanding(self.db) as a, Understanding(self.db) as b:
            one, two = a.next()["batch"], b.next()["batch"]
            self.assertEqual(a.save(one, self.lines()[:1])["status"], "OK")
            self.assertEqual(b.save(two, self.lines())["reason"], "STALE_BATCH")
            self.assertEqual(len(a.state("global")), 1)          # nothing of the loser

    def test_a_past_turn_imported_late_is_not_skipped(self):
        with Understanding(self.db) as u:
            u.save(u.next()["batch"], [])
            with ConversationMemoryStore(self.db) as store:
                turn(store, 9, "Back in 2022 I lived in Busan", "2022-01-01T10:00:00")
            self.assertEqual([t["text"] for t in u.next()["turns"]],
                             ["Back in 2022 I lived in Busan"])

    def test_refs_outside_the_batch_or_forgotten_since_are_skipped(self):
        from turnbook.forget import erase
        with Understanding(self.db) as u:
            batch = u.next()["batch"]
            erase(self.db, {"turn_ids": ["t1"]})                 # between next and save
            lines = self.lines() + [{**self.lines()[0], "sources": ["t7"]}]
            saved = u.save(batch, lines)
        self.assertEqual([s["line"] for s in saved["saved"]], [1])
        self.assertEqual(sorted(s["line"] for s in saved["skipped"]), [0, 2])

    def test_forgetting_a_turn_takes_the_lines_worked_out_from_it(self):
        from turnbook.forget import erase
        with Understanding(self.db) as u:
            u.save(u.next()["batch"], self.lines())
        erase(self.db, {"turn_ids": ["t1"]})
        with Understanding(self.db) as u:
            self.assertEqual([m["content"] for m in u.state("global")],
                             ["old sneaker storage plan: shoe rack in the closet (plan)"])
        # and a line citing it, arriving later from anywhere, is not stored
        with LocalBrainStore(self.db) as brain:
            late = brain.remember({"memory_id": "late", "scope": "global",
                                   "memory_type": "STATE", "title": "x", "content": "x",
                                   "effective_at": "2023-05-30T00:00:00",
                                   "known_at": "2023-05-30T00:00:00", "actor": "agent",
                                   "tags": [], "entities": [], "supersedes_memory_id": None,
                                   "provenance": {"source_type": "sync", "source_ref": "pc2"},
                                   "attributes": {"from_turns": ["t1"]}})
        self.assertEqual(late["disposition"], "FORGOTTEN")

    def test_versions_keep_history_and_never_mix_with_what_the_user_saved(self):
        with Understanding(self.db) as u:
            batch = u.next()["batch"]
            u.save(batch, [{"kind": "value", "subject": "me", "attribute": "city",
                            "value": "Seoul", "date": "2023-05-29", "sources": ["t2"],
                            "replaced": [["Busan", "2023-05-25"]]}])
            key = u.state("global")[0]["attributes"]["state_key"]
            with ConversationMemoryStore(self.db) as store:
                turn(store, 4, "I moved to Incheon", "2023-06-10T10:00:00")
            again = u.next()
            self.assertEqual(again["state"][0]["state_key"], key)       # shown for reuse
            u.save(again["batch"], [{"kind": "value", "subject": "me", "attribute": "city",
                                     "value": "Incheon", "date": "2023-06-10",
                                     "state_key": key, "sources": ["t1"]}])
            (city,) = u.state("global")
        self.assertEqual(city["content"], "city: Incheon")
        self.assertEqual([e["content"] for e in city["earlier"]], ["city: Seoul", "city: Busan"])
        with LocalBrainStore(self.db) as brain:     # the user's own saved fact stays apart
            brain.remember({"memory_id": "mine", "scope": "global", "memory_type": "FACT",
                            "title": "home city", "content": "Daegu",
                            "effective_at": "2023-01-01T00:00:00",
                            "known_at": "2023-01-01T00:00:00", "actor": "user", "tags": [],
                            "entities": [], "supersedes_memory_id": None,
                            "provenance": {"source_type": "user", "source_ref": "x"},
                            "attributes": {"fact": "me|city"}})
            found = brain.recall("city", as_of=datetime.now().isoformat(),
                                 limit=10)["matches"]
        self.assertEqual(sorted(m["content"] for m in found), ["Daegu", "city: Incheon"])

    def test_a_date_still_ahead_is_a_plan_not_an_event(self):
        with Understanding(self.db) as u:
            u.save(u.next()["batch"], [{"kind": "event", "subject": "trip", "value": "Jeju",
                                        "status": "plan", "date": "2099-01-01",
                                        "sources": ["t2"]}])
            (trip,) = u.state("global")
        self.assertEqual(trip["content"], "trip: Jeju (plan, for 2099-01-01)")
        self.assertIsNone(trip["attributes"]["date"])

    def test_a_state_line_never_stands_in_for_its_turns_in_a_packet(self):
        with Understanding(self.db) as u:
            u.save(u.next()["batch"], self.lines()[:1])
            (line,) = u.state("global")
        memory = {**line, "memory_type": "STATE", "title": "t", "known_at": line["known_at"],
                  "provenance": {"source_type": "understanding", "source_ref": "b"}}
        turn1 = {"turn_id": "t1", "role": "user", "content": "under the bed",
                 "happened_at": "2023-05-25T10:00:00", "scope": "global",
                 "client": "claude-code", "client_session_id": "s", "source_path": "p",
                 "source_ordinal": 1}
        packet = build_context_pack({"status": "KNOWN", "matches": [memory],
                                     "conversation_turns": [turn1]}, max_chars=2000)
        kinds = [item["source_kind"] for item in packet["items"]]
        self.assertIn("STATE", kinds)
        self.assertIn("CONVERSATION_TURN", kinds)

    def test_current_facts_groups_understood_lines_by_key(self):
        rows = [{"memory_id": f"m{n}", "scope": "global", "content": c, "effective_at": e,
                 "known_at": e, "attributes": {"fact": "understood:sk-1"}}
                for n, (c, e) in enumerate([("a", "2023-01-01"), ("b", "2023-02-01")])]
        (one,) = current_facts(rows)
        self.assertEqual(one["content"], "b")

    def test_a_corrected_event_date_becomes_current_even_when_earlier(self):
        with Understanding(self.db) as u:
            u.save(u.next()["batch"], [{"kind": "event", "subject": "trip", "value": "Jeju",
                                        "date": "2023-05-29", "sources": ["t2"]}])
            key = u.state("global")[0]["attributes"]["state_key"]
            with ConversationMemoryStore(self.db) as store:
                turn(store, 5, "Actually the Jeju trip was on the 25th", "2023-06-01T10:00:00")
            u.save(u.next()["batch"], [{"kind": "event", "subject": "trip", "value": "Jeju",
                                        "date": "2023-05-25", "state_key": key,
                                        "sources": ["t1"]}])
            (trip,) = u.state("global")
        self.assertEqual(trip["content"], "trip: Jeju (2023-05-25)")

    def test_a_long_user_turn_is_read_whole_in_pieces_and_assistant_text_is_context(self):
        from turnbook import understanding
        long = " ".join(f"fact{n}" for n in range(3000))           # ~20k characters
        with ConversationMemoryStore(self.db) as store:
            turn(store, 6, long, "2023-06-02T10:00:00")
            turn(store, 7, "x" * 9000, "2023-06-02T10:01:00", role="assistant")
        with Understanding(self.db) as u:
            seen = ""
            for _ in range(10):
                batch = u.next()
                if batch["status"] == "UP_TO_DATE":
                    break
                for t in batch["turns"]:
                    if t["role"] == "user" and "part" in t:     # the long turn's pieces
                        seen += t["text"]
                    elif t["role"] == "assistant":
                        self.assertEqual(len(t["text"]), understanding.ASSISTANT_CHARS)
                u.save(batch["batch"], [])
        self.assertEqual(seen, long)

    def test_too_many_lines_are_refused_and_nothing_moves(self):
        from turnbook import understanding
        with Understanding(self.db) as u:
            batch = u.next()["batch"]
            with self.assertRaises(ValueError):
                u.save(batch, [self.lines()[0]] * (understanding.MAX_LINES + 1))
            self.assertEqual(u.save(batch, self.lines())["status"], "OK")

    def test_an_empty_store_is_up_to_date(self):
        with Understanding(Path(self.folder.name) / "empty.sqlite") as u:
            self.assertEqual(u.next()["status"], "UP_TO_DATE")

    def test_saved_versions_go_to_the_shared_log(self):
        from unittest import mock
        sent = []
        with mock.patch("turnbook.sharing.folder", lambda: Path("shared")), \
                mock.patch("turnbook.sharing.append_many",
                           lambda shared, items: sent.extend(items)):
            with Understanding(self.db) as u:
                u.save(u.next()["batch"], [{**self.lines()[0],
                                            "replaced": [["in the car", "2023-05-01"]]}])
        self.assertEqual([kind for kind, _ in sent], ["memory", "memory"])   # history too
        self.assertEqual({m["memory_type"] for _, m in sent}, {"STATE"})

    def test_a_secret_or_private_text_in_a_line_is_never_stored(self):
        key = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0" * 3
        with Understanding(self.db) as u:
            u.save(u.next()["batch"], [{"kind": "value", "subject": "me",
                                        "attribute": "api key", "sources": ["t1"],
                                        "value": f"{key} <private>home code 4242</private>x"}])
            (line,) = u.state("global")
        self.assertNotIn(key, line["content"])
        self.assertNotIn("4242", line["content"])

    def test_an_unknown_date_stays_unknown_in_the_history(self):
        with Understanding(self.db) as u:
            u.save(u.next()["batch"], [{"kind": "value", "subject": "me", "attribute": "city",
                                        "value": "Seoul", "date": None, "sources": ["t1"]}])
            key = u.state("global")[0]["attributes"]["state_key"]
            with ConversationMemoryStore(self.db) as store:
                turn(store, 8, "I live in Incheon now", "2023-06-10T10:00:00")
            u.save(u.next()["batch"], [{"kind": "value", "subject": "me", "attribute": "city",
                                        "value": "Incheon", "date": None, "state_key": key,
                                        "sources": ["t1"]}])
            (city,) = u.state("global")
        packet = build_context_pack({"status": "KNOWN", "matches": [
            {**city, "memory_type": "STATE", "title": "city",
             "provenance": {"source_type": "understanding", "source_ref": "b"}}],
            "conversation_turns": []}, max_chars=2000)
        self.assertIn("current since date unknown; before: date unknown city: Seoul",
                      packet["context"])


if __name__ == "__main__":
    unittest.main()
