"""Project brains on a team hub: a teammate sees only the projects given to them, can never read
the owner's personal memory, acts within their role, and loses access when removed."""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
from unittest import mock
import os
import socket
import tempfile
import time
import unittest

from turnbook import sharing
from turnbook import project_brains as pl


class FolderTests(unittest.TestCase):
    def test_a_bound_folder_names_the_default_project(self):
        root = Path(tempfile.mkdtemp())
        (root / "src" / "deep").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"TURNBOOK_CONFIG": str(root / "config.json")}):
            pl.bind_folder(root, "graph-mind")
            self.assertEqual(pl.folder_project(root / "src" / "deep"), "graph-mind")
            self.assertIsNone(pl.folder_project(Path(tempfile.mkdtemp())))

    def test_without_team_hubs_only_the_own_ledger_is_used(self):
        root = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"TURNBOOK_CONFIG": str(root / "config.json")}):
            found, unreachable = pl.ledgers()
            self.assertEqual((len(found), unreachable), (1, []))
            found[0].create_project("solo", "me")
            found[0].db.close()
            with self.assertRaisesRegex(ValueError, "PROJECT_NOT_FOUND"):
                pl.ledger_for("someone-elses")
            pl.ledger_for("solo").db.close()


    def test_someone_on_several_team_hubs_says_which_one_gets_a_new_project(self):
        import base64
        import json
        code = lambda host: pl.TEAM_PREFIX + base64.urlsafe_b64encode(json.dumps(  # noqa: E731
            {"h": [host], "p": 1, "u": "u", "k": "k", "l": "", "n": "n"}).encode()).decode()
        root = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"TURNBOOK_CONFIG": str(root / "config.json")}):
            sharing._save({"team_hubs": [code("10.0.0.1"), code("10.0.0.2")]})
            with self.assertRaisesRegex(ValueError, "HUB_REQUIRED.*10.0.0.1.*10.0.0.2"):
                pl.home_ledger()


class HubInputTests(unittest.TestCase):
    def test_an_invite_cannot_carry_connection_options(self):
        """A login like "u host=127.0.0.1 hostaddr=<attacker> password=" sent the hub's own
        password to the attacker."""
        import base64
        import json

        def code(**changes):
            value = {"h": ["10.0.0.1"], "p": 54329, "u": "gm_m_minji_ab12cd", "k": "k",
                     "n": "n", **changes}
            return pl.TEAM_PREFIX + base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
        self.assertEqual(pl._decode_team(code())["u"], "gm_m_minji_ab12cd")
        for bad in ({"u": "x host=127.0.0.1 hostaddr=203.0.113.9 password="},
                    {"h": ["10.0.0.1 sslmode=disable"]}, {"h": ["a@b"]}, {"p": 0}):
            with self.assertRaisesRegex(ValueError, "INVALID_TEAM_CODE"):
                pl._decode_team(code(**bad))

    def test_a_key_in_a_reference_or_a_link_is_not_shared(self):
        root = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"TURNBOOK_CONFIG": str(root / "config.json")}):
            ledger = pl.ledgers()[0][0]
            ledger.create_project("solo", "me")
            key = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"
            with self.assertRaisesRegex(ValueError, "SECRET_IN_CHECKPOINT"):
                ledger.publish("solo", "progress", "ok", "me", source_refs=[key])
            with self.assertRaisesRegex(ValueError, "SECRET_IN_CHECKPOINT"):
                ledger.attach("solo", f"https://example.com/x?api_key={key}", "doc", "me")
            ledger.db.close()


