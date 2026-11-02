import os

from turnbook import documents
from turnbook.context_packet import _conversation_item
from turnbook.conversation_memory import ConversationMemoryStore


def test_sections_follow_headings():
    text = "intro\n# Login\nemail\n## Kakao\nkakao too\n```\n# not a heading\n```\n# Pay\ncard"
    assert documents.sections(text) == [
        ("", "intro"), ("Login", "email"),
        ("Login > Kakao", "kakao too\n```\n# not a heading\n```"), ("Pay", "card")]


def test_edits_keep_the_old_text_marked(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "WALK_SECONDS", 0)
    folder = tmp_path / "plan"
    folder.mkdir()
    spec = folder / "spec.md"
    spec.write_text("# Login\nUsers sign in with email.\n# Pay\nCard only.", encoding="utf-8")
    (folder / "node_modules").mkdir()
    (folder / "node_modules" / "x.md").write_text("# Skip\nme", encoding="utf-8")
    times = iter(f"2026-10-{d:02d}T09:00:00" for d in range(1, 30))
    with ConversationMemoryStore(tmp_path / "m.sqlite") as store:
        capture = documents.DocumentCapture([folder], store, state_path=tmp_path / "s.json",
                                            now=lambda: next(times))
        assert capture.scan()["recorded_turns"] == 2
        assert capture.scan()["recorded_turns"] == 0              # unchanged: nothing new
        spec.write_text("# Login\nUsers sign in with Kakao.\n# Pay\nCard only.", encoding="utf-8")
        os.utime(spec, (1, 2_000_000_000))
        result = capture.scan()
        assert result["recorded_turns"] == 1 and result["replaced_sections"] == 1
        hits = store.search("sign in", limit=10)["turns"]
        assert {h["content"].splitlines()[-1] for h in hits} == {
            "Users sign in with Kakao.", "Users sign in with email."}
        old = documents.replaced_at(store.db, [h["turn_id"] for h in hits])
        assert len(old) == 1
        stale = next(h for h in hits if h["turn_id"] in old)
        assert "email" in stale["content"] and stale["content"].startswith("plan/spec.md > Login")
        item = _conversation_item({**stale, "replaced_at": old[stale["turn_id"]]})
        assert item["title"].startswith("document section, replaced on 2026-10-")
        spec.unlink()
        assert capture.scan()["replaced_sections"] == 2           # deleted: all of it replaced


def test_a_locked_file_does_not_stop_the_others(tmp_path, monkeypatch):
    """One unreadable file (locked, or a cloud file not downloaded) stopped every document
    after it, every minute; it is now tried again next time."""
    monkeypatch.setattr(documents, "WALK_SECONDS", 0)
    folder = tmp_path / "notes"
    folder.mkdir()
    for name in ("a.md", "b.md", "c.md"):
        (folder / name).write_text(f"# {name}\ntext of {name}", encoding="utf-8")
    real = type(folder).read_bytes

    def locked(path):
        if path.name == "b.md":
            raise PermissionError("locked")
        return real(path)
    with ConversationMemoryStore(tmp_path / "m.sqlite") as store:
        capture = documents.DocumentCapture([folder], store, state_path=tmp_path / "s.json",
                                            now=lambda: "2026-10-30T09:00:00")
        monkeypatch.setattr(type(folder), "read_bytes", locked)
        assert capture.scan()["recorded_turns"] == 2
        monkeypatch.setattr(type(folder), "read_bytes", real)
        assert capture.scan()["recorded_turns"] == 1


def test_a_file_in_the_pcs_own_encoding_keeps_its_letters(tmp_path, monkeypatch):
    """Notepad's ANSI on a Korean Windows is CP949: read as UTF-8 every letter became U+FFFD."""
    monkeypatch.setattr(documents, "WALK_SECONDS", 0)
    monkeypatch.setattr("locale.getpreferredencoding", lambda *_: "cp949")
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "meeting.txt").write_bytes("# 회의\n점심은 파니니".encode("cp949"))
    (folder / "plan.md").write_bytes("﻿# Plan\nship Friday".encode("utf-8"))
    with ConversationMemoryStore(tmp_path / "m.sqlite") as store:
        documents.DocumentCapture([folder], store, state_path=tmp_path / "s.json",
                                  now=lambda: "2026-10-30T09:00:00").scan()
        texts = sorted(t["content"] for t in store.latest(limit=10)["turns"])
    assert texts == ["notes/meeting.txt > 회의\n\n점심은 파니니",
                     "notes/plan.md > Plan\n\nship Friday"]
