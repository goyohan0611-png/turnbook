import json
from datetime import datetime, timedelta

from turnbook import automatic_capture, mcp_server


def test_stopped_capture_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(automatic_capture, "default_service_state_dir", lambda: tmp_path)
    assert mcp_server.capture_warnings() == []          # capture never installed: nothing to say
    done = datetime(2026, 10, 29, 9, 0)
    (tmp_path / "status.json").write_text(json.dumps(
        {"completed_at": done.isoformat(), "warnings": ["codex: format changed"]}))
    assert mcp_server.capture_warnings(now=done + timedelta(minutes=1)) == [
        "codex: format changed"]
    late = mcp_server.capture_warnings(now=done + timedelta(hours=2))
    assert len(late) == 2 and "has not run since 2026-10-29 09:00" in late[1]