class TeamHubTests(unittest.TestCase):
    def test_teammates_cannot_reach_past_their_roles(self):
        """Codex round 9: a temporary table shadowing hub_template ran as superuser; the host's
        own account could be removed (its personal memory tables went with it); a sole owner's
        projects could be handed to themselves on the way out."""
        import psycopg
        owner = pl.own_ledger()
        owner.create_project("alpha", "yohan")
        owner.db.close()
        with mock.patch.object(sharing, "_firewall", return_value="open"):
            minji_code = pl.invite("minji", "owner", ["alpha"], "yohan")["team_code"]
        minji = pl._team_ledger(minji_code)
        with self.assertRaises(psycopg.Error):
            minji.db.execute("CREATE TEMP TABLE hub_template(id int, ddl text)")
        minji.db.close()
        # no SET ROLE to a project's group: kept past a removal, it kept the group's rights
        import hashlib
        minji = pl._team_ledger(minji_code)
        with self.assertRaises(psycopg.Error):
            minji.db.execute(f"SET ROLE p_{hashlib.md5(b'alpha').hexdigest()[:12]}_o")
        minji.db.close()
        minji = pl._team_ledger(minji_code)
        self.assertTrue(minji.create_project("beta", "minji")["created"])
        minji.db.close()
        with self.assertRaisesRegex(ValueError, "HOST_ACCOUNT"):
            pl.remove_account("yohan")
        with self.assertRaisesRegex(ValueError, "SAME_ACCOUNT"):
            pl.remove_account("minji", transfer_to="minji")
        self.assertIn("minji", [a["member"] for a in pl.accounts()])

    def setUp(self):
        self.root = Path(tempfile.gettempdir()) / f"graph-mind-team-{os.getpid()}-{time.time_ns()}"
        self.root.mkdir()
        self.saved = (sharing.PORT, os.environ.get("TURNBOOK_HOME"),
                      os.environ.get("TURNBOOK_CONFIG"))
        # a free port per test, never the real hub's: on Windows the previous test's server was
        # sometimes still shutting down on a shared port, and dropped the next test's connection
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            sharing.PORT = probe.getsockname()[1]
        sharing._LOCAL_URI.clear()
        os.environ["TURNBOOK_HOME"] = str(self.root / "home")
        os.environ["TURNBOOK_CONFIG"] = str(self.root / "owner-config.json")
        sharing._save({"memory_folder": "local", "user_name": "yohan"})

    def tearDown(self):
        try:
            sharing.stop_postgres()
        except Exception:
            pass
        sharing.PORT = self.saved[0]
        sharing._LOCAL_URI.clear()
        for name, value in (("TURNBOOK_HOME", self.saved[1]),
                            ("TURNBOOK_CONFIG", self.saved[2])):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_members_see_their_projects_only_and_never_personal_memory(self):
        owner = pl.own_ledger()
        owner.create_project("alpha", "yohan")
        owner.create_project("beta", "yohan")
        decision = owner.publish("alpha", "decision", "허브는 PC 한 대", "yohan")
        owner.publish("beta", "progress", "다른 팀 일", "yohan")
        owner.db.close()
        shared = sharing.folder()
        sharing.append(shared, "memory", {"memory_id": "m-personal", "scope": "global",
                                            "memory_type": "fact", "title": "연봉",
                                            "content": "연봉 협상 전략", "effective_at": None,
                                            "known_at": None, "actor": "yohan", "tags": [],
                                            "entities": [], "provenance": {},
                                            "supersedes_memory_id": None})
        with mock.patch.object(sharing, "_firewall", return_value="open"):
            editor = pl.invite("minji", "editor", ["alpha"], "yohan")["team_code"]
            viewer = pl.invite("jun", "viewer", ["alpha"], "yohan")["team_code"]

        minji = pl._team_ledger(editor)
        self.assertEqual(minji.names(), ["alpha"])                       # beta is invisible
        self.assertEqual(minji.timeline("alpha", by="minji")[0]["summary"], "허브는 PC 한 대")
        with self.assertRaisesRegex(ValueError, "PROJECT_NOT_FOUND"):
            minji.timeline("beta")
        import psycopg
        with self.assertRaises(psycopg.OperationalError):               # personal memory: her
            psycopg.connect(pl._team_url(pl._decode_team(editor)).replace(  # login cannot even
                f"/{pl.HUB_DATABASE}", "/graphmind"), connect_timeout=5)    # reach its database
        beta = minji.db.execute("SELECT hub_schema('beta')").fetchone()[0]
        with self.assertRaises(psycopg.Error) as denied:                 # nor another project's
            minji.db.execute(f'SELECT * FROM "{beta}".ledger_checkpoints').fetchall()
        self.assertIn("permission denied", str(denied.exception))
        mine = minji.publish("alpha", "progress", "허브 재시도 추가", "minji")
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):
            minji.retract(decision["id"], "minji")                      # someone else's
        minji.retract(mine["id"], "minji")                              # her own
        minji.db.close()

        jun = pl._team_ledger(viewer)
        self.assertEqual(len(jun.timeline("alpha")), 1)
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):
            jun.publish("alpha", "progress", "읽기만 가능", "jun")
        jun.db.close()

        pl.remove_account("minji", "yohan")
        with self.assertRaises(Exception):
            pl._team_ledger(editor).names()
        owner = pl.own_ledger()
        actions = [entry["action"] for entry in owner.audit(limit=100)]
        owner.db.close()
        for action in ("invite", "publish", "retract", "timeline"):
            self.assertIn(action, actions)
        self.assertIn("remove_account", [entry["action"] for entry in pl.hub_audit()])

    def test_owners_run_their_projects_and_the_host_only_runs_the_hub(self):
        import contextlib
        import io
        owner = pl.own_ledger()
        owner.create_project("alpha", "yohan")
        owner.db.close()
        with mock.patch.object(sharing, "_firewall", return_value="open"):
            minji_code = pl.invite("minji", "editor", ["alpha"], "yohan")["team_code"]
            jun_code = pl.add_account("jun")["team_code"]
            self.assertIsNone(pl.add_account("jun")["team_code"])     # the code stays valid
        minji, jun = pl._team_ledger(minji_code), pl._team_ledger(jun_code)
        self.assertEqual(jun.names(), [])                     # an account alone opens nothing
        self.assertTrue(minji.create_project("gamma", "minji")["created"])
        with self.assertRaisesRegex(ValueError, "PROJECT_NAME_TAKEN"):
            jun.create_project("gamma", "jun")
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):
            minji.grant("alpha", "jun", "viewer", "minji")    # an editor there, not the owner
        minji.grant("gamma", "jun", "editor", "minji")
        minji.set_instructions("gamma", "테스트 먼저", "minji")
        minji.publish("gamma", "decision", "감마는 3월 출시", "minji")

        host = pl.own_ledger()                                # who runs the hub sees none of it
        self.assertEqual(host.names(), ["alpha"])
        with self.assertRaisesRegex(ValueError, "PROJECT_NOT_FOUND"):
            host.grant("gamma", "yohan", "owner", "yohan")
        gamma = host.db.execute("SELECT hub_schema('gamma')").fetchone()[0]
        with self.assertRaises(Exception):                    # nor by going around the tools
            host.db.execute(f'SELECT payload FROM "{gamma}".ledger_checkpoints').fetchall()
        host.db.close()

        self.assertEqual(jun.names(), ["gamma"])
        self.assertEqual(jun.instructions("gamma"), "테스트 먼저")
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):
            jun.set_instructions("gamma", "바꿔치기", "jun")
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):
            jun.grant("gamma", "yohan", "viewer", "jun")
        note = jun.publish("gamma", "progress", "준 작업", "jun")
        import psycopg
        with self.assertRaises(psycopg.Error):                # but never rewrite it as jun's
            minji.db.execute("UPDATE ledger_checkpoints SET payload='forged' WHERE id=%s",
                             (note["id"],))
        minji.retract(note["id"], "minji")                    # the owner may take it back
        with self.assertRaisesRegex(ValueError, "LAST_OWNER"):
            minji.revoke("gamma", "minji", "minji")
        with self.assertRaisesRegex(ValueError, "LAST_OWNER"):
            minji.grant("gamma", "minji", "editor", "minji")
        with self.assertRaisesRegex(ValueError, "NO_ACCOUNT"):
            minji.grant("gamma", "nobody", "viewer", "minji")
        self.assertEqual({m["member"]: m["role"] for m in jun.members("gamma")},
                         {"minji": "owner", "jun": "editor"})
        self.assertIn("retract", [e["action"] for e in minji.audit(limit=100)])
        minji.db.close()

        users = {a["member"]: a["projects"] for a in pl.accounts()}
        self.assertEqual(users["minji"], {"alpha": "editor", "gamma": "owner"})
        with self.assertRaisesRegex(ValueError, "LAST_OWNER"):
            pl.remove_account("minji")
        pl.remove_account("minji", transfer_to="jun")
        self.assertEqual(jun.members("gamma"), [{"member": "jun", "role": "owner"}])
        jun.db.close()
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(pl.hub_main(["users"]), 0)
        self.assertIn('"jun"', printed.getvalue())


    def test_a_teammate_picks_up_the_work_through_their_own_assistant(self):
        """A publishes and links from their assistant; B, on their own config, joins with the
        code and asks where things stand. Everything through brain_checkpoint."""
        import asyncio
        from mcp import Client
        from turnbook.mcp_server import build_server

        def current_instructions():
            ledger = pl.ledger_for("graph-mind")
            try:
                return ledger.instructions("graph-mind")
            finally:
                ledger.db.close()

        async def run():
            async with Client(build_server(self.root / "a.sqlite")) as a:
                call = lambda **k: a.call_tool("brain_checkpoint", k)  # noqa: E731
                await call(action="create_project", project="graph-mind")
                await call(action="publish", project="graph-mind", type="decision",
                           summary="Postgres 시작 실패 시 최대 10회 재시도",
                           details="sharing._retry; initdb·pg_ctl start. 남은 일: 원인 확정")
                await call(action="attach", project="graph-mind",
                           path="https://lab241.slack.com/archives/C1/p1", summary="제안서 v1 완료")
                asked = (await call(action="invite", member="minji", role="owner",
                                    projects=["graph-mind"])).structured_content
                self.assertEqual(asked["command"], 'turnbook team invite "minji" --role owner'
                                                   ' --project "graph-mind"')    # not from chat
                self.assertNotIn("minji", [a["member"] for a in pl.accounts()])
                self.assertEqual((await call(action="host")).structured_content["status"],
                                 "RUN_IN_TERMINAL")
                async with Client(build_server(self.root / "a.sqlite", terminal=True)) as typed:
                    with mock.patch.object(sharing, "_firewall", return_value="open"):
                        invited = (await typed.call_tool("brain_checkpoint", {
                            "action": "invite", "member": "minji", "role": "editor",
                            "projects": ["graph-mind"]})).structured_content
                self.assertIn("team_code", invited, invited)
                code = invited["team_code"]
                proposed = (await call(action="set_instructions", project="graph-mind",
                                       instructions="커밋 전에 테스트")).structured_content
                self.assertEqual(proposed["command"], 'turnbook team instructions "graph-mind"')
                self.assertEqual(current_instructions(), "")
                from turnbook import mcp_server
                with mock.patch.object(mcp_server, "configured_db_path",
                                       return_value=self.root / "a.sqlite"), \
                        mock.patch("builtins.input", return_value="n"):
                    self.assertEqual(await asyncio.to_thread(
                        mcp_server.terminal_command, ["team", "instructions", "graph-mind"]), 1)
                self.assertEqual(current_instructions(), "")
                with mock.patch.object(mcp_server, "configured_db_path",
                                       return_value=self.root / "a.sqlite"), \
                        mock.patch("builtins.input", return_value="y"):
                    await asyncio.to_thread(
                        mcp_server.terminal_command, ["team", "instructions", "graph-mind"])
                self.assertEqual(current_instructions(), "커밋 전에 테스트")
                refused = (await call(action="remove_member", member="minji")).structured_content
                self.assertEqual(refused["status"], "REFUSED")    # never the whole account
                self.assertTrue(pl.accounts())
            os.environ["TURNBOOK_CONFIG"] = str(self.root / "minji-config.json")
            async with Client(build_server(self.root / "b.sqlite")) as b:
                call = lambda **k: b.call_tool("brain_checkpoint", k)  # noqa: E731
                joined = (await call(action="join_team", code=code, name="minji")
                          ).structured_content
                where = (await call(action="timeline", project="graph-mind")).structured_content
            return joined, where
        joined, where = asyncio.run(run())
        self.assertEqual(joined["projects"], ["graph-mind"])
        summaries = [c["summary"] for c in where["checkpoints"]]
        self.assertEqual(summaries, ["제안서 v1 완료", "Postgres 시작 실패 시 최대 10회 재시도"])
        self.assertIn("남은 일", where["checkpoints"][1]["details"])
        self.assertEqual(where["checkpoints"][0]["artifacts"][0]["url"],
                         "https://lab241.slack.com/archives/C1/p1")

    def test_people_choose_the_names_others_see(self):
        """Joining with a name, renaming everywhere at once, one name per person per hub, and
        owners finding people by the name they chose."""
        owner = pl.own_ledger()
        owner.create_project("alpha", "yohan")
        owner.create_project("beta", "yohan")
        owner.db.close()
        with mock.patch.object(sharing, "_firewall", return_value="open"):
            minji = pl.invite("mini", "editor", ["alpha"], "yohan")["team_code"]
            jun = pl.add_account("jun")["team_code"]
        owner_config = os.environ["TURNBOOK_CONFIG"]

        os.environ["TURNBOOK_CONFIG"] = str(self.root / "minji.json")
        self.assertEqual(pl.join_team(minji, "김민지")["joined_as"], "김민지")
        self.assertEqual(sharing._config()["user_name"], "김민지")
        os.environ["TURNBOOK_CONFIG"] = str(self.root / "jun.json")
        with self.assertRaisesRegex(ValueError, "NAME_TAKEN"):
            pl.join_team(jun, "김민지")
        self.assertEqual(sharing._config().get("team_hubs", []), [])   # not half joined

        os.environ["TURNBOOK_CONFIG"] = owner_config
        with self.assertRaisesRegex(ValueError, "NAME_TAKEN"):
            pl.add_account("김민지")             # a second 김민지 would make "add 김민지" a guess
        with pl._admin() as hub:                # a hub from before names were unique
            hub.execute("DROP INDEX ledger_accounts_member")
            hub.execute("INSERT INTO ledger_accounts VALUES('gm_m_old_abcdef', '김민지')")
        pl._PREPARED.clear()
        pl.prepare_hub()
        self.assertTrue({"김민지", "김민지 (abcdef)"} <= {a["member"] for a in pl.accounts()})
        owner = pl.own_ledger()
        owner.grant("beta", "김민지", "viewer", "yohan")                  # by her chosen name
        self.assertEqual({m["member"]: m["role"] for m in owner.members("beta")},
                         {"yohan": "owner", "김민지": "viewer"})
        owner.db.close()
        self.assertEqual(pl.set_display_name("고요한")["renamed_on"], ["your project hub"])
        owner = pl.own_ledger()
        self.assertEqual(owner.by, "고요한")
        self.assertIn({"member": "고요한", "role": "owner"}, owner.members("alpha"))
        owner.db.close()

    def test_projects_from_one_shared_set_of_tables_become_brains_of_their_own(self):
        """A hub from before: every project in one set of tables beside personal memory, all
        sealed with one hub key. Each moves into its own schema under its own key, with its
        members and history, and reads back the same."""
        import json
        sharing._save({"share_password": "pw-before"})
        old_key = pl._fernet(pl.ledger_secret("pw-before"))
        sharing.local_postgres()
        checkpoint = {"id": "cp-0123456789ab", "project": "legacy", "type": "artifact",
                      "summary": "옛 결과물", "details": "", "actor": "yohan",
                      "created_at": "2026-10-01T10:00:00+09:00", "artifacts": [],
                      "source_refs": [], "supersedes": None, "context": {}}
        with pl._admin("graphmind") as old:
            old.execute("CREATE TABLE ledger_projects(name TEXT PRIMARY KEY, created_by TEXT, "
                        "created_at TEXT, instructions TEXT NOT NULL DEFAULT '')")
            old.execute("CREATE TABLE ledger_checkpoints(id TEXT PRIMARY KEY, project TEXT, "
                        "stamp TEXT, created_at TEXT, status TEXT, actor_role TEXT, payload TEXT)")
            old.execute("CREATE TABLE ledger_access(member_role TEXT, project TEXT, role TEXT, "
                        "member TEXT)")
            old.execute("CREATE TABLE ledger_audit(stamp TEXT, at TEXT, actor_role TEXT, "
                        "actor TEXT, action TEXT, target TEXT)")
            old.execute("INSERT INTO ledger_projects VALUES('legacy', 'yohan', '2026-10-01', '')")
            old.execute("INSERT INTO ledger_checkpoints VALUES(%s, 'legacy', '1', %s, "
                        "'published', 'graphmind', %s)",
                        (checkpoint["id"], checkpoint["created_at"],
                         old_key.encrypt(json.dumps(checkpoint).encode()).decode()))
            old.execute("INSERT INTO ledger_access VALUES('graphmind', 'legacy', 'owner', 'yohan')")
            old.execute("INSERT INTO ledger_audit VALUES('1', 'x', 'graphmind', 'yohan', "
                        "'publish', 'legacy/cp-0123456789ab')")
        pl._PREPARED.clear()
        pl.prepare_hub()

        book = pl.own_ledger()
        self.assertEqual(book.timeline("legacy")[0]["summary"], "옛 결과물")
        self.assertEqual(book.members("legacy"), [{"member": "yohan", "role": "owner"}])
        schema = book.db.execute("SELECT hub_schema('legacy')").fetchone()[0]
        book.db.close()
        with pl._admin() as hub:                                           # under its own key
            stored = hub.execute(f'SELECT payload FROM "{schema}".ledger_checkpoints').fetchone()[0]
        with self.assertRaises(Exception):
            old_key.decrypt(stored.encode())
        with pl._admin("graphmind") as old:
            self.assertIsNotNone(old.execute(
                "SELECT to_regclass('public.old_ledger_projects')").fetchone()[0])
            # made again after the move: kept beside the first, and preparing no longer fails
            old.execute("CREATE TABLE ledger_projects(name TEXT PRIMARY KEY, created_by TEXT, "
                        "created_at TEXT, instructions TEXT NOT NULL DEFAULT '')")
        pl._PREPARED.clear()
        pl.prepare_hub()
        with pl._admin("graphmind") as old:
            self.assertIsNotNone(old.execute(
                "SELECT to_regclass('public.old2_ledger_projects')").fetchone()[0])

    def test_the_operator_removes_a_project_whole(self):
        owner = pl.own_ledger()
        owner.create_project("alpha", "yohan")
        owner.publish("alpha", "decision", "알파 결정", "yohan")
        owner.db.close()
        with self.assertRaisesRegex(ValueError, "CONFIRM"):
            pl.delete_project("alpha", confirm="alph")
        self.assertEqual(pl.delete_project("alpha", confirm="alpha"), {"deleted": "alpha"})
        owner = pl.own_ledger()
        self.assertEqual(owner.names(), [])
        owner.db.close()


if __name__ == "__main__":
    unittest.main()
