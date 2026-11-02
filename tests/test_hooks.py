"""Gemini CLI: its hook payloads (as documented) are saved through turnbook.hook and stored
once; the installer adds our hooks next to the user's own and takes only ours away again.
Copilot CLI and Cursor: their own transcripts are read, in the shape they were seen to write."""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
from unittest import mock
import io
import json
import os
import tempfile
import unittest

from turnbook import hook, install
from turnbook.conversation_memory import (ConversationMemoryStore, CopilotConversationCapture,
                                            CursorConversationCapture, HookConversationCapture)


def run(agent, event, payload, *extra):
    out = io.StringIO()
    out.reconfigure = lambda **_: None
    stdin = io.StringIO(json.dumps(payload))
    stdin.reconfigure = lambda **_: None
    with mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", out):
        hook.main([agent, event, *extra])
    return json.loads(out.getvalue())


class HookTests(unittest.TestCase):
    def test_gemini_cli_is_saved_once(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"TURNBOOK_HOME": directory}):
            run("gemini-cli", "BeforeAgent", {"session_id": "g1", "cwd": directory,
                                              "hook_event_name": "BeforeAgent",
                                              "prompt": "our golden retriever Kongi turned three"})
            for _ in range(2):                     # fired again for the same reply: kept once
                run("gemini-cli", "AfterAgent", {"session_id": "g1", "cwd": directory,
                                                 "prompt": "x",
                                                 "prompt_response": "Happy birthday!"})
            self.assertEqual(run("gemini-cli", "AfterAgent", {"prompt_response": ""}), {})
            rows = []
            with ConversationMemoryStore(Path(directory) / "m.sqlite") as store:
                for folder in sorted(hook.hooks_root().iterdir()):
                    capture = HookConversationCapture(
                        folder.name, folder, store, state_path=Path(directory) / folder.name)
                    capture.initialize_baseline()
                    capture.scan()
                    capture.scan()                               # nothing new: nothing twice
                rows = store.db.execute("SELECT client, role, content_redacted FROM "
                                        "conversation_turns ORDER BY client, happened_at")
                rows = sorted(tuple(r) for r in rows.fetchall())
        self.assertEqual(rows, sorted([
            ("gemini-cli", "user", "our golden retriever Kongi turned three"),
            ("gemini-cli", "assistant", "Happy birthday!")]))

    def test_the_conversation_about_deleting_is_not_kept(self):
        """docs/privacy.md: asking to delete a secret must not store the secret again. Gemini's
        hooks carried only text, so the forget call was invisible and the request was kept."""
        # a second apart, as separate hook processes are; within one millisecond, lines tied
        # on their time and the order (and so what the forget swept) was left to chance
        seconds = iter(range(100))
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"TURNBOOK_HOME": directory}), \
                mock.patch.object(hook, "_now",
                                  lambda: f"2026-10-19T00:00:{next(seconds):02d}.000000Z"):
            def say(event, **fields):
                run("gemini-cli", event, {"session_id": "g3", **fields})
            say("BeforeAgent", prompt="my dog is called Kongi")
            say("AfterAgent", prompt_response="Nice name!")
            say("BeforeAgent", prompt="delete the wifi password qwerty123 I typed earlier")
            say("AfterTool", tool_name="mcp_graph-mind-memory_brain_view")     # not a forget
            say("AfterTool", tool_name="mcp_graph-mind-memory_brain_forget")
            say("AfterAgent", prompt_response="Deleted it.")
            say("BeforeAgent", prompt="thanks, what's my dog called?")
            with ConversationMemoryStore(Path(directory) / "m.sqlite") as store:
                capture = HookConversationCapture(
                    "gemini-cli", hook.hooks_root() / "gemini-cli", store,
                    state_path=Path(directory) / "c.json")       # the real erase
                capture.initialize_baseline()
                capture.scan()
                kept = [r[0] for r in store.db.execute(
                    "SELECT content_redacted FROM conversation_turns ORDER BY happened_at")]
        self.assertEqual(kept, ["my dog is called Kongi", "Nice name!",
                                "thanks, what's my dog called?"])

    def test_cursor_transcript_is_read_as_said(self):
        """As the Cursor CLI agent wrote it: the typed text inside <user_query>, the reply's
        text blocks; a brain_forget call through CallDynamicTool drops the exchange."""
        with tempfile.TemporaryDirectory() as directory:
            chat = Path(directory) / "projects" / "C-work" / "agent-transcripts" / "t1"
            chat.mkdir(parents=True)
            def say(role, *blocks):
                return {"role": role, "message": {"content": list(blocks)}}
            records = [
                say("user", {"type": "text",
                             "text": "<timestamp>Sunday, Oct 18, 2026, 2:46 PM (UTC+9)</timestamp>"
                             "\n<user_query>\nI moved to Busan\n</user_query>"}),
                say("assistant", {"type": "text", "text": "Saving that."},
                    {"type": "tool_use", "name": "CallDynamicTool",
                     "input": {"namespace": "graph-mind-memory", "toolName": "brain_remember"}}),
                say("assistant", {"type": "text", "text": "Busan is lovely."}),
                {"type": "turn_ended", "status": "success"}]
            (chat / "t1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records),
                                           encoding="utf-8")
            with ConversationMemoryStore(Path(directory) / "m.sqlite") as store:
                capture = CursorConversationCapture(Path(directory) / "projects", store,
                                                    state_path=Path(directory) / "c.json")
                capture._write_state({})
                capture.scan()
                capture.scan()
                rows = store.db.execute("SELECT client, client_session_id, role, "
                                        "content_redacted FROM conversation_turns "
                                        "ORDER BY rowid").fetchall()
                said = {r[0] for r in store.db.execute(
                    "SELECT happened_at FROM conversation_turns")}
                self.assertFalse(capture._forget_call(records[1]))
                self.assertTrue(capture._forget_call(say("assistant", {
                    "type": "tool_use", "name": "CallDynamicTool",
                    "input": {"toolName": "brain_forget"}})))
        self.assertEqual([tuple(r) for r in rows],
                         [("cursor", "t1", "user", "I moved to Busan"),
                          ("cursor", "t1", "assistant", "Saving that."),
                          ("cursor", "t1", "assistant", "Busan is lovely.")])
        # when it was said, from the user's <timestamp>, and the replies with it (not scan time)
        self.assertEqual(len(said), 1)
        self.assertTrue(next(iter(said)).startswith("2026-10-18"), said)

    def test_hook_command_survives_a_space_and_a_shadowing_project(self):
        """Gemini CLI runs hooks through PowerShell on Windows, where a quoted path alone is a
        string; and apps start them in the user's project, where a turnbook.py shadowed us."""
        python = r"C:\Users\Jane Doe\py\python.exe"
        with mock.patch.object(install.sys, "executable", python), \
                mock.patch.object(install.os, "name", "nt"):
            self.assertEqual(install._hook_command("gemini-cli", "BeforeAgent"),
                             f"& '{python}' -m turnbook.hook gemini-cli BeforeAgent")
        # single quotes: in double quotes PowerShell expanded "$Doe" and ran "$(...)"
        with mock.patch.object(install.sys, "executable", r"C:\Users\O'Neil $(calc)\python.exe"), \
                mock.patch.object(install.os, "name", "nt"):
            self.assertTrue(install._hook_command("gemini-cli", "BeforeAgent").startswith(
                r"& 'C:\Users\O''Neil $(calc)\python.exe' "))
        entry = install._hook_files(Path("x"))[0][3]["BeforeAgent"]["hooks"][0]
        self.assertEqual(entry["env"]["PYTHONSAFEPATH"], "1")
        self.assertEqual(install.ENV["PYTHONSAFEPATH"], "1")

    def test_apps_saved_for_them_are_told_so(self):
        """Their conversations are captured, so the model is not asked to save them again."""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for name in (".gemini", ".copilot", ".lmstudio", ".cursor"):
                (home / name).mkdir()
            environment = {"USERPROFILE": directory, "HOME": directory,
                           "APPDATA": str(home / "AppData")}
            with mock.patch.dict(os.environ, environment):
                install.agent_hooks(home)            # Gemini counts as saved only once hooked
                install.other_apps(home)
            def env(*parts):
                return json.loads(home.joinpath(*parts).read_text(encoding="utf-8"))[
                    "mcpServers"]["turnbook"]["env"]
            self.assertEqual(env(".gemini", "settings.json").get("TURNBOOK_CAPTURED"), "1")
            self.assertEqual(env(".copilot", "mcp-config.json").get("TURNBOOK_CAPTURED"), "1")
            self.assertNotIn("TURNBOOK_CAPTURED", env(".lmstudio", "mcp.json"))
            self.assertNotIn("TURNBOOK_CAPTURED", env(".cursor", "mcp.json"))   # editor: unseen
            # Gemini's hooks break (an unexpected "hooks" value): nothing saves its chats now,
            # so the flag that told the model not to save them goes, even if set before
            settings = home / ".gemini" / "settings.json"
            broken = json.loads(settings.read_text(encoding="utf-8"))
            broken["hooks"] = ["not", "an", "object"]
            settings.write_text(json.dumps(broken), encoding="utf-8")
            with mock.patch.dict(os.environ, environment):
                self.assertNotIn("Gemini CLI", install.agent_hooks(home))
                install.other_apps(home)
            self.assertNotIn("TURNBOOK_CAPTURED", env(".gemini", "settings.json"))

    def test_copilot_cli_session_is_read_as_said(self):
        """Records as Copilot CLI 1.0.98 writes them: what was typed (not transformedContent),
        the reply's text, nothing from hooks or a reply that only calls a tool."""
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "state" / "s1"
            session.mkdir(parents=True)
            at = "2026-10-18T05:38:11.000Z"
            records = [
                {"type": "session.start", "id": "e0", "timestamp": at,
                 "data": {"sessionId": "s1", "context": {"cwd": directory}}},
                {"type": "hook.start", "id": "e1", "timestamp": at,
                 "data": {"input": {"prompt": "my sister is Mina"}}},
                {"type": "user.message", "id": "e2", "timestamp": at,
                 "data": {"content": "my sister is Mina",
                          "transformedContent": "<current_datetime>...</current_datetime>"}},
                {"type": "assistant.message", "id": "e3", "timestamp": at,
                 "data": {"content": "", "toolRequests": [{"name": "view"}]}},
                {"type": "assistant.message", "id": "e4", "timestamp": at,
                 "data": {"content": "Nice to know about Mina.", "toolRequests": []}}]
            (session / "events.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            with ConversationMemoryStore(Path(directory) / "m.sqlite") as store:
                capture = CopilotConversationCapture(Path(directory) / "state", store,
                                                     state_path=Path(directory) / "c.json")
                capture._write_state({})                  # read from the start
                capture.scan()
                capture.scan()
                rows = store.db.execute("SELECT client, client_session_id, role, "
                                        "content_redacted FROM conversation_turns "
                                        "ORDER BY rowid").fetchall()
        self.assertEqual([tuple(r) for r in rows],
                         [("copilot-cli", "s1", "user", "my sister is Mina"),
                          ("copilot-cli", "s1", "assistant", "Nice to know about Mina.")])

    def test_the_memory_a_hook_added_is_not_stored_as_the_users_words(self):
        """Copilot CLI keeps the prompt our hook extended as the user's message: stored again,
        each packet would come back as something the user said."""
        from turnbook.inject import HEADER
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "state" / "s1"
            session.mkdir(parents=True)
            at = "2026-10-30T05:38:11.000Z"
            records = [
                {"type": "session.start", "id": "e0", "timestamp": at,
                 "data": {"sessionId": "s1", "context": {"cwd": directory}}},
                {"type": "user.message", "id": "e1", "timestamp": at,
                 "data": {"content": "what did I eat?\n\n" + HEADER + "Now: 2026-10-30 14:38\n\n"
                                     "[CONVERSATION_TURN | ...] I had a panini"}}]
            (session / "events.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            with ConversationMemoryStore(Path(directory) / "m.sqlite") as store:
                capture = CopilotConversationCapture(Path(directory) / "state", store,
                                                     state_path=Path(directory) / "c.json")
                capture._write_state({})
                capture.scan()
                rows = store.db.execute("SELECT content_redacted FROM conversation_turns"
                                        ).fetchall()
        self.assertEqual([r[0] for r in rows], ["what did I eat?"])

    def test_gemini_gets_the_memory_before_it_answers(self):
        with mock.patch("turnbook.inject.context", return_value="Turnbook memory: Kongi"):
            answer = run("gemini-cli", "BeforeAgent", {"session_id": "g2", "prompt": "my dog?"})
        self.assertEqual(answer["hookSpecificOutput"]["additionalContext"],
                         "Turnbook memory: Kongi")

    def test_installer_adds_ours_beside_the_users_and_removes_only_ours(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".gemini").mkdir()
            theirs = {"hooks": [{"type": "command", "command": "C:/their/hook.exe"}]}
            (home / ".gemini" / "settings.json").write_text(json.dumps(
                {"theme": "Dracula", "hooks": {"BeforeAgent": [theirs], "enabled": True}}),
                encoding="utf-8")                # "enabled" stopped the whole install once
            for _ in range(2):                                   # again: still one of ours
                self.assertEqual(install.agent_hooks(home), ["Gemini CLI"])

            def read():
                return json.loads((home / ".gemini" / "settings.json").read_text(encoding="utf-8"))
            gemini = read()
            self.assertEqual(gemini["theme"], "Dracula")
            self.assertEqual(gemini["hooks"]["BeforeAgent"][0], theirs)
            self.assertEqual(len(gemini["hooks"]["BeforeAgent"]), 2)
            self.assertIn("turnbook.hook gemini-cli AfterAgent",
                          gemini["hooks"]["AfterAgent"][0]["hooks"][0]["command"])
            install.agent_hooks(home, remove=True)
            self.assertEqual(read()["hooks"], {"BeforeAgent": [theirs], "enabled": True})

    def test_agents_with_transcripts_get_only_the_memory(self):
        """Claude Code, Codex, Copilot CLI: their hook adds the memory for the message in each
        one's shape, and saves nothing (their transcripts are read already)."""
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"TURNBOOK_HOME": directory}), \
                mock.patch("turnbook.inject.context", lambda text: "MEMORY for " + text):
            prompt = {"session_id": "c1", "prompt": "where did we leave off?"}
            for agent in ("claude-code", "codex"):
                self.assertEqual(run(agent, "UserPromptSubmit", prompt), {"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "MEMORY for where did we leave off?"}})
            self.assertEqual(run("copilot-cli", "userPromptTransformed",
                                 {**prompt, "transformedPrompt": "<date/> where?"}),
                             {"modifiedTransformedPrompt":
                              "<date/> where?\n\nMEMORY for where did we leave off?"})
            self.assertFalse(hook.hooks_root().exists())                # nothing saved
        with mock.patch("turnbook.inject.context", lambda text: ""):
            self.assertEqual(run("codex", "UserPromptSubmit", {"prompt": "hi"}), {})

    def test_the_plugin_hook_steps_aside_for_the_installers(self):
        """The Claude Code plugin's hook adds the memory, unless turnbook-install's hook is in
        ~/.claude/settings.json too: then once is enough."""
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(Path, "home", lambda: Path(directory)), \
                mock.patch("turnbook.inject.context", lambda text: "MEMORY"):
            prompt = {"prompt": "where?"}
            self.assertIn("hookSpecificOutput",
                          run("claude-code", "UserPromptSubmit", prompt, "plugin"))
            (Path(directory) / ".claude").mkdir()
            (Path(directory) / ".claude" / "settings.json").write_text(
                '{"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": '
                '"py -m turnbook.hook claude-code UserPromptSubmit"}]}]}}', encoding="utf-8")
            self.assertEqual(run("claude-code", "UserPromptSubmit", prompt, "plugin"), {})
            self.assertIn("hookSpecificOutput", run("claude-code", "UserPromptSubmit", prompt))
            # an install from before SessionStart: the plugin's session hook still runs
            with mock.patch("turnbook.hook.left_off", lambda event: "LEFT OFF"):
                self.assertEqual(run("claude-code", "SessionStart", {"source": "startup"},
                                     "plugin")["hookSpecificOutput"]["additionalContext"],
                                 "LEFT OFF")

    def test_a_new_session_starts_where_the_last_one_in_the_folder_ended(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(database) as store:
                for n, (session, cwd, role, text) in enumerate([
                        ("old", "C:/work", "user", "first session"),
                        ("prev", "C:/work", "user", "fix the login bug"),
                        ("prev", "C:/work", "assistant", "Did:\nedited login.py"),
                        ("prev", "C:/elsewhere", "user", "another folder"),
                        ("now", "C:/work", "user", "this session")]):
                    store.record({"turn_id": f"t{n}", "client": "claude-code",
                                  "client_session_id": session, "scope": "global", "cwd": cwd,
                                  "role": role, "content_redacted": text, "raw_sha256": f"h{n}",
                                  "source_path": session, "source_ordinal": n,
                                  "happened_at": f"2026-11-0{n + 1}T10:00:00",
                                  "known_at": f"2026-11-0{n + 1}T10:00:00"})
            with mock.patch("turnbook.mcp_server.configured_db_path", lambda: database):
                text = hook.left_off({"cwd": "C:/work", "session_id": "now",
                                      "source": "startup"})
                self.assertTrue(text.endswith("[user] fix the login bug\n"
                                              "[assistant] Did: edited login.py"), text)
                self.assertNotIn("first session", text)
                self.assertEqual(hook.left_off({"cwd": "C:/work", "session_id": "now",
                                                "source": "resume"}), "")
                self.assertEqual(hook.left_off({"cwd": "C:/new", "session_id": "now"}), "")

    def test_the_message_hook_does_not_wait_on_the_shared_hub(self):
        """The memory servers pull other devices' lines; a hub connection per message added
        0.8-5 s before every answer."""
        from turnbook import inject
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"TURNBOOK_HOME": directory}), \
                mock.patch("turnbook.mcp_server.shared_sync",
                           side_effect=AssertionError("hub synced per message")):
            inject.context("where did we leave off on the launch?")

    def test_earlier_messages_with_the_same_words_are_left_out(self):
        """"했어" brought back four older "했어" and nothing else, in every packet."""
        from turnbook import inject

        def turn(n, text):
            return {"turn_id": f"t{n}", "role": "user", "scope": "global", "content": text,
                    "happened_at": f"2026-10-0{n}T10:00:00", "client": "codex",
                    "source_path": "p", "client_session_id": "s", "source_ordinal": n}
        echoes = {"matches": [], "conversation_turns": [turn(1, "했어"), turn(2, " 했어\n")]}
        useful = {"matches": [], "conversation_turns": [turn(3, "했어"),
                                                        turn(4, "배포는 금요일에 했어")]}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"TURNBOOK_HOME": directory}), \
                mock.patch("turnbook.inject.from_service", return_value=None), \
                mock.patch("turnbook.mcp_server.embedder_for"):
            with mock.patch("turnbook.mcp_server.gather", return_value=echoes):
                self.assertEqual(inject.context("했어"), "")
            with mock.patch("turnbook.mcp_server.gather", return_value=useful):
                packet = inject.context("했어")
        self.assertIn("배포는 금요일에 했어", packet)
        self.assertEqual(packet.count("conversation turn"), 1)

    def test_the_hook_asks_the_capture_service_first(self):
        """Its index is warm: a fresh process per message took 1-2 s to import and read it.
        An older service (no "context" in embedder.json) leaves the hook to search itself."""
        from turnbook import inject, local_embedder
        lender = mock.Mock(model_name=local_embedder.LOCAL_MODEL)
        message = "where did we leave off on the launch?"
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"TURNBOOK_HOME": directory}):
            local_embedder.serve(lender)
            self.assertIsNone(inject.from_service(message))
            local_embedder.serve(lender, context=lambda m: "SERVED " + m)
            with mock.patch("turnbook.mcp_server.gather",
                            side_effect=AssertionError("searched in the hook's process")):
                self.assertEqual(inject.context(message), "SERVED " + message)

    def test_opencodes_plugin_gets_the_same_deadline(self):
        """python -m turnbook.inject waited for the search however long it took (the plugin
        gave it 15 s)."""
        import time
        from turnbook import inject
        out = io.StringIO()
        with mock.patch.object(hook, "DEADLINE", 0.2), \
                mock.patch.object(inject, "context", lambda text: time.sleep(2) or "late"), \
                mock.patch("sys.stdin", io.StringIO("where were we?")), \
                mock.patch("sys.stdout", out), mock.patch("os._exit") as leave:
            began = time.monotonic()
            inject.main()
            self.assertLess(time.monotonic() - began, 1.0)
        self.assertEqual(out.getvalue(), "")
        leave.assert_called_once_with(0)
        hook._abandoned.clear()

    def test_every_message_is_searched_with_no_keyword_needed(self):
        """Whether a message needs the past is the model's call: a keyword list missed
        "오늘 점심 뭐 먹었지?". Only an empty message is skipped."""
        from turnbook import inject
        asked = []
        with mock.patch("turnbook.inject.from_service", lambda q: asked.append(q) or "M"):
            self.assertEqual(inject.context("오늘 점심 뭐 먹었지?"), "M")
            self.assertEqual(inject.context("   "), "")
        self.assertEqual(asked, ["오늘 점심 뭐 먹었지?"])

    def test_a_pasted_log_is_searched_by_its_two_ends(self):
        """The question sits at the start or end; the middle only slowed the search."""
        from turnbook import inject
        asked = []
        message = "where did we leave off? " + "Traceback line " * 400 + " same error as last time?"
        with mock.patch("turnbook.inject.from_service", lambda q: asked.append(q) or "M"):
            self.assertEqual(inject.context(message), "M")
        self.assertLessEqual(len(asked[0]), 2 * inject.ENDS + 1)
        self.assertTrue(asked[0].startswith("where did we leave off?"))
        self.assertTrue(asked[0].endswith("same error as last time?"))

    def test_a_slow_search_never_holds_the_message(self):
        """Service down, model still loading: the message goes on without memory after
        DEADLINE seconds (it waited 7-22 s)."""
        import time
        with mock.patch.object(hook, "DEADLINE", 0.2), \
                mock.patch("turnbook.inject.context", lambda text: time.sleep(2) or "late"):
            began = time.monotonic()
            self.assertEqual(hook.reply("codex", "UserPromptSubmit", "where were we?"), {})
            self.assertLess(time.monotonic() - began, 1.0)
        hook._abandoned.clear()

    def test_installer_adds_the_memory_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for name in (".claude", ".codex", ".copilot"):
                (home / name).mkdir()
            theirs = {"hooks": [{"type": "command", "command": "C:/their/hook.exe"}]}
            (home / ".claude" / "settings.json").write_text(json.dumps(
                {"model": "opus", "hooks": {"UserPromptSubmit": [theirs]}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"COPILOT_HOME": ""}):
                self.assertEqual(sorted(install.agent_hooks(home)),
                                 ["Claude Code", "Codex", "Copilot CLI"])
            claude = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(claude["model"], "opus")
            self.assertEqual(claude["hooks"]["UserPromptSubmit"][0], theirs)
            self.assertIn("turnbook.hook claude-code UserPromptSubmit",
                          claude["hooks"]["UserPromptSubmit"][1]["hooks"][0]["command"])
            codex = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
            self.assertIn("turnbook.hook codex UserPromptSubmit",
                          codex["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"])
            for settings, agent in ((claude, "claude-code"), (codex, "codex")):
                self.assertIn(f"turnbook.hook {agent} SessionStart",
                              settings["hooks"]["SessionStart"][0]["hooks"][0]["command"])
            copilot = json.loads((home / ".copilot" / "hooks" / "turnbook.json")
                                 .read_text(encoding="utf-8"))
            self.assertEqual(copilot["version"], 1)
            self.assertIn("copilot-cli", json.dumps(copilot["hooks"]["userPromptTransformed"]))
            install.agent_hooks(home, remove=True)
            claude = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(claude["hooks"], {"UserPromptSubmit": [theirs]})


if __name__ == "__main__":
    unittest.main()
