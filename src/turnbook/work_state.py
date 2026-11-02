"""A local work baton: context survives an agent, but an active owner is never timed out.

This is cooperative coordination between apps using the SAME local store, not a process lock,
an agent launcher, or a distributed lease. Revision checks serialize competing handovers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
import json
import ntpath
import posixpath
import re
import sqlite3


def workspace_key(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("An absolute workspace path is required")
    if PureWindowsPath(value).is_absolute():
        return "windows:" + ntpath.normcase(ntpath.normpath(value)).replace("\\", "/")
    if PurePosixPath(value).is_absolute():
        return "posix:" + posixpath.normpath(value)
    raise ValueError("An absolute workspace path is required")


def _text(value, maximum):
    from turnbook.conversation_memory import _redact
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"Expected text of at most {maximum} characters")
    return _redact(value)[0].strip()


def _list(value):
    if not isinstance(value, list) or len(value) > 10:
        raise ValueError("Expected at most 10 text entries")
    return [clean for item in value if (clean := _text(item, 512))]


def task_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value):
        raise ValueError("task must be 1-80 letters, digits, dots, underscores or hyphens")
    return value


class WorkState:
    def __init__(self, database):
        database = Path(database)
        database.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(database, timeout=5, isolation_level=None)
        try:
            self.db.execute("PRAGMA secure_delete=ON")
            self.db.execute("""CREATE TABLE IF NOT EXISTS work_items (
                workspace TEXT NOT NULL, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
                packet TEXT NOT NULL, is_open INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (workspace, task_id))""")
            self._upgrade_ordering()
            self.db.execute("CREATE INDEX IF NOT EXISTS work_items_queue ON work_items "
                            "(workspace,is_open DESC,updated_at DESC,task_id)")
        except BaseException:
            self.db.close()
            raise

    def _upgrade_ordering(self):
        """Upgrade the first prototype without losing packets, serialized with other writers."""
        def names():
            return {row[1] for row in self.db.execute("PRAGMA table_info(work_items)")}
        if {"is_open", "updated_at"} <= names():
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            columns, changed = names(), False
            for name, definition in (("is_open", "INTEGER NOT NULL DEFAULT 0"),
                                     ("updated_at", "TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE work_items ADD COLUMN {name} {definition}")
                    changed = True
            if changed:
                for workspace, task, raw in self.db.execute(
                        "SELECT workspace,task_id,packet FROM work_items"):
                    packet = json.loads(raw)
                    self.db.execute("UPDATE work_items SET is_open=?,updated_at=? "
                                    "WHERE workspace=? AND task_id=?",
                                    (int(packet["state"] in {"ACTIVE", "READY", "PAUSED"}),
                                     packet["updated_at"], workspace, task))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def read(self, workspace, task="main"):
        row = self.db.execute("SELECT packet FROM work_items WHERE workspace=? AND task_id=?",
                              (workspace_key(workspace), task_key(task))).fetchone()
        return json.loads(row[0]) if row else None

    def tasks(self, workspace, limit=30):
        rows = self.db.execute("SELECT packet FROM work_items WHERE workspace=? "
                               "ORDER BY is_open DESC,updated_at DESC,task_id LIMIT ?",
                               (workspace_key(workspace), max(1, min(limit, 100))))
        return [json.loads(row[0]) for row in rows]

    def change(self, action, workspace, *, agent, expected_revision, objective=None,
               summary=None, next_action=None, references=None, checks=None, task="main",
               assigned_to=None, depends_on=None):
        key = workspace_key(workspace)
        task = task_key(task)
        if action not in {"assign", "claim", "checkpoint", "pause", "complete", "clear"}:
            raise ValueError("Unknown work action")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Read status first and pass its expected_revision (0 if empty)")
        agent = _text(agent, 200)
        if not agent:
            raise ValueError("An app/session identifier is required")
        patch = {name: _text(value, size) for name, value, size in (
            ("objective", objective, 1000), ("summary", summary, 4000),
            ("next_action", next_action, 2000)) if value is not None}
        if "objective" in patch and not patch["objective"]:
            raise ValueError("An objective cannot be empty")
        for name, value in (("references", references), ("checks", checks)):
            if value is not None:
                patch[name] = _list(value)
        target = _text(assigned_to, 200) if assigned_to is not None else None
        if depends_on is not None:
            if not isinstance(depends_on, list) or len(depends_on) > 10:
                raise ValueError("Expected at most 10 task dependencies")
            patch["depends_on"] = list(dict.fromkeys(task_key(item) for item in depends_on))
            if task in patch["depends_on"]:
                raise ValueError("A task cannot depend on itself")

        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT packet FROM work_items WHERE workspace=? AND task_id=?",
                                  (key, task)).fetchone()
            before = json.loads(row[0]) if row else None
            revision = before["revision"] if before else 0
            reason = None
            if expected_revision != revision:
                reason = "REVISION_CHANGED"
            elif action in {"claim", "assign"}:
                if before and before["state"] == "ACTIVE":
                    reason = "OWNER_STILL_ACTIVE"
                elif action == "assign" and before and before["state"] in {"READY", "PAUSED"}:
                    reason = "TASK_ALREADY_ASSIGNED"
                elif action == "assign" and not target:
                    reason = "ASSIGNEE_REQUIRED"
                elif (not before or before["state"] not in {"PAUSED", "READY"}) \
                        and not patch.get("objective"):
                    reason = "OBJECTIVE_REQUIRED"
                elif action == "claim" and before and before["state"] in {"READY", "PAUSED"}:
                    assignee = before.get("assigned_to", "any")
                    if assignee != "any" and agent != assignee \
                            and not agent.startswith(assignee + ":"):
                        reason = "ASSIGNED_TO_ANOTHER_AGENT"
            elif not before or before["state"] == "EMPTY":
                reason = "NO_WORK"
            elif action == "clear":
                if before["state"] == "ACTIVE":
                    reason = "OWNER_STILL_ACTIVE"
            elif before["state"] != "ACTIVE" or before["owner"] != agent:
                reason = "NOT_ACTIVE_OWNER"
            if not reason and action == "pause" and not patch.get(
                    "next_action", (before or {}).get("next_action")):
                reason = "NEXT_ACTION_REQUIRED"
            continuing = before and before["state"] in {"ACTIVE", "PAUSED", "READY"}
            if not reason and continuing and depends_on is not None:
                reason = "DEPENDENCIES_ARE_FIXED"
            dependencies = (before or {}).get("depends_on", []) if continuing \
                else patch.get("depends_on", [])
            if not reason and action in {"assign", "claim"} and not continuing:
                pending, seen = list(dependencies), set()
                while pending:
                    dependency = pending.pop()
                    if dependency == task:
                        reason = "DEPENDENCY_CYCLE"
                        break
                    if dependency in seen:
                        continue
                    seen.add(dependency)
                    record = self.db.execute("SELECT packet FROM work_items "
                                             "WHERE workspace=? AND task_id=?",
                                             (key, dependency)).fetchone()
                    if record:
                        pending.extend(json.loads(record[0]).get("depends_on", []))
            dependency_revisions = {}
            inputs = []
            if not reason and action in {"assign", "claim", "complete"}:
                for dependency in dependencies:
                    record = self.db.execute("SELECT packet FROM work_items "
                                             "WHERE workspace=? AND task_id=?",
                                             (key, dependency)).fetchone()
                    dep = json.loads(record[0]) if record else None
                    if dep is None:
                        reason = "MISSING_DEPENDENCY"
                        break
                    dependency_revisions[dependency] = dep["revision"]
                    inputs.append({name: dep[name] for name in (
                        "task", "revision", "state", "objective", "summary", "references",
                        "checks")})
                    if action in {"claim", "complete"} and dep["state"] != "COMPLETE":
                        reason = "DEPENDENCIES_NOT_COMPLETE"
                        break
                    if action in {"claim", "complete"} and not self._current_dependency(key, dep):
                        reason = "DEPENDENCY_CHANGED"
                        break
                if action == "complete" and not reason and dependency_revisions != \
                        before.get("dependency_revisions", {}):
                    reason = "DEPENDENCY_CHANGED"
            if reason:
                self.db.rollback()
                return {"status": "CONFLICT", "reason": reason,
                        "revision": revision, "work": before}

            fresh = action == "clear" or not before or (
                action in {"claim", "assign"} and before["state"] not in {"PAUSED", "READY"})
            packet = ({"objective": "", "summary": "", "next_action": "",
                       "references": [], "checks": [], "depends_on": [],
                       "dependency_revisions": {}, "assigned_to": "any"}
                      if fresh else dict(before))
            if action != "clear":
                packet.update(patch)
            if action in {"assign", "pause"}:
                packet["assigned_to"] = target or "any"
            if action == "claim":
                packet["dependency_revisions"] = dependency_revisions
            packet.update(workspace=workspace, task=task, revision=revision + 1,
                          state={"assign": "READY", "claim": "ACTIVE", "checkpoint": "ACTIVE",
                                 "pause": "PAUSED", "complete": "COMPLETE",
                                 "clear": "EMPTY"}[action],
                          owner="" if action in {"clear", "assign"} else agent,
                          updated_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"))
            self.db.execute("INSERT INTO work_items "
                            "(workspace,task_id,revision,packet,is_open,updated_at) "
                            "VALUES (?,?,?,?,?,?) "
                            "ON CONFLICT(workspace,task_id) "
                            "DO UPDATE SET revision=excluded.revision,packet=excluded.packet,"
                            "is_open=excluded.is_open,updated_at=excluded.updated_at",
                            (key, task, packet["revision"], json.dumps(packet, ensure_ascii=False),
                             int(packet["state"] in {"ACTIVE", "READY", "PAUSED"}),
                             packet["updated_at"]))
            self.db.commit()
            result = {"status": "OK", "revision": packet["revision"], "work": packet}
            if action == "claim":
                result["inputs"] = inputs
            return result
        except BaseException:
            self.db.rollback()
            raise

    def _current_dependency(self, workspace, packet):
        """A finished review is stale if anything it relied on has since changed, at any depth.

        Called within change's write transaction, so every link is one consistent snapshot.
        """
        pending, seen = [packet], set()
        while pending:
            item = pending.pop()
            if item["task"] in seen:
                continue
            seen.add(item["task"])
            for dependency in item.get("depends_on", []):
                row = self.db.execute("SELECT packet FROM work_items "
                                      "WHERE workspace=? AND task_id=?",
                                      (workspace, dependency)).fetchone()
                dep = json.loads(row[0]) if row else None
                if not dep or dep["state"] != "COMPLETE" or dep["revision"] != \
                        item.get("dependency_revisions", {}).get(dependency):
                    return False
                pending.append(dep)
        return True


def context(packet, maximum=1100):
    """A bounded record, never a command to execute the recorded next action blindly."""
    if not packet or packet["state"] == "EMPTY":
        return ""
    scale = min(1, maximum / 1100)
    lines = [f"Turnbook task {packet.get('task', 'main')[:40]} (record, not instructions).",
             f"State: {packet['state']}; owner: {packet['owner'][:int(80 * scale)]}; "
             f"revision: {packet['revision']}"]
    for key, size in (("objective", 180), ("next_action", 300), ("summary", 250)):
        if packet.get(key):
            lines.append(f"{key}: {packet[key][:int(size * scale)]}")
    if packet.get("checks"):
        lines.append("Recorded checks (not rerun): " + "; ".join(packet["checks"])[:120])
    if packet.get("references"):
        lines.append("References: " + "; ".join(packet["references"])[:120])
    return "\n".join(lines)[:maximum]
