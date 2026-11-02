"""Two PCs, one brain: the real MCP tools, two indexes, one shared folder.

What has to hold for it to feel like one memory and not two machines reading each other: a memory
saved on one PC is recalled on the other; a fact corrected on the second PC replaces the first PC's
version everywhere; and nothing but plain log lines is ever written into the shared folder.
"""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
from contextlib import contextmanager
import json
import os
import shutil
import tempfile
import time
import unittest
import unittest.mock

from mcp import Client

from turnbook.mcp_server import build_server, wait_for_index


def serve(path):
    return build_server(path, terminal=True)          # the user at a terminal: may move memory


@contextmanager
def plain_directory():
    """A temporary directory made with plain mkdir. tempfile's directories are owner-only, and
    when the tests run elevated (CI) the owner is the Administrators group, which initdb drops
    from its token before it starts: it then cannot see the directory and fails."""
    path = Path(tempfile.gettempdir()) / f"graph-mind-test-{os.getpid()}-{time.time_ns()}"
    path.mkdir()
    try:
        yield str(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


@contextmanager
def scratch_postgres(root: Path, port: int):
    """A throwaway Postgres on `root` (the hub's own programs), and its admin URL."""
    from turnbook import sharing
    data = root / "pg"
    sharing._initdb_with_retries(data)
    sharing._start(data, port)
    try:
        yield f"postgresql://postgres@127.0.0.1:{port}/postgres"
    finally:
        sharing.stop_postgres(data)


def remember(text, **extra):
    return {"content": text, "title": text, "memory_type": "fact", "source_ref": "test",
            "entities": ["사는 곳"], **extra}


class SharedBrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_pcs_share_one_brain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / "OneDrive" / "Graph-MIND"
            os.environ["TURNBOOK_FOLDER"] = str(shared)
            try:
                os.environ["TURNBOOK_DEVICE"] = "desktop"
                async with Client(serve(root / "desktop-index.sqlite")) as desktop:
                    saved = await desktop.call_tool("brain_remember", remember("사는 곳: 서울"))
                    seoul = saved.structured_content["memory_id"]

                os.environ["TURNBOOK_DEVICE"] = "laptop"
                async with Client(serve(root / "laptop-index.sqlite")) as laptop:
                    seen = await laptop.call_tool("brain_recall", {"query": "사는 곳"})
                    self.assertEqual([m["memory_id"] for m
                                      in seen.structured_content["matches"]], [seoul],
                                     "a memory saved on the desktop is recalled on the laptop")
                    moved = await laptop.call_tool("brain_remember", remember(
                        "사는 곳: 부산", supersedes_memory_id=seoul))
                    self.assertFalse(moved.is_error, "the laptop can replace a desktop memory")
                    busan = moved.structured_content["memory_id"]

                os.environ["TURNBOOK_DEVICE"] = "desktop"
                async with Client(serve(root / "desktop-index.sqlite")) as desktop:
                    now = await desktop.call_tool("brain_recall", {"query": "사는 곳"})
                    self.assertEqual([m["memory_id"] for m
                                      in now.structured_content["matches"]], [busan],
                                     "the correction made on the laptop holds on the desktop")
                    history = await desktop.call_tool("brain_recall", {"entity": "사는 곳"})
                    entries = history.structured_content["entries"]
                    self.assertEqual([e["memory_id"] for e in entries], [seoul, busan])
                    self.assertTrue(entries[0]["superseded"])

                written = sorted(p.relative_to(shared).as_posix() for p in shared.rglob("*")
                                 if p.is_file())
                self.assertEqual(written, ["log/desktop.jsonl", "log/laptop.jsonl"],
                                 "only plain logs live in the shared folder, never a database")
            finally:
                wait_for_index()          # the index files must be closed before cleanup
                os.environ.pop("TURNBOOK_FOLDER", None)
                os.environ.pop("TURNBOOK_DEVICE", None)

    async def test_two_pcs_share_one_brain_through_postgres(self):
        """The same, with the log in Postgres (a real server, started on a temp dir)."""
        with plain_directory() as directory, scratch_postgres(Path(directory), 54392) as url:
            root = Path(directory)
            os.environ["TURNBOOK_FOLDER"] = url
            try:
                os.environ["TURNBOOK_DEVICE"] = "desktop"
                async with Client(serve(root / "desktop-index.sqlite")) as desktop:
                    saved = await desktop.call_tool("brain_remember", remember("사는 곳: 서울"))
                    seoul = saved.structured_content["memory_id"]
                os.environ["TURNBOOK_DEVICE"] = "laptop"
                async with Client(serve(root / "laptop-index.sqlite")) as laptop:
                    seen = await laptop.call_tool("brain_recall", {"query": "사는 곳"})
                    self.assertEqual([m["memory_id"] for m
                                      in seen.structured_content["matches"]], [seoul])
                    moved = await laptop.call_tool("brain_remember", remember(
                        "사는 곳: 부산", supersedes_memory_id=seoul))
                    busan = moved.structured_content["memory_id"]
                    shown = await laptop.call_tool("brain_folder", {})
                    self.assertEqual(shown.structured_content["devices"], ["desktop", "laptop"])
                os.environ["TURNBOOK_DEVICE"] = "desktop"
                async with Client(serve(root / "desktop-index.sqlite")) as desktop:
                    now = await desktop.call_tool("brain_recall", {"query": "사는 곳"})
                    self.assertEqual([m["memory_id"] for m
                                      in now.structured_content["matches"]], [busan])
            finally:
                wait_for_index()
                os.environ.pop("TURNBOOK_FOLDER", None)
                os.environ.pop("TURNBOOK_DEVICE", None)

    async def test_connection_code_joins_another_pc(self):
        """'다른 PC도 붙게 해줘' on the desktop, the code pasted on the laptop: one brain.
        The laptop comes in over the network address (not 127.0.0.1), so it is the password
        that lets it in."""
        from turnbook import sharing
        import psycopg
        with plain_directory() as directory:
            root = Path(directory)
            port, sharing.PORT = sharing.PORT, 54391        # never the real hub's port
            sharing._LOCAL_URI.clear()
            home_before = os.environ["TURNBOOK_HOME"]          # the isolated one (_isolate)
            os.environ["TURNBOOK_HOME"] = str(root / "home")
            try:
                os.environ["TURNBOOK_DEVICE"] = "desktop"
                os.environ["TURNBOOK_CONFIG"] = str(root / "desktop-config.json")
                with unittest.mock.patch.object(sharing, "_firewall", return_value="open"):
                    async with Client(serve(root / "desktop-index.sqlite")) as desktop:
                        shared = await desktop.call_tool("brain_folder", {"path": "share"})
                        await desktop.call_tool("brain_remember", remember("사는 곳: 서울"))
                code = shared.structured_content["connection_code"]
                self.assertTrue(code.startswith("gm1."))
                self.assertEqual(shared.structured_content["folder"], "this PC's Postgres")
                value = sharing._decode(code)
                self.assertNotIn("127.0.0.1", value["h"])
                lan = value["h"][0]
                pinned = sharing.tls_options(value["c"])
                with psycopg.connect(f"postgresql://graphmind:{value['k']}@{lan}:54391/graphmind"
                                     + pinned) as c:
                    self.assertFalse(c.execute("SELECT rolsuper FROM pg_roles "
                                               "WHERE rolname = current_user").fetchone()[0])
                    self.assertTrue(c.info.pgconn.ssl_in_use)          # TLS, this hub's own
                with self.assertRaises(psycopg.OperationalError):    # never in clear
                    psycopg.connect(f"postgresql://graphmind:{value['k']}@{lan}:54391/graphmind"
                                    "?sslmode=disable")
                with self.assertRaises(psycopg.OperationalError):
                    psycopg.connect(f"postgresql://graphmind:wrong@{lan}:54391/graphmind")
                with self.assertRaises(psycopg.OperationalError):    # the admin stays local
                    psycopg.connect(f"postgresql://postgres@{lan}:54391/postgres")

                os.environ["TURNBOOK_DEVICE"] = "laptop"
                os.environ["TURNBOOK_CONFIG"] = str(root / "laptop-config.json")
                async with Client(serve(root / "laptop-index.sqlite")) as laptop:
                    joined = await laptop.call_tool("brain_folder", {"path": code})
                    self.assertEqual(joined.structured_content["folder"],
                                     f"Postgres on {lan}:54391")
                    self.assertNotIn(value["k"], str(joined.structured_content))
                    seen = await laptop.call_tool("brain_recall", {"query": "사는 곳"})
                    self.assertEqual([m["content"] for m in seen.structured_content["matches"]],
                                     ["사는 곳: 서울"])
                    self.assertEqual(joined.structured_content["devices"], ["desktop"])
                # what crosses the network and sits in the hub is ciphertext only
                url = f"postgresql://graphmind:{value['k']}@{lan}:54391/graphmind"
                with psycopg.connect(url) as c:
                    stored = [row[0] for row in c.execute("SELECT line FROM sharing")]
                self.assertTrue(stored)
                self.assertTrue(all(s.startswith(sharing.SEALED) for s in stored))
                self.assertFalse(any("서울" in s for s in stored))
                with self.assertRaises(ValueError):
                    sharing.set_folder("gm1.not-a-code")
            finally:
                wait_for_index()
                for name in ("TURNBOOK_DEVICE", "TURNBOOK_CONFIG"):
                    os.environ.pop(name, None)
                os.environ["TURNBOOK_HOME"] = home_before
                sharing.stop_postgres(root / "home" / "postgres")
                sharing.PORT = port
                sharing._LOCAL_URI.clear()
                sharing._JOINED.clear()

    async def test_folder_chosen_in_conversation(self):
        """No environment variable: the user names the folder, and that is enough."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / "GoogleDrive" / "Turnbook"
            try:
                os.environ["TURNBOOK_DEVICE"] = "desktop"
                os.environ["TURNBOOK_CONFIG"] = str(root / "desktop-config.json")
                async with Client(serve(root / "desktop-index.sqlite")) as desktop:
                    before = await desktop.call_tool("brain_folder", {})
                    self.assertIsNone(before.structured_content["folder"])
                    await desktop.call_tool("brain_remember", remember("사는 곳: 서울"))
                    chosen = await desktop.call_tool("brain_folder", {
                        "path": str(shared), "share_existing": True})
                    self.assertEqual(chosen.structured_content["existing_memories_shared"]
                                     ["memories"], 1, "the memory saved before is now shared")
                    again = await desktop.call_tool("brain_folder", {
                        "path": str(shared), "share_existing": True})
                    self.assertEqual(again.structured_content["existing_memories_shared"]
                                     ["lines_written"], 0, "sharing twice uploads nothing twice")
                    password = chosen.structured_content["folder_password"]
                    status = await desktop.call_tool("brain_folder", {})
                    self.assertEqual(status.structured_content["folder_password"], password)
                logs = "".join(p.read_text(encoding="utf-8") for p in shared.rglob("*.jsonl"))
                self.assertIn("gme1:", logs)
                self.assertNotIn("서울", logs, "the cloud service sees only ciphertext")

                os.environ["TURNBOOK_DEVICE"] = "laptop"
                os.environ["TURNBOOK_CONFIG"] = str(root / "laptop-config.json")
                async with Client(serve(root / "laptop-index.sqlite")) as laptop:
                    refused = await laptop.call_tool("brain_folder", {"path": str(shared)})
                    self.assertEqual(refused.structured_content["status"],
                                     "FOLDER_PASSWORD_NEEDED")
                    from turnbook import sharing
                    with self.assertRaises(ValueError):
                        sharing.set_folder(shared, "wrong-password")
                    sharing.set_folder(shared, password)     # what the terminal prompt does
                    joined = await laptop.call_tool("brain_folder", {"path": str(shared)})
                    self.assertEqual(joined.structured_content["imported_from_other_devices"], 1)
                    seen = await laptop.call_tool("brain_recall", {"query": "사는 곳"})
                    self.assertEqual(len(seen.structured_content["matches"]), 1,
                                     "the laptop knows what the desktop knew")
            finally:
                wait_for_index()
                os.environ.pop("TURNBOOK_DEVICE", None)
                os.environ.pop("TURNBOOK_CONFIG", None)


class SyncCursorTests(unittest.TestCase):
    def test_lines_are_read_again_until_they_are_imported(self):
        """The cursor moved before the import: a crash in between skipped those lines for good."""
        from turnbook import sharing
        with tempfile.TemporaryDirectory() as directory:
            shared, index = Path(directory) / "shared", Path(directory) / "index.sqlite"
            (shared / "log").mkdir(parents=True)
            (shared / "log" / "desk.jsonl").write_text('{"kind": "x"}\n', encoding="utf-8")
            db = sharing._cursor_db(index)
            try:
                self.assertEqual(len(sharing._new_lines(db, shared)[0]), 1)
                self.assertEqual(len(sharing._new_lines(db, shared)[0]), 1)   # not consumed
            finally:
                db.close()
            sharing.sync(shared, index)
            db = sharing._cursor_db(index)
            try:
                self.assertEqual(sharing._new_lines(db, shared)[0], [])          # now it is
            finally:
                db.close()


class HubCursorTests(unittest.TestCase):
    def test_a_new_hub_is_read_from_its_first_row(self):
        """The cursor was plain "postgres": after reading hub A to row 3, hub B's rows 1-3
        were skipped."""
        import psycopg
        from turnbook import sharing
        from turnbook.conversation_memory import ConversationMemoryStore

        def turn(i):
            return {"turn_id": f"t{i}", "client": "codex", "client_session_id": "s",
                    "scope": "global", "role": "user", "content_redacted": f"note {i}",
                    "raw_sha256": f"t{i}", "source_path": "p", "source_ordinal": i,
                    "happened_at": "2026-10-01T09:00:00", "known_at": "2026-10-01T09:00:00",
                    "redaction_count": 0, "cwd": None}
        with plain_directory() as directory, scratch_postgres(Path(directory), 54397) as url, \
                unittest.mock.patch.dict(os.environ, {"TURNBOOK_DEVICE": "desk"}):
            index = Path(directory) / "index.sqlite"
            sharing.append_many(url, [("turn", turn(i)) for i in (1, 2, 3)])   # hub A
            sharing.sync(url, index)
            with psycopg.connect(url, autocommit=True) as admin:              # now hub B
                admin.execute("DROP TABLE sharing, sharing_hub")
            sharing._READERS.clear()
            sharing.append_many(url, [("turn", turn(i)) for i in (4, 5)])
            sharing.sync(url, index)
            with ConversationMemoryStore(index) as store:
                got = sorted(t["turn_id"] for t in store.latest(limit=10)["turns"])
        self.assertEqual(got, ["t1", "t2", "t3", "t4", "t5"])


class MoveFolderTests(unittest.TestCase):
    def test_sharing_existing_memories_again_fills_a_new_folder(self):
        """Exported ids were kept without where they went: after moving to an empty folder,
        sharing what this PC had sent nothing."""
        from turnbook import sharing
        from turnbook.conversation_memory import ConversationMemoryStore
        with tempfile.TemporaryDirectory() as directory, \
                unittest.mock.patch.dict(os.environ, {"TURNBOOK_DEVICE": "desk"}):
            root = Path(directory)
            index = root / "index.sqlite"
            with ConversationMemoryStore(index) as store:
                for i in (1, 2):
                    store.record({"turn_id": f"t{i}", "client": "codex",
                                  "client_session_id": "s", "scope": "global", "role": "user",
                                  "content_redacted": f"note {i}", "raw_sha256": f"t{i}",
                                  "source_path": "p", "source_ordinal": i,
                                  "happened_at": "2026-10-01T09:00:00",
                                  "known_at": "2026-10-01T09:00:00"})
            first = sharing.export_history(root / "A", index)
            again = sharing.export_history(root / "A", index)
            moved = sharing.export_history(root / "B", index)
        self.assertEqual((first["turns"], again["turns"], moved["turns"]), (2, 0, 2))


class DamagedLogTests(unittest.TestCase):
    def test_a_torn_line_or_a_malformed_record_loses_nothing_else(self):
        """Codex round 10: a crash mid-write glued the next record to half a line; one record
        with a numeric written_at stopped every later sync."""
        from turnbook import sharing
        from turnbook.conversation_memory import ConversationMemoryStore
        with tempfile.TemporaryDirectory() as directory, \
                unittest.mock.patch.dict(os.environ, {"TURNBOOK_DEVICE": "desk"}):
            shared, index = Path(directory) / "shared", Path(directory) / "index.sqlite"
            (shared / "log").mkdir(parents=True)
            log = shared / "log" / "desk.jsonl"
            log.write_text('{"kind": "turn", "written_at": 1, "record": {}}\n'
                           '{"kind": "forget"}\n{"kind": "turn", "rec', encoding="utf-8")

            def turn(i):
                return {"turn_id": f"t{i}", "client": "codex", "client_session_id": "s",
                        "scope": "global", "role": "user", "content_redacted": f"note {i}",
                        "raw_sha256": f"t{i}", "source_path": "p", "source_ordinal": i,
                        "happened_at": "2026-10-01T09:00:00", "known_at": "2026-10-01T09:00:00",
                        "redaction_count": 0, "cwd": None}
            sharing.append(shared, "turn", turn(1))
            sharing.sync(shared, index)
            sharing.append(shared, "turn", turn(2))
            sharing.sync(shared, index)
            with ConversationMemoryStore(index) as store:
                got = sorted(t["turn_id"] for t in store.latest(limit=10)["turns"])
        self.assertEqual(got, ["t1", "t2"])

    def test_a_malformed_connection_code_is_refused(self):
        import base64
        import json
        from turnbook import sharing

        def code(**changes):
            value = {"h": ["192.168.0.2"], "p": 54329, "k": "pw", "c": None, **changes}
            return sharing.CODE_PREFIX + base64.urlsafe_b64encode(
                json.dumps(value).encode()).decode()
        self.assertEqual(sharing._decode(code())["h"], ["192.168.0.2"])
        for bad in ({"c": 7}, {"h": ["x host=evil"]}, {"p": 70000}):
            with self.assertRaisesRegex(ValueError, "INVALID_CONNECTION_CODE"):
                sharing._decode(code(**bad))


class SameNameTests(unittest.TestCase):
    def test_two_pcs_with_one_name_write_separate_logs(self):
        """Both appended to user-pc.jsonl, and the sync client kept only one PC's copy."""
        from turnbook import sharing
        with tempfile.TemporaryDirectory() as directory, \
                unittest.mock.patch.dict(os.environ, {}, clear=False), \
                unittest.mock.patch.object(sharing.platform, "node", return_value="USER-PC"):
            os.environ.pop("TURNBOOK_DEVICE", None)
            shared = Path(directory) / "shared"
            names, password = [], None
            for pc in ("first", "second"):       # both chose the folder before either one wrote
                os.environ["TURNBOOK_CONFIG"] = str(Path(directory) / pc / "config.json")
                sharing.set_folder(shared, password)
                password = sharing.folder_password(shared)
            for pc in ("first", "second"):
                os.environ["TURNBOOK_CONFIG"] = str(Path(directory) / pc / "config.json")
                sharing.append(shared, "forget", {"memory_ids": [pc]})
                sharing.append(shared, "forget", {"memory_ids": [pc]})   # same file again
                names.append(sharing.log_name())
            self.assertTrue(all(name.startswith("user-pc-") for name in names))
            self.assertNotEqual(names[1], names[0])
            self.assertEqual(sorted(p.stem for p in (shared / "log").glob("*.jsonl")),
                             sorted(names))


if __name__ == "__main__":
    unittest.main()


class SealTests(unittest.TestCase):
    def test_lines_are_sealed_with_the_code_password(self):
        from turnbook import sharing
        joined = "postgresql://graphmind:s3cret-pw@192.0.2.1:54329/graphmind"
        line = json.dumps({"kind": "turn", "record": {"content_redacted": "회의실 와이파이는 7층"}},
                          ensure_ascii=False)
        sealed = sharing._seal(joined, [line])[0]
        self.assertTrue(sealed.startswith(sharing.SEALED))
        self.assertNotIn("와이파이", sealed)
        self.assertEqual(sharing._unseal(joined, sealed), line)
        stranger = "postgresql://graphmind:another@192.0.2.1:54329/graphmind"
        self.assertIsNone(sharing._unseal(stranger, sealed), "another key opens nothing")
        # a hub with a key refuses plain lines (sharing seals the old ones): forged otherwise
        self.assertIsNone(sharing._unseal(stranger, line), "a plain line on a keyed hub")

    def test_a_synced_folder_holds_only_ciphertext(self):
        from turnbook import sharing
        from turnbook.forget import scrub_shared
        with tempfile.TemporaryDirectory() as directory, \
                unittest.mock.patch.dict(os.environ, {"TURNBOOK_DEVICE": "desktop"}):
            shared = Path(directory) / "OneDrive" / "Turnbook"
            os.environ["TURNBOOK_CONFIG"] = str(Path(directory) / "desktop.json")
            (shared / "log").mkdir(parents=True)
            turn = {"turn_id": "t1", "content_redacted": "회의실 와이파이는 7층"}
            sharing._folder_append(shared, sharing._lines([("turn", turn)]))   # from before
            sharing.set_folder(shared)
            log = shared / "log" / "desktop.jsonl"
            self.assertNotIn("와이파이", log.read_text(encoding="utf-8"),
                             "what was written before sealing is sealed too")
            self.assertIn(("turn", "t1"), sharing._shared_ids(shared))
            self.assertEqual(scrub_shared(shared, {"turn_ids": ["t1"]}), 1,
                             "forgetting finds a sealed line")

            os.environ["TURNBOOK_DEVICE"] = "laptop"
            os.environ["TURNBOOK_CONFIG"] = str(Path(directory) / "laptop.json")
            with self.assertRaises(PermissionError):         # queued by append, never in clear
                sharing._folder_append(shared, sharing._lines([("turn", turn)]))
            self.assertFalse((shared / "log" / "laptop.jsonl").exists())

    def test_a_pc_that_read_past_sealed_lines_reads_them_once_it_has_the_password(self):
        from turnbook import sharing
        with tempfile.TemporaryDirectory() as directory, \
                unittest.mock.patch.dict(os.environ, {"TURNBOOK_DEVICE": "desktop"}):
            root = Path(directory)
            shared = root / "OneDrive" / "Turnbook"
            os.environ["TURNBOOK_CONFIG"] = str(root / "desktop.json")
            sharing.set_folder(shared)
            password = sharing.folder_password(shared)
            sharing.append(shared, "turn", {
                "turn_id": "t9", "client": "codex", "client_session_id": "s", "scope": "global",
                "role": "user", "content_redacted": "주차는 B2", "raw_sha256": "t9",
                "source_path": "p", "happened_at": "2026-11-01T09:00:00",
                "known_at": "2026-11-01T09:00:00", "source_ordinal": 1})
            os.environ["TURNBOOK_DEVICE"] = "laptop"
            os.environ["TURNBOOK_CONFIG"] = str(root / "laptop.json")
            index = root / "laptop.sqlite"
            db = sharing._cursor_db(index)                  # an older version skipped them all
            log = shared / "log" / "desktop.jsonl"
            db.execute("INSERT INTO brain_log_cursor(source, offset, mark) VALUES(?,?,?)",
                       (log.name, log.stat().st_size, None))
            db.commit()
            db.close()
            sharing.set_folder(shared, password)
            self.assertEqual(sharing.sync(shared, index)["imported"], 1)
            self.assertEqual(sharing.sync(shared, index)["imported"], 0, "and only once")


class OutboxTests(unittest.TestCase):
    def test_a_line_cut_off_in_the_outbox_does_not_swallow_the_next(self):
        from turnbook import sharing

        def down(url, lines):
            raise OSError("connection refused")
        url = "postgresql://graphmind@127.0.0.1:1/graphmind"
        sharing._outbox().parent.mkdir(parents=True, exist_ok=True)
        sharing._outbox().write_text('{"kind": "turn", "rec', encoding="utf-8")   # torn
        sharing._HUB_DOWN.clear()
        with unittest.mock.patch.object(sharing, "_pg_append", down):
            sharing.append(url, "turn", {"turn_id": "t9"})
        last = sharing._outbox().read_text(encoding="utf-8").split("\n")[-2]
        self.assertEqual(json.loads(last)["record"]["turn_id"], "t9")
        sharing._outbox().unlink()
        sharing._HUB_DOWN.clear()

    def test_lines_wait_while_the_hub_is_down_and_go_with_the_next_append(self):
        from turnbook import sharing
        sent, hub_up = [], [False]

        def fake_append(url, lines):
            if not hub_up[0]:
                raise OSError("connection refused")
            sent.extend(lines)
            return len(lines)
        with unittest.mock.patch.object(sharing, "_pg_append", fake_append):
            url = "postgresql://graphmind@127.0.0.1:1/graphmind"
            sharing.append(url, "turn", {"turn_id": "t1"})      # hub down: queued, no error
            sharing.append(url, "turn", {"turn_id": "t2"})
            self.assertEqual(sent, [])
            self.assertTrue(sharing._outbox().exists())
            hub_up[0] = True
            sharing.append(url, "turn", {"turn_id": "t3"})      # down a moment ago: not tried
            self.assertEqual(sent, [])
            sharing._HUB_DOWN.clear()                            # a minute later
            sharing.append(url, "turn", {"turn_id": "t4"})
        self.assertEqual([json.loads(s)["record"]["turn_id"] for s in sent],
                         ["t1", "t2", "t3", "t4"])
        self.assertFalse(sharing._outbox().exists())

    def test_a_shared_folder_that_cannot_be_written_queues_the_turn(self):
        from turnbook import sharing
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory)
            sharing._HUB_DOWN.clear()
            with unittest.mock.patch.object(sharing, "_folder_append",
                                            side_effect=PermissionError("OneDrive has it")):
                sharing.append(shared, "turn", {"turn_id": "f1"})       # no error
            self.assertTrue(sharing._outbox().exists())
            sharing._HUB_DOWN.clear()                                    # a minute later
            sharing.append(shared, "turn", {"turn_id": "f2"})
            log = next((shared / "log").glob("*.jsonl")).read_text(encoding="utf-8")
            self.assertEqual([json.loads(line)["record"]["turn_id"]
                              for line in log.split("\n") if line], ["f1", "f2"])
            self.assertFalse(sharing._outbox().exists())


class NoPgserverTests(unittest.TestCase):
    def test_hosting_where_postgres_has_no_build_says_why(self):
        """A machine Postgres has no build for: a clear reason, and nothing downloaded."""
        from turnbook import sharing
        with (unittest.mock.patch("platform.machine", return_value="riscv64"),
              unittest.mock.patch("urllib.request.urlopen") as download):
            with self.assertRaisesRegex(RuntimeError, "not supported yet"):
                sharing.postgres_bin()
        download.assert_not_called()
