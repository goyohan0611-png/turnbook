"""Project brains: what a team's AIs need to know about a project, apart from personal memory.

Everyone has a private brain (conversations, notes, trial and error), saved automatically on
their own PCs. Every project has a shared brain, and it holds only what someone explicitly
publishes: checkpoints (decision, progress, blocker, next action, artifact, release) with their
context (what changed, where, why, how it was verified, what is left) and links to where the
work itself is: a git commit at the stage it reached, a Slack message, a document in a drive.
Files and conversations about them stay in those tools; the shared brain is how another
person's AI picks the work up. Nothing in this module reads the conversation or memory stores,
so no misfiled scope can leak a conversation into a project; `source_refs` carry local ids only.

Code is not copied: a git folder is linked by repository, branch and commit, and recorded at
the stage it really reached (committed, pushed, pr_open, merged, released), so "done" is never
claimed for a commit that only exists on one PC.

Project brains are optional: personal memory is the product, and a team shares only what it
chooses to. A project lives on a Postgres hub (a PC, server or NAS; office network or Tailscale),
or on this PC alone. On a hub, whoever runs it runs the server: they give a person an account
(`turnbook-hub add-user`), a login that cannot reach anyone's personal memory and sees only the
projects it is a member of. Each project is its own
schema with its own groups and key. Whoever creates a project owns it; only owners add members
(owner, editor or viewer), set the instructions or take others' checkpoints back. Every action,
reads included, is audited.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

TYPES = ("decision", "progress", "blocker", "next_action", "artifact", "release")
ROLES = ("owner", "editor", "viewer")
TEAM_PREFIX = "gmt1."
TEAM_GROUP = "gm_team"
OWNER_ROLE = "graphmind"
HUB_DATABASE = "gmhub"
FERNET = b"gAAAAA"               # how every Fernet token begins (version byte 0x80)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_projects(
    name TEXT PRIMARY KEY, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS ledger_checkpoints(
    id TEXT PRIMARY KEY, project TEXT NOT NULL, stamp TEXT NOT NULL, created_at TEXT NOT NULL,
    status TEXT NOT NULL, actor_role TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_audit(
    stamp TEXT NOT NULL, at TEXT NOT NULL, actor_role TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL)
"""

# On a hub, every project is its own brain: a schema of its own in the hub's database (gmhub,
# apart from anyone's personal memory), its own three groups (reader, writer, owner; each with
# the rights of the one before), and its own key. A login sees a project only as a member of one
# of its groups, so a policy written wrong in one project cannot open another, and a project can
# be backed up, moved or dropped whole. Whoever creates a project owns it; whoever runs the hub
# runs the server and is a member of nothing by that alone. The template is filled in with the
# schema and its groups when a project is made (format(): %1$I schema, %2$I/%3$I/%4$I groups).
PROJECT_TEMPLATE = """
CREATE SCHEMA %1$I;
CREATE TABLE %1$I.ledger_projects(
    name TEXT PRIMARY KEY, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '');
CREATE TABLE %1$I.ledger_checkpoints(
    id TEXT PRIMARY KEY, project TEXT NOT NULL, stamp TEXT NOT NULL, created_at TEXT NOT NULL,
    status TEXT NOT NULL, actor_role TEXT NOT NULL DEFAULT current_user, payload TEXT NOT NULL);
CREATE TABLE %1$I.ledger_audit(
    stamp TEXT NOT NULL, at TEXT NOT NULL, actor_role TEXT NOT NULL DEFAULT current_user,
    actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL);
GRANT %2$I TO %3$I;
GRANT %3$I TO %4$I;
GRANT USAGE ON SCHEMA %1$I TO %2$I;
GRANT SELECT ON ALL TABLES IN SCHEMA %1$I TO %2$I;
GRANT INSERT ON %1$I.ledger_audit TO %2$I;
GRANT UPDATE (status, payload) ON %1$I.ledger_checkpoints TO %2$I;
GRANT INSERT ON %1$I.ledger_checkpoints TO %3$I;
GRANT UPDATE (instructions) ON %1$I.ledger_projects TO %4$I;
ALTER TABLE %1$I.ledger_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE %1$I.ledger_audit ENABLE ROW LEVEL SECURITY;
CREATE POLICY see ON %1$I.ledger_checkpoints FOR SELECT USING (true);
CREATE POLICY publish ON %1$I.ledger_checkpoints FOR INSERT WITH CHECK (actor_role = current_user);
%5$s;
CREATE POLICY log ON %1$I.ledger_audit FOR INSERT WITH CHECK (actor_role = current_user);
CREATE POLICY see ON %1$I.ledger_audit FOR SELECT
    USING (actor_role = current_user OR pg_has_role(current_user, %4$L, 'MEMBER'))
"""

# The publisher may change their own checkpoint; an owner may only take someone else's back
# (ponytail: the owner still writes the retracted row's payload, which records who retracted it).
RETRACT_POLICY = """DROP POLICY IF EXISTS retract ON %1$I.ledger_checkpoints;
CREATE POLICY retract ON %1$I.ledger_checkpoints FOR UPDATE
    USING (actor_role = current_user OR pg_has_role(current_user, %4$L, 'MEMBER'))
    WITH CHECK (actor_role = current_user OR (status = 'retracted'
                AND pg_has_role(current_user, %4$L, 'MEMBER')))"""
PROJECT_TEMPLATE = PROJECT_TEMPLATE.replace("%5$s", RETRACT_POLICY)

# The hub's directory, in gmhub's public schema: which projects exist (the key is read only
# through hub_project_key, by members), the accounts, and who is in which project.
DIRECTORY = """
CREATE TABLE IF NOT EXISTS hub_projects(
    name TEXT PRIMARY KEY, schema TEXT UNIQUE NOT NULL, created_by TEXT NOT NULL,
    created_at TEXT NOT NULL, key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_accounts(member_role TEXT PRIMARY KEY, member TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_access(
    member_role TEXT NOT NULL, project TEXT NOT NULL, role TEXT NOT NULL,
    member TEXT NOT NULL, PRIMARY KEY(member_role, project));
UPDATE ledger_accounts a SET member = a.member || ' (' || right(a.member_role, 6) || ')'
    WHERE EXISTS (SELECT 1 FROM ledger_accounts b
                  WHERE b.member = a.member AND b.member_role < a.member_role);
UPDATE ledger_access x SET member = a.member FROM ledger_accounts a
    WHERE a.member_role = x.member_role AND x.member <> a.member;
CREATE UNIQUE INDEX IF NOT EXISTS ledger_accounts_member ON ledger_accounts(member);
CREATE TABLE IF NOT EXISTS hub_audit(
    stamp TEXT NOT NULL, at TEXT NOT NULL, actor_role TEXT NOT NULL, actor TEXT NOT NULL,
    action TEXT NOT NULL, target TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hub_template(id INTEGER PRIMARY KEY, ddl TEXT NOT NULL)
"""

# The hub's functions. They run as the database superuser (to make a project's schema and
# groups), check the caller themselves (session_user is the caller), and take names only.
# pg_temp is searched last: by default it comes first, and a caller's temporary table named
# hub_template ran its DDL as superuser (ALTER ROLE ... SUPERUSER).
HUB_FUNCTIONS = """
CREATE OR REPLACE FUNCTION hub_join(g text, r text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $f$
BEGIN
  -- without SET (Postgres 16+): the member uses the group's rights but cannot SET ROLE to it,
  -- so taking the group away ends those rights in every session at commit. Ending sessions
  -- instead raced: a reconnect before the commit could SET ROLE and keep the group.
  EXECUTE format('GRANT %I TO %I', g, r) || CASE
    WHEN current_setting('server_version_num')::int >= 160000 THEN ' WITH SET FALSE' ELSE '' END;
END $f$;
CREATE OR REPLACE FUNCTION hub_schema(p text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $f$
  SELECT schema FROM hub_projects WHERE name = p
$f$;
CREATE OR REPLACE FUNCTION hub_in_group(r text, g text) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $f$
BEGIN
  IF r IS NULL OR g IS NULL OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r)
     OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = g) THEN
    RETURN false;
  END IF;
  RETURN pg_has_role(r, g, 'MEMBER');
END $f$;
CREATE OR REPLACE FUNCTION hub_can(p text, level text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $f$
  SELECT hub_in_group(session_user, hub_schema(p) || level)
$f$;
CREATE OR REPLACE FUNCTION hub_project_key(p text) RETURNS text
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $f$
BEGIN
  IF NOT hub_can(p, '_r') THEN RAISE EXCEPTION 'permission denied for project %', p; END IF;
  RETURN (SELECT key FROM hub_projects WHERE name = p);
END $f$;
CREATE OR REPLACE FUNCTION hub_create_project_for(r text, p text, who text, at text, k text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $f$
DECLARE s text := 'p_' || left(md5(p), 12); g text;
BEGIN
  IF EXISTS (SELECT 1 FROM hub_projects WHERE name = p) THEN RETURN false; END IF;
  FOREACH g IN ARRAY ARRAY[s || '_r', s || '_w', s || '_o'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = g) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', g);
    END IF;
  END LOOP;
  EXECUTE format((SELECT ddl FROM hub_template WHERE id = 1), s, s || '_r', s || '_w', s || '_o');
  EXECUTE format('INSERT INTO %I.ledger_projects(name, created_by, created_at) VALUES (%L, %L, %L)',
                 s, p, who, at);
  INSERT INTO hub_projects VALUES (p, s, who, at, k);
  PERFORM hub_join(s || '_o', r);
  INSERT INTO ledger_access VALUES (r, p, 'owner',
    COALESCE((SELECT member FROM ledger_accounts WHERE member_role = r), who));
  RETURN true;
END $f$;
CREATE OR REPLACE FUNCTION hub_create_project(p text, who text, at text, k text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $f$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM ledger_accounts WHERE member_role = session_user) THEN
    RAISE EXCEPTION 'permission denied: no account on this hub';
  END IF;
  RETURN hub_create_project_for(session_user, p, who, at, k);
END $f$;
CREATE OR REPLACE FUNCTION hub_set_member(p text, r text, level text) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $f$
DECLARE s text := hub_schema(p); m text; was text;
BEGIN
  SELECT member INTO m FROM ledger_accounts WHERE member_role = r;
  IF NOT FOUND THEN RAISE EXCEPTION 'NO_ACCOUNT'; END IF;
  SELECT role INTO was FROM ledger_access WHERE member_role = r AND project = p;
  EXECUTE format('REVOKE %I, %I, %I FROM %I', s || '_r', s || '_w', s || '_o', r);
  DELETE FROM ledger_access WHERE member_role = r AND project = p;
  IF level IS NOT NULL THEN
    PERFORM hub_join(s || CASE level WHEN 'owner' THEN '_o' WHEN 'editor' THEN '_w' ELSE '_r' END,
                     r);
    INSERT INTO ledger_access VALUES (r, p, level, m);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM ledger_access WHERE project = p AND role = 'owner') THEN
    RAISE EXCEPTION 'LAST_OWNER';
  END IF;
  -- before Postgres 16 a session could SET ROLE to the group and keep it after losing it: end
  -- that member's sessions on this hub (16+: hub_join grants without SET, nothing to end).
  -- Not the host: an owner granting and revoking the host must not cut the host's sessions.
  IF current_setting('server_version_num')::int < 160000 AND r <> 'graphmind'
     AND was IS NOT NULL AND (level IS NULL OR array_position(ARRAY['viewer','editor','owner'],
                              level) < array_position(ARRAY['viewer','editor','owner'], was)) THEN
    PERFORM pg_terminate_backend(pid) FROM pg_stat_activity
      WHERE usename = r AND datname = current_database() AND pid <> pg_backend_pid();
  END IF;
  RETURN m;
END $f$;
CREATE OR REPLACE FUNCTION hub_grant(p text, r text, level text) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $f$
BEGIN
  IF NOT hub_can(p, '_o') THEN
    RAISE EXCEPTION 'permission denied: only the project''s owner adds members';
  END IF;
  IF level NOT IN ('owner', 'editor', 'viewer') THEN RAISE EXCEPTION 'invalid role'; END IF;
  RETURN hub_set_member(p, r, level);
END $f$;
CREATE OR REPLACE FUNCTION hub_revoke(p text, r text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $f$
BEGIN
  IF NOT (hub_can(p, '_o') OR r = session_user) THEN
    RAISE EXCEPTION 'permission denied: only the project''s owner removes members';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM ledger_access WHERE member_role = r AND project = p) THEN
    RETURN false;
  END IF;
  PERFORM hub_set_member(p, r, NULL);
  RETURN true;
END $f$;
CREATE OR REPLACE FUNCTION hub_role_of(who text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $f$
  SELECT member_role FROM ledger_accounts WHERE member = who LIMIT 1
$f$;
CREATE OR REPLACE FUNCTION hub_rename(who text) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $f$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM ledger_accounts WHERE member_role = session_user) THEN
    RAISE EXCEPTION 'permission denied: no account on this hub';
  END IF;
  IF length(who) < 1 OR length(who) > 40 THEN RAISE EXCEPTION 'invalid name'; END IF;
  IF EXISTS (SELECT 1 FROM ledger_accounts WHERE member = who AND member_role <> session_user)
  THEN
    RAISE EXCEPTION 'NAME_TAKEN';
  END IF;
  UPDATE ledger_accounts SET member = who WHERE member_role = session_user;
  UPDATE ledger_access SET member = who WHERE member_role = session_user;
  RETURN who;
END $f$
"""
# what members may call (hub_create_project_for and hub_set_member are the hub's own)
HUB_FUNCTION_NAMES = ("hub_schema(text)", "hub_in_group(text, text)", "hub_can(text, text)",
                      "hub_project_key(text)", "hub_create_project(text, text, text, text)",
                      "hub_grant(text, text, text)", "hub_revoke(text, text)",
                      "hub_role_of(text)", "hub_rename(text)")
HUB_ONLY = ("hub_join(text, text)", "hub_create_project_for(text, text, text, text, text)",
            "hub_set_member(text, text, text)")
DIRECTORY_POLICIES = """
ALTER TABLE hub_projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_access ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_accounts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS member_read ON hub_projects;
CREATE POLICY member_read ON hub_projects FOR SELECT USING (hub_can(name, '_r'));
DROP POLICY IF EXISTS member_read ON ledger_access;
CREATE POLICY member_read ON ledger_access FOR SELECT USING (hub_can(project, '_r'));
DROP POLICY IF EXISTS everyone ON ledger_accounts;
CREATE POLICY everyone ON ledger_accounts FOR SELECT USING (true)
"""


def _stamp() -> str:
    """Order of publishing: created_at is to the second, and two checkpoints often share one.
    Strictly increasing within a process: Windows' clock can return the same ns twice."""
    global _LAST
    _LAST = max(time.time_ns(), _LAST + 1)
    return f"{_LAST:020d}"


_LAST = 0


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def named() -> bool:
    """Whether this person chose the name others see (else it is this PC's name)."""
    from turnbook.sharing import _config
    return bool(_config().get("user_name"))


def set_display_name(name: str) -> dict:
    """The name others see on checkpoints and member lists, instead of this PC's name:
    kept on this PC and set on the account of every hub this person is in (their own and each
    team hub), so all their PCs and teammates show it. Names are unique per hub."""
    from turnbook import sharing
    name = " ".join(str(name or "").split())
    if not name or len(name) > 40:
        raise ValueError("NAME: 1 to 40 characters, as teammates should see it (e.g. 고요한)")
    was = actor()
    sharing._save({"user_name": name})
    renamed, taken = [], []
    found, unreachable = ledgers()
    for book in found:
        try:
            if book.postgres:
                book._sql("SELECT hub_rename(?)", (name,))
                renamed.append(book.where)
        except ValueError as error:
            if "NAME_TAKEN" not in str(error):
                raise
            taken.append(book.where)
        finally:
            book.db.close()
    return {"name": name, "was": was, "renamed_on": renamed, "taken_on": taken,
            "unreachable": unreachable,
            "note": "Checkpoints already written keep the name they were written "
                    "with." + (" On the hubs in taken_on someone already has this name: choose "
                               "another." if taken else "")}


def actor() -> str:
    """Who this person is on their own ledger: config "user_name", else this PC's name. On a
    team hub it is the name in their invite (Ledger.by)."""
    from turnbook.sharing import _config, device_name
    return _config().get("user_name") or device_name()


def _home() -> Path:
    from turnbook.paths import turnbook_home
    return turnbook_home()


class Ledger:
    """The project brains on one hub (a connection to its database), or on this PC alone
    (sqlite). On a hub each project is its own schema with its own
    key: the methods first choose the project (_require), which sets both."""

    def __init__(self, connection, *, postgres=False, key=None, where="", by="", role=""):
        self.db, self.postgres, self.key = connection, postgres, key
        self.where, self.by, self.role = where, by, role
        self._project, self._keys = None, {}
        if not postgres:
            self.db.executescript(SCHEMA)

    # -- storage ----------------------------------------------------------------------------
    def _sql(self, query, parameters=()):
        try:
            return self.db.execute(query.replace("?", "%s") if self.postgres else query,
                                   parameters)
        except Exception as error:
            text = str(error)
            if "NO_ACCOUNT" in text:
                raise ValueError("NO_ACCOUNT: they have no account on this hub yet. The hub's "
                                 "operator adds one (turnbook-hub add-user <name>) and gives "
                                 "them the code; then add them again") from None
            if "NAME_TAKEN" in text:
                raise ValueError("NAME_TAKEN: someone on this hub already goes by that name; "
                                 "add something to tell you apart") from None
            if "LAST_OWNER" in text:
                raise ValueError("LAST_OWNER: a project keeps at least one owner; make someone "
                                 "else owner first") from None
            if "permission denied" in text or "row-level security" in text:
                raise ValueError("NOT_ALLOWED: your role in this project does not permit "
                                 "that") from None
            if "artifact not found" in text:
                raise ValueError("ARTIFACT_NOT_FOUND: it was retracted, or is not shared with "
                                 "you") from None
            raise

    def _commit(self):
        if not self.postgres:
            self.db.commit()

    @contextlib.contextmanager
    def _atomic(self):
        if self.postgres:
            with self.db.transaction():              # nested: a savepoint
                yield
        else:
            with self.db:                            # sqlite: commit, or roll back on error
                yield

    def _seal(self, data: bytes) -> bytes:
        return self.key.encrypt(data) if self.key else data

    def _unseal(self, data) -> bytes:
        """Sealed data starts as every Fernet token does; anything else was stored before the
        hub had a key, and is read as it is."""
        data = bytes(data)
        return self.key.decrypt(data) if self.key and data.startswith(FERNET) else data

    def _payload(self, stored: str) -> dict:
        return json.loads(self._unseal(stored.encode("utf-8")).decode("utf-8"))

    def _store(self, payload: dict) -> str:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._seal(raw).decode("ascii") if self.key else raw.decode("utf-8")

    def _audit(self, by, action, target):
        self._sql("INSERT INTO ledger_audit(stamp, at, actor, action, target) VALUES(?,?,?,?,?)",
                  (_stamp(), _now(), by, action, str(target)[:200]))

    # -- projects ---------------------------------------------------------------------------
    def create_project(self, name: str, by: str, *, at=None) -> dict:
        """Whoever creates a project owns it: on a hub, an owner row for their own login."""
        name = _project_name(name)
        if self.postgres:
            from cryptography.fernet import Fernet
            created = self._sql("SELECT hub_create_project(?,?,?,?)",
                                (name, by, at or _now(),
                                 Fernet.generate_key().decode())).fetchall()[0][0]
            if not created and name not in self.names():
                raise ValueError(f"PROJECT_NAME_TAKEN: someone on this hub already has a "
                                 f"project called '{name}'; choose another name")
            self._require(name)
            if created:
                self._audit(by, "create_project", name)
            return {"project": name, "created": created, "owner": by}
        created = not self._sql("SELECT 1 FROM ledger_projects WHERE name=?", (name,)).fetchall()
        if created:
            with self._atomic():
                self._sql("INSERT INTO ledger_projects(name, created_by, created_at) "
                          "VALUES(?,?,?)", (name, by, at or _now()))
                self._audit(by, "create_project", name)
        return {"project": name, "created": created, "owner": by}

    # -- members (a project's owner manages them) -------------------------------------------
    def grant(self, project, member, role, by) -> dict:
        """Add a member who has an account on this hub to `project`, or change their role.
        Owner only, checked on the hub."""
        if role not in ROLES:
            raise ValueError(f"ROLE must be one of {', '.join(ROLES)}")
        project = self._require(project)
        self._needs_hub()
        with self._atomic():
            name = self._sql("SELECT hub_grant(?,?,?)",
                             (project, self._role_of(member), role)).fetchall()[0][0]
            self._audit(by, "invite", f"{project}/{name} {role}")
        return {"project": project, "member": name, "role": role}

    def revoke(self, project, member, by) -> dict:
        """Take `project` away from a member (owner only), or leave it yourself."""
        project = self._require(project)
        self._needs_hub()
        with self._atomic():
            # logged first: leaving yourself takes your right to write the log, and the failed
            # insert after it rolled the leaving back
            self._audit(by, "remove_member", f"{project}/{member}")
            removed = self._sql("SELECT hub_revoke(?,?)",
                                (project, self._role_of(member))).fetchall()[0][0]
        return {"project": project, "member": member, "removed": bool(removed),
                "note": "Their access to this project ended now."}

    def _role_of(self, member) -> str:
        """The login of the account shown as `member` (names can change; logins do not)."""
        found = self._sql("SELECT hub_role_of(?)", (str(member).strip(),)).fetchall()
        return found[0][0] if found and found[0][0] else _member_role(member)

    def _needs_hub(self):
        if not self.postgres:
            raise ValueError("NO_HUB: this project is on this PC only; host a hub (or move it "
                             "to one) before adding people")

    def members(self, project) -> list[dict]:
        project = self._require(project)
        if not self.postgres:
            by = self._sql("SELECT created_by FROM ledger_projects WHERE name=?",
                           (project,)).fetchall()[0][0]
            return [{"member": by, "role": "owner"}]
        rows = self._sql("SELECT member, role FROM public.ledger_access WHERE project=? "
                         "ORDER BY role, member", (project,)).fetchall()
        return [{"member": member, "role": role} for member, role in rows]

    def names(self) -> list[str]:
        table = "public.hub_projects" if self.postgres else "ledger_projects"
        return [row[0] for row in self._sql(f"SELECT name FROM {table} ORDER BY name").fetchall()]

    def projects(self) -> list[dict]:
        out = []
        for name in self.names():
            self._require(name)
            by, at = self._sql("SELECT created_by, created_at FROM ledger_projects WHERE name=?",
                               (name,)).fetchall()[0]
            last = self._sql("SELECT created_at FROM ledger_checkpoints WHERE project=? AND "
                             "status='published' ORDER BY stamp DESC LIMIT 1", (name,)).fetchall()
            count = self._sql("SELECT COUNT(*) FROM ledger_checkpoints WHERE project=? AND "
                              "status='published'", (name,)).fetchall()[0][0]
            out.append({"project": name, "created_by": by, "created_at": at,
                        "checkpoints": count, "last_shared_at": last[0][0] if last else None})
        return out

    def set_instructions(self, project, text, by) -> dict:
        """The project's standing guidance for every AI that works on it (owner only)."""
        project = self._require(project)
        text = str(text or "").strip()[:4000]
        found = scan_text(text)
        if found:
            raise ValueError("SECRET_IN_INSTRUCTIONS: " + ", ".join(found))
        with self._atomic():
            updated = self._sql("UPDATE ledger_projects SET instructions=? WHERE name=?",
                                (self._store({"text": text}) if text else "", project))
            if getattr(updated, "rowcount", 1) == 0:
                raise ValueError("NOT_ALLOWED: only the project's owner sets its instructions")
            self._audit(by, "set_instructions", project)
        return {"project": project, "instructions": text}

    def instructions(self, project) -> str:
        rows = self._sql("SELECT instructions FROM ledger_projects WHERE name=?",
                         (self._require(project),)).fetchall()
        return self._payload(rows[0][0])["text"] if rows and rows[0][0] else ""

    def _require(self, project: str) -> str:
        """The project, checked to exist for this person; on a hub, also the one the next
        statements read and write (its schema) and seal with (its key)."""
        project = _project_name(project)
        if self.postgres:
            if project == self._project:
                return project
            found = self._sql("SELECT schema FROM public.hub_projects WHERE name=?",
                              (project,)).fetchall()
            if found:
                if project not in self._keys:
                    self._keys[project] = _fernet_key(self._sql(
                        "SELECT hub_project_key(?)", (project,)).fetchall()[0][0])
                self._sql("SELECT set_config('search_path', ?, false)",
                          (f'"{found[0][0]}", public',))
                self._project, self.key = project, self._keys[project]
                return project
        elif self._sql("SELECT 1 FROM ledger_projects WHERE name=?", (project,)).fetchall():
            return project
        raise ValueError(f"PROJECT_NOT_FOUND: '{project}' does not exist or is not shared "
                         "with you (the owner creates projects and grants access)")

    def _locate(self, table: str, row_id: str) -> str | None:
        """The project holding row `row_id` of `table` (each project is its own schema on a
        hub), chosen for the next statements; None when no project of this person has it."""
        for name in self.names() if self.postgres else [None]:
            if name:
                self._require(name)
            found = self._sql(f"SELECT project FROM {table} WHERE id=?", (row_id,)).fetchall()
            if found:
                return self._require(found[0][0])
        return None

    # -- checkpoints ------------------------------------------------------------------------
    def publish(self, project, kind, summary, by, *, artifacts=None, source_refs=None,
                supersedes=None, details=None, context=None) -> dict:
        """`summary`: one line. `details`: what changed and why, files and functions, test
        results, links, what is left; a handover note for a teammate, never pasted chat."""
        project = self._require(project)
        checkpoint = self._checkpoint_record(project, kind, summary, by, artifacts, source_refs,
                                             supersedes, details, context)
        with self._atomic():
            self._insert_checkpoint(checkpoint)
        return {**checkpoint, "status": "published"}

    def _checkpoint_record(self, project, kind, summary, by, artifacts, source_refs, supersedes,
                           details, context=None) -> dict:
        if kind not in TYPES:
            raise ValueError(f"CHECKPOINT_TYPE must be one of {', '.join(TYPES)}")
        summary = str(summary or "").strip()
        if not summary:
            raise ValueError("CHECKPOINT_SUMMARY_REQUIRED")
        details = str(details or "").strip()
        if len(details) > 4000:
            raise ValueError("DETAILS_TOO_LONG: a checkpoint is a handover note (4,000 "
                             "characters at most), not a transcript")
        refs = [str(r) for r in (source_refs or [])]
        if any(len(r) > 200 for r in refs):         # ids, hashes and links only: never text
            raise ValueError("SOURCE_REFS_ARE_IDS: give local ids, hashes or links, not text")
        # the refs and attached links too: a key in a URL's query or a ref went out unchecked
        found = scan_text("\n".join([summary, details, *refs,
                                     json.dumps(artifacts or [], ensure_ascii=False)]))
        if found:
            raise ValueError("SECRET_IN_CHECKPOINT: " + ", ".join(found))
        if supersedes:
            self._checkpoint(supersedes, project)
        return {"id": "cp-" + uuid.uuid4().hex[:12], "project": project, "type": kind,
                "summary": summary, "details": details, "actor": by, "created_at": _now(),
                "artifacts": artifacts or [], "source_refs": refs, "supersedes": supersedes,
                "context": _clean_context(context)}

    def _insert_checkpoint(self, checkpoint):
        self._sql("INSERT INTO ledger_checkpoints(id, project, stamp, created_at, status, payload)"
                  " VALUES(?,?,?,?,?,?)", (checkpoint["id"], checkpoint["project"], _stamp(),
                                           checkpoint["created_at"], "published",
                                           self._store(checkpoint)))
        self._audit(checkpoint["actor"], "publish", f"{checkpoint['project']}/{checkpoint['id']}")

    def _checkpoint(self, checkpoint_id, project=None) -> tuple[dict, str]:
        if not project:
            self._locate("ledger_checkpoints", checkpoint_id)
        rows = self._sql("SELECT project, status, payload FROM ledger_checkpoints WHERE id=?",
                         (checkpoint_id,)).fetchall()
        if not rows or (project and rows[0][0] != project):
            raise ValueError(f"CHECKPOINT_NOT_FOUND: {checkpoint_id}")
        return self._payload(rows[0][2]), rows[0][1]

    def timeline(self, project, *, limit=30, include_withdrawn=False, by=None) -> list[dict]:
        """Newest first. A checkpoint another one supersedes shows as superseded: worked out
        here, so superseding someone's decision never means editing their record."""
        project = self._require(project)
        rows = self._sql("SELECT status, payload FROM ledger_checkpoints WHERE project=? "
                         "ORDER BY stamp DESC", (project,)).fetchall()
        everything = [{**self._payload(payload), "status": status} for status, payload in rows]
        replaced = {c["supersedes"] for c in everything
                    if c.get("supersedes") and c["status"] == "published"}
        out = []
        for checkpoint in everything:
            if checkpoint["status"] == "published" and checkpoint["id"] in replaced:
                checkpoint["status"] = "superseded"
            if checkpoint["status"] == "retracted" and not include_withdrawn:
                continue
            out.append(checkpoint)
            if len(out) >= limit:
                break
        if by:
            self._audit(by, "timeline", project)
            self._commit()
        return out

    def retract(self, checkpoint_id, by) -> dict:
        checkpoint, status = self._checkpoint(checkpoint_id)
        checkpoint.update(retracted_by=by, retracted_at=_now())
        with self._atomic():
            updated = self._sql("UPDATE ledger_checkpoints SET status='retracted', payload=? "
                                "WHERE id=?", (self._store(checkpoint), checkpoint_id))
            if getattr(updated, "rowcount", 1) == 0:     # a member's policy hid another's row
                raise ValueError("NOT_ALLOWED: only the person who published it, or the "
                                 "owner, can retract it")
            self._audit(by, "retract", f"{checkpoint['project']}/{checkpoint_id}")
        return {"id": checkpoint_id, "status": "retracted", "was": status,
                "note": "Removed from the shared project."}

    # -- where the work is -------------------------------------------------------------------
    def attach(self, project, path, summary, by, *, kind="artifact", source_refs=None,
               details=None, context=None) -> dict:
        """Link what the user made, never copy it: a git folder as its commit at the stage it
        reached, or the address of a file or a message (a Slack link, a drive link). The file
        itself is shared where people share files."""
        project = self._require(project)
        target = str(path or "").strip()
        if re.match(r"^[a-z][a-z0-9+.-]*://", target, re.I):
            if len(target) > 500:
                raise ValueError("LINK_TOO_LONG: give the address, not the content")
            link = {"kind": "link", "url": target}
            return self.publish(project, kind, summary or target, by, artifacts=[link],
                                source_refs=source_refs, details=details, context=context)
        folder = Path(target).expanduser().resolve()
        link = git_link(folder if folder.is_dir() else folder.parent)
        if link is None:
            raise ValueError("FILES_STAY_WHERE_THEY_ARE: the shared brain keeps links, not files. "
                             "Share the file in Slack or a drive and attach its link, or attach "
                             "a git folder to link its commit.")
        summary = summary or f"{link['repo']} {link['stage']} @ {link['commit'][:8]}"
        return self.publish(project, kind, summary, by, artifacts=[link],
                            source_refs=source_refs, details=details, context=context)

    def audit(self, *, limit=50) -> list[dict]:
        rows = []
        for name in self.names() if self.postgres else [None]:
            if name:
                self._require(name)
            # on a hub the name shown is the account of the login that wrote the row: `actor`
            # is whatever the writer typed, and a viewer could log an action as the owner's
            rows += self._sql(
                "SELECT a.at, COALESCE(acc.member, a.actor_role), a.action, a.target, a.stamp "
                "FROM ledger_audit a LEFT JOIN public.ledger_accounts acc "
                "ON acc.member_role = a.actor_role ORDER BY a.stamp DESC LIMIT ?",
                (limit,)).fetchall() if self.postgres else self._sql(
                "SELECT at, actor, action, target, stamp FROM ledger_audit ORDER BY "
                "stamp DESC LIMIT ?", (limit,)).fetchall()
        rows = [row[:4] for row in sorted(rows, key=lambda row: row[4], reverse=True)[:limit]]
        return [{"at": at, "actor": who, "action": action, "target": target}
                for at, who, action, target in rows]


def _project_name(name) -> str:
    name = re.sub(r"\s+", "-", str(name or "").strip())
    if not name or len(name) > 80 or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("PROJECT_NAME: a short name, e.g. turnbook or 고객사-A-제안")
    return name


def git_link(folder: Path) -> dict | None:
    """Repository, branch and commit of the git checkout containing `folder`, and the stage the
    commit reached: committed (this PC only), pushed, pr_open, merged, released."""
    def git(*args):
        done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True,
                              timeout=15, stdin=subprocess.DEVNULL)
        return done.stdout.strip() if done.returncode == 0 else None
    try:
        commit = git("rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return None
    if not commit:
        return None
    remote = re.sub(r"//[^/@]+@", "//", git("remote", "get-url", "origin") or "")   # no tokens
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    top = git("rev-parse", "--show-toplevel") or folder
    link = {"kind": "git", "repo": remote or Path(top).name,
            "branch": branch, "commit": commit,
            "uncommitted_changes": bool(git("status", "--porcelain")), "stage": "committed"}
    remote_branches = git("branch", "-r", "--contains", commit) or ""
    if remote_branches:
        link["stage"] = "pushed"
    default = (git("symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "origin/main")
    if remote_branches and default in remote_branches.split():
        link["stage"] = "merged"
    tags = git("tag", "--contains", commit)
    if tags and link["stage"] == "merged":
        link["stage"], link["release"] = "released", tags.splitlines()[0]
    if link["stage"] == "pushed" and shutil.which("gh"):
        try:
            done = subprocess.run(["gh", "pr", "view", branch, "--json", "url,number,state"],
                                  cwd=str(folder), capture_output=True, text=True, timeout=20,
                                  stdin=subprocess.DEVNULL)
            if done.returncode == 0:
                pr = json.loads(done.stdout)
                link["pr"] = {"number": pr["number"], "url": pr["url"]}
                link["stage"] = {"OPEN": "pr_open", "MERGED": "merged"}.get(pr["state"], "pushed")
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            pass                                        # no gh login: pushed is still true
    return link


# -- work context ------------------------------------------------------------------------------
# what a checkpoint says about its work. The text fields are the publishing AI's own account;
# `addresses` (ids of the decisions, feedback and reviews it answers) and `evidence` (commit,
# PR, test log or document ids and links) point at the record itself.
CONTEXT_TEXT = ("what", "where", "why", "verification", "remaining", "review_focus")
CONTEXT_LINKS = ("addresses", "evidence")


def _clean_context(context) -> dict:
    if not context:
        return {}
    if not isinstance(context, dict):
        raise ValueError("CONTEXT: an object with " + ", ".join(CONTEXT_TEXT + CONTEXT_LINKS))
    out = {}
    for key, value in context.items():
        if key in CONTEXT_LINKS:
            items = [str(v).strip() for v in (value if isinstance(value, list) else [value])
                     if str(v).strip()]
            if any(len(item) > 300 for item in items):
                raise ValueError(f"CONTEXT_{key.upper()}: ids and links only, not text")
            out[key] = items
        elif key in CONTEXT_TEXT:
            text = str(value or "").strip()
            if len(text) > 2000:
                raise ValueError(f"CONTEXT_{key.upper()}_TOO_LONG: 2,000 characters at most")
            if text:
                out[key] = text
        else:
            raise ValueError(f"CONTEXT_FIELD '{key}': use "
                             + ", ".join(CONTEXT_TEXT + CONTEXT_LINKS))
    found = scan_text(json.dumps(out, ensure_ascii=False))
    if found:
        raise ValueError("SECRET_IN_CONTEXT: " + ", ".join(found))
    return out


# -- secret scan --------------------------------------------------------------------------------
def scan_text(text: str) -> list[str]:
    """Kinds of secrets in the text: the masking rules capture uses, applied whatever the
    user's mask_secrets setting (that setting is about this PC; this is about sharing)."""
    from turnbook.conversation_memory import RANDOM_CANDIDATE, SECRET_PATTERNS, _looks_random
    found = []
    for pattern, replacement in SECRET_PATTERNS:
        if callable(replacement):        # a check (card numbers: Luhn) decides per match
            replacement = next((masked for m in pattern.finditer(text)
                                if (masked := replacement(m)) != m.group(0)), None)
            if replacement is not None:
                found.append(re.sub(r"[\\\[\]\d]", "", replacement).strip("_ ") or "secret")
        elif pattern.search(text):
            found.append(re.sub(r"[\\\[\]\d]", "", replacement).strip("_ ") or "secret")
    if any(_looks_random(m.group(0)) for m in RANDOM_CANDIDATE.finditer(text)):
        found.append("random token")
    return sorted(set(found))


# -- keys, hub and teams -------------------------------------------------------------------------
def _fernet(secret: bytes):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(secret))


def _fernet_key(key: str):
    from cryptography.fernet import Fernet
    return Fernet(key.encode())


def _hub_url(url: str) -> str:
    """The hub database on the same server as a personal memory URL."""
    import urllib.parse
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(path="/" + HUB_DATABASE))


def ledger_secret(share_password: str) -> bytes:
    """The one key a hub had for all its projects before each got its own (derived one way from
    the share password): only moving those projects into their own schemas still needs it."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"graph-mind ledger",
                info=b"ledger key v1").derive(share_password.encode())


def _member_role(member: str) -> str:
    """A Postgres role name for a teammate: ascii, prefixed, unique per display name."""
    slug = re.sub(r"[^a-z0-9]+", "_", member.casefold()).strip("_")[:20] or "member"
    return f"gm_m_{slug}_{hashlib.sha256(member.encode()).hexdigest()[:6]}"


def _decode_team(code: str) -> dict:
    body = code.strip()[len(TEAM_PREFIX):]
    try:
        value = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        decoded = {"h": [str(h) for h in value["h"]], "p": int(value["p"]),
                   "u": str(value["u"]), "k": str(value["k"]), "n": str(value["n"]),
                   "c": value.get("c")}
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError("INVALID_TEAM_CODE: a team invite starts with gmt1.") from error
    # the login and hosts go into a connection string: a crafted invite once carried
    # "host=127.0.0.1 hostaddr=<attacker> password=" in its login name, and this PC's hub
    # password went to the attacker. Logins we make are gm_m_<slug>_<hash>; hosts are addresses.
    if (not re.fullmatch(r"[a-z0-9_]{1,63}", decoded["u"])
            or not all(re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", h) for h in decoded["h"])
            or not 0 < decoded["p"] < 65536):
        raise ValueError("INVALID_TEAM_CODE: the invite's login or address is malformed")
    return decoded


def _admin(database=HUB_DATABASE):
    import psycopg

    from turnbook import sharing
    return psycopg.connect(sharing._admin_uri(database), autocommit=True)


_PREPARED = set()


def prepare_hub(owner_url=None) -> None:
    """On the PC hosting a hub: its database, functions and project template, and the team
    group. Idempotent; done once per process (sharing.local_postgres calls it)."""
    from turnbook import sharing
    owner_url = owner_url or sharing.local_postgres()
    # each home has its own cluster: a hub prepared for one home is not prepared for another
    key = (owner_url, str(_home()))
    if key in _PREPARED:
        return
    _PREPARED.add(key)                       # first: local_postgres() below must not recurse
    try:
        _prepare(owner_url)
    except BaseException:
        _PREPARED.discard(key)               # tried again by the next caller
        raise


def _prepare(owner_url) -> None:
    """The hub database (gmhub) beside the personal memory one, its directory, functions and
    project template, the team group (the hosting person's own role is in it, a member like
    any other), and nobody but its owner able to reach the personal memory database."""
    with _admin("postgres") as admin:
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname=%s",
                             (HUB_DATABASE,)).fetchall():
            admin.execute(f"CREATE DATABASE {HUB_DATABASE}")
        if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (TEAM_GROUP,)).fetchall():
            admin.execute(f"CREATE ROLE {TEAM_GROUP} NOLOGIN")
        admin.execute(f"GRANT {TEAM_GROUP} TO {OWNER_ROLE}")
        admin.execute(f"REVOKE CONNECT ON DATABASE {HUB_DATABASE} FROM PUBLIC")
        admin.execute(f"GRANT CONNECT ON DATABASE {HUB_DATABASE} TO {TEAM_GROUP}")
        # a teammate's temporary table named like a hub table was found first by the
        # superuser functions (temp schema first in search_path): nobody needs temp tables here
        admin.execute(f"REVOKE TEMPORARY ON DATABASE {HUB_DATABASE} FROM PUBLIC")
        admin.execute("REVOKE CONNECT ON DATABASE graphmind FROM PUBLIC")
        admin.execute(f"REVOKE CONNECT ON DATABASE graphmind FROM {TEAM_GROUP}")
    with _admin() as hub:
        for statement in DIRECTORY.split(";"):
            if statement.strip():
                hub.execute(statement)
        hub.execute("INSERT INTO hub_template VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET "
                    "ddl = EXCLUDED.ddl", (PROJECT_TEMPLATE,))
        for statement in HUB_FUNCTIONS.split("$f$;"):
            if statement.strip():
                hub.execute(statement + ("$f$" if not statement.rstrip().endswith("$f$") else ""))
        for signature in HUB_FUNCTION_NAMES + HUB_ONLY:
            hub.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        for signature in HUB_FUNCTION_NAMES:
            hub.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {TEAM_GROUP}")
        hub.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        hub.execute(f"GRANT USAGE ON SCHEMA public TO {TEAM_GROUP}")
        hub.execute(f"GRANT SELECT (name, schema, created_by, created_at) ON hub_projects "
                    f"TO {TEAM_GROUP}")
        hub.execute(f"GRANT SELECT ON ledger_accounts, ledger_access TO {TEAM_GROUP}")
        for statement in DIRECTORY_POLICIES.split(";"):
            if statement.strip():
                hub.execute(statement)
        hub.execute("INSERT INTO ledger_accounts VALUES(%s,%s) ON CONFLICT DO NOTHING",
                    (OWNER_ROLE, actor()))
        for (schema,) in hub.execute("SELECT schema FROM hub_projects").fetchall():
            policy = hub.execute("SELECT format(%s::text, %s::text, %s::text, %s::text, "
                                 "%s::text)", (RETRACT_POLICY, schema, schema + "_r",
                                               schema + "_w", schema + "_o")).fetchone()[0]
            hub.execute(policy)
        # members added before hub_join could still SET ROLE to their group: grant again
        hub.execute("SELECT hub_join(p.schema || CASE a.role WHEN 'owner' THEN '_o' WHEN "
                    "'editor' THEN '_w' ELSE '_r' END, a.member_role) FROM ledger_access a "
                    "JOIN hub_projects p ON p.name = a.project")
    _migrate_shared_tables()
    # a hub already open to the network (shared before, or teammates in it): the person's other
    # PCs and the teammates reach the hub database, and nobody but the owner the memory one
    from turnbook import sharing
    rules = (sharing._pg_data() / "pg_hba.conf").read_text(encoding="utf-8")
    if "graphmind graphmind all scram-sha-256" in rules and \
            f"hostssl {HUB_DATABASE} +{TEAM_GROUP} all" not in rules:
        sharing.allow_from_network(f"{HUB_DATABASE} +{TEAM_GROUP}", "teammates, project brains")
        sharing.forbid_from_network(f"graphmind +{TEAM_GROUP}")


def _migrate_shared_tables() -> dict:
    """Projects from before each was a brain of its own (one set of tables beside the personal
    memory, one key for the whole hub) move into schemas of their own, sealed again with their
    own keys, one project per transaction. The old tables stay, renamed old_ledger_*, until
    every project has moved. (Stored files and reviews belonged to a feature since removed.)"""
    from cryptography.fernet import Fernet

    from turnbook import sharing
    with _admin("graphmind") as old:
        if not old.execute("SELECT to_regclass('public.ledger_projects')").fetchone()[0]:
            return {}
        password = sharing._config().get("share_password")
        old_key = _fernet(ledger_secret(password)) if password else None

        def plain(stored) -> bytes:
            data = stored.encode() if isinstance(stored, str) else bytes(stored)
            return old_key.decrypt(data) if old_key and data.startswith(FERNET) else data

        def rows(query, *parameters):
            return old.execute(query, parameters).fetchall()

        has = lambda table: old.execute("SELECT to_regclass(%s)",  # noqa: E731
                                        (f"public.{table}",)).fetchone()[0]
        accounts = rows("SELECT member_role, member FROM ledger_accounts") if has(
            "ledger_accounts") else []
        access = rows("SELECT member_role, project, role, member FROM ledger_access") if has(
            "ledger_access") else []
        moved = {"projects": 0}
        with _admin() as hub:
            for login, member in accounts + [(OWNER_ROLE, actor())]:
                hub.execute("INSERT INTO ledger_accounts VALUES(%s,%s) ON CONFLICT DO NOTHING",
                            (login, member))
            for name, by, at, instructions in rows("SELECT name, created_by, created_at, "
                                                   "instructions FROM ledger_projects"):
                if hub.execute("SELECT 1 FROM hub_projects WHERE name=%s", (name,)).fetchall():
                    continue                                   # moved by an earlier run
                owner = next((r for r, p, role, _ in access if p == name and role == "owner"),
                             OWNER_ROLE)
                key = Fernet.generate_key()
                new = Fernet(key)
                seal = lambda stored: new.encrypt(plain(stored)).decode()  # noqa: E731
                with hub.transaction():
                    hub.execute("SELECT hub_create_project_for(%s,%s,%s,%s,%s)",
                                (owner, name, by, at, key.decode()))
                    schema = hub.execute("SELECT schema FROM hub_projects WHERE name=%s",
                                         (name,)).fetchone()[0]
                    if instructions:
                        hub.execute(f'UPDATE "{schema}".ledger_projects SET instructions=%s',
                                    (seal(instructions),))
                    for login, project, role, member in access:
                        if project == name and login != owner:
                            hub.execute("INSERT INTO ledger_accounts VALUES(%s,%s) ON "
                                        "CONFLICT DO NOTHING", (login, member))
                            hub.execute("SELECT hub_set_member(%s,%s,%s)",
                                        (name, login, role))
                    for row in rows("SELECT id, stamp, created_at, status, actor_role, "
                                    "payload FROM ledger_checkpoints WHERE project=%s", name):
                        hub.execute(f'INSERT INTO "{schema}".ledger_checkpoints(id, project, '
                                    "stamp, created_at, status, actor_role, payload) "
                                    "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                                    (row[0], name, *row[1:5], seal(row[5])))
                    for row in rows("SELECT stamp, at, actor_role, actor, action, target "
                                    "FROM ledger_audit WHERE target=%s OR "
                                    "left(target, length(%s) + 1) = %s || '/'",
                                    name, name, name):
                        hub.execute(f'INSERT INTO "{schema}".ledger_audit VALUES'
                                    "(%s,%s,%s,%s,%s,%s)", row)
                moved["projects"] += 1
        for table in ("ledger_projects", "ledger_checkpoints", "ledger_artifacts",
                      "ledger_access", "ledger_reviews", "ledger_audit", "ledger_accounts"):
            if has(table):
                # a table made again after an earlier move (an app still on old code) is kept
                # too: renaming it onto old_<table> failed and every later call retried it
                kept, n = f"old_{table}", 1
                while has(kept):
                    n += 1
                    kept = f"old{n}_{table}"
                old.execute(f"ALTER TABLE {table} RENAME TO {kept}")
    return moved


def secure_hub() -> None:
    """prepare_hub, and let the team group in from the network: to the hub database only."""
    from turnbook import sharing
    prepare_hub()
    sharing.allow_from_network(f"{HUB_DATABASE} +{TEAM_GROUP}", "teammates, project brains")
    sharing.forbid_from_network(f"graphmind +{TEAM_GROUP}")


def host_projects() -> dict:
    """Make this PC's hub the home of this person's project brains (a NAS or an always-on PC
    can do it without holding anyone's personal memory). Projects kept on this PC alone so far
    move into it."""
    from turnbook import sharing
    opened = sharing.open_to_network()
    sharing._save({"hosts_projects": True})
    prepare_hub()
    return {"hosting": True, "moved": _move_local_projects(), "addresses": opened["addresses"],
            "firewall": opened["firewall"]}


def _move_local_projects() -> dict:
    """Projects from ~/.turnbook/ledger.sqlite into the hub, keeping ids and times."""
    path = _home() / "ledger.sqlite"
    if not path.exists():
        return {"projects": 0, "checkpoints": 0}
    solo = Ledger(sqlite3.connect(path))
    hub = own_ledger()
    moved = {"projects": 0, "checkpoints": 0}
    try:
        for name, by, at, _ in solo._sql("SELECT name, created_by, created_at, instructions "
                                         "FROM ledger_projects").fetchall():
            if name in hub.names():
                continue
            with hub._atomic():
                hub.create_project(name, by, at=at)
                if solo.instructions(name):
                    hub._sql("UPDATE ledger_projects SET instructions=? WHERE name=?",
                             (hub._store({"text": solo.instructions(name)}), name))
                rows = solo._sql("SELECT id, stamp, created_at, status, payload FROM "
                                 "ledger_checkpoints WHERE project=? ORDER BY stamp",
                                 (name,)).fetchall()
                for row_id, stamp, created, status, payload in rows:
                    checkpoint = solo._payload(payload)
                    hub._sql("INSERT INTO ledger_checkpoints(id, project, stamp, created_at, "
                             "status, payload) VALUES(?,?,?,?,?,?)",
                             (row_id, name, stamp, created, status, hub._store(checkpoint)))
                    moved["checkpoints"] += 1
            moved["projects"] += 1
    finally:
        solo.db.close()
        hub.db.close()
    return moved


def add_account(member: str, by: str = "hub operator", *, reset: bool = False) -> dict:
    """On the hub's machine: an account for a person, with no project at all, and the code to
    give them. Owners then add the account to their projects; the operator never does. An
    existing account keeps its code unless `reset` (a lost code): a new code ends the old one."""
    import secrets

    from turnbook import sharing
    member = str(member or "").strip()
    if not member or len(member) > 60:
        raise ValueError("MEMBER_NAME: the person's name, as others will see it")
    opened = sharing.open_to_network()
    secure_hub()
    login = _member_role(member)
    with _admin() as admin:
        exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (login,)).fetchall()
        if exists and not reset:
            return {"member": member, "account": "exists", "team_code": None,
                    "note": "They already have an account and their code still works. A "
                            "project's owner adding them is enough. Lost code: reset it."}
        password = secrets.token_urlsafe(18)
        if exists:
            admin.execute(f"ALTER ROLE {login} LOGIN PASSWORD '{password}'")
        else:
            admin.execute(f"CREATE ROLE {login} LOGIN PASSWORD '{password}' IN ROLE {TEAM_GROUP}")
        if admin.execute("SELECT 1 FROM ledger_accounts WHERE member=%s AND member_role<>%s",
                         (member, login)).fetchall():
            raise ValueError(f"NAME_TAKEN: an account on this hub already goes by '{member}'; "
                             "add something to tell them apart")
        admin.execute("INSERT INTO ledger_accounts VALUES(%s,%s) ON CONFLICT (member_role) DO "
                      "UPDATE SET member = EXCLUDED.member", (login, member))
        admin.execute("INSERT INTO hub_audit VALUES(%s,%s,%s,%s,%s,%s)",
                      (_stamp(), _now(), "postgres", by,
                       "reset_account" if exists else "add_account", f"account {member}"))
    # no key in the code: each project's key comes from the hub, to its members only
    code = TEAM_PREFIX + base64.urlsafe_b64encode(json.dumps(
        {"h": opened["addresses"], "p": sharing.PORT, "u": login, "k": password, "n": member,
         "c": opened["certificate"]}).encode()).decode().rstrip("=")
    return {"member": member, "account": "reset" if exists else "created", "team_code": code,
            "firewall": opened["firewall"],
            "note": "Give this code to them privately (it works like a password). On their PC "
                    "they paste it to their AI, which joins the hub. They see no project until "
                    "its owner adds them, and can create their own. Never the gm1. code: that "
                    "is your personal memory."}


def accounts() -> list[dict]:
    """The operator's view: who has an account and their role per project. Names only; the
    operator's tools never read what is in a project."""
    with _admin() as admin:
        people = admin.execute("SELECT member_role, member FROM ledger_accounts "
                               "ORDER BY member").fetchall()
        access = admin.execute("SELECT member_role, project, role FROM ledger_access").fetchall()
    out = []
    for login, member in people:
        out.append({"member": member, "hosting_person": login == OWNER_ROLE,
                    "projects": {p: r for who, p, r in access if who == login}})
    return out


def remove_account(member: str, by: str = "hub operator", *, transfer_to: str = None) -> dict:
    """Close a person's account on this hub: their login ends now. A project they alone own
    needs a new owner first (`transfer_to`, someone with an account)."""
    with _admin() as admin:
        found = admin.execute("SELECT member_role FROM ledger_accounts WHERE member=%s",
                              (member,)).fetchall()
        login = found[0][0] if found else _member_role(member)        # names can change
        if login == OWNER_ROLE:
            # the hosting person's login owns their personal memory database: DROP OWNED BY
            # below deleted every memory table in it
            raise ValueError("HOST_ACCOUNT: the person hosting this hub cannot be removed")
        sole = [p for (p,) in admin.execute(
            "SELECT project FROM ledger_access a WHERE member_role=%s AND role='owner' AND NOT "
            "EXISTS (SELECT 1 FROM ledger_access b WHERE b.project=a.project AND b.role='owner' "
            "AND b.member_role<>a.member_role) ORDER BY project", (login,)).fetchall()]
        if sole and not transfer_to:
            raise ValueError("LAST_OWNER: they alone own " + ", ".join(sole) + "; name who takes "
                             "them over (transfer_to), or have them make someone owner first")
        if sole:
            heir = (admin.execute("SELECT member_role FROM ledger_accounts WHERE member=%s",
                                  (transfer_to,)).fetchall() or [[_member_role(transfer_to)]])[0][0]
            found = admin.execute("SELECT member FROM ledger_accounts WHERE member_role=%s",
                                  (heir,)).fetchall()
            if not found:
                raise ValueError(f"NO_ACCOUNT: {transfer_to} has no account on this hub")
            if heir == login:
                raise ValueError("SAME_ACCOUNT: hand their projects to someone else")
            for project in sole:
                admin.execute("SELECT hub_set_member(%s,%s,'owner')", (project, heir))
        for (project,) in admin.execute("SELECT project FROM ledger_access WHERE member_role=%s",
                                        (login,)).fetchall():
            admin.execute("DELETE FROM ledger_access WHERE member_role=%s AND project=%s",
                          (login, project))
        admin.execute("DELETE FROM ledger_accounts WHERE member_role=%s", (login,))
        if admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (login,)).fetchall():
            # a session that did SET ROLE to a project group kept that group after the login
            # lost it: end their sessions too
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                          "WHERE usename=%s", (login,))
            admin.execute(f"DROP OWNED BY {login}")            # its rights here; then the
            with _admin("graphmind") as other:                 # other database, then the login
                other.execute(f"DROP OWNED BY {login}")
            admin.execute(f"DROP ROLE {login}")
        admin.execute("INSERT INTO hub_audit VALUES(%s,%s,%s,%s,%s,%s)",
                      (_stamp(), _now(), "postgres", by, "remove_account", f"account {member}"))
    return {"member": member, "removed": True, "transferred": {p: transfer_to for p in sole},
            "note": "Their access ended now."}


def delete_project(project: str, by: str = "hub operator", *, confirm: str = "") -> dict:
    """The operator removes a project whole: its schema and its groups. It cannot be undone, so
    the project's exact name must be given again as `confirm`."""
    name = _project_name(project)
    if confirm != name:
        raise ValueError("CONFIRM: give the project's exact name again to delete it")
    with _admin() as admin:
        found = admin.execute("SELECT schema FROM hub_projects WHERE name=%s", (name,)).fetchall()
        if not found:
            raise ValueError(f"PROJECT_NOT_FOUND: {name}")
        schema = found[0][0]
        with admin.transaction():
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            for group in ("_o", "_w", "_r"):
                admin.execute(f'DROP ROLE IF EXISTS "{schema}{group}"')
            admin.execute("DELETE FROM ledger_access WHERE project=%s", (name,))
            admin.execute("DELETE FROM hub_projects WHERE name=%s", (name,))
            admin.execute("INSERT INTO hub_audit VALUES(%s,%s,%s,%s,%s,%s)",
                          (_stamp(), _now(), "postgres", by, "delete_project", name))
    return {"deleted": name}


def hub_audit(limit=100) -> list[dict]:
    with _admin() as admin:
        rows = admin.execute("SELECT at, actor, action, target FROM hub_audit ORDER BY stamp "
                             "DESC LIMIT %s", (limit,)).fetchall()
    return [{"at": at, "actor": who, "action": action, "target": target}
            for at, who, action, target in rows]


def invite(member: str, role: str, projects: list[str], by: str) -> dict:
    """The owner, on the PC hosting their hub: an account for the teammate if they have none
    (with the code to give them), and access to `projects`. Projects kept on this PC alone
    move into the hub first."""
    if role not in ROLES:
        raise ValueError(f"ROLE must be one of {', '.join(ROLES)}")
    if not projects:
        raise ValueError("PROJECTS_REQUIRED: which projects this teammate may see")
    if not _hosts_here():
        host_projects()
    result = add_account(member, by)
    ledger = own_ledger()
    try:
        granted = [ledger.grant(p, member, role, by)["project"] for p in projects]
    finally:
        ledger.db.close()
    return {**result, "role": role, "projects": granted}


def join_team(code: str, name: str | None = None) -> dict:
    """On a teammate's PC: add a team hub to the ones it takes part in, shown there as `name`
    (else the name the invite gave). It changes nothing about this PC's personal memory or
    where that memory syncs: no conversation is sent anywhere."""
    from turnbook import sharing
    value = _decode_team(code)
    key = (tuple(value["h"]), value["p"], value["u"])
    hubs = [c for c in sharing._config().get("team_hubs", [])
            if (lambda v: (tuple(v["h"]), v["p"], v["u"]))(_decode_team(c)) != key]
    ledger = _team_ledger(code.strip())
    try:
        if name and name.strip() != ledger.by:           # refused (taken): not joined at all
            ledger.by = ledger._sql("SELECT hub_rename(?)",
                                    (" ".join(name.split())[:40],)).fetchall()[0][0]
        visible = ledger.names()
    finally:
        ledger.db.close()
    sharing._save({"team_hubs": hubs + [code.strip()]})
    if not named():
        sharing._save({"user_name": ledger.by})
    return {"joined_as": ledger.by, "projects": visible, "ledger": ledger.where,
            "personal_memory": "unchanged: still only on your own PCs"}


def leave_team(project: str) -> dict:
    """Stop using the team hub that holds `project` on this PC."""
    from turnbook import sharing
    hubs = sharing._config().get("team_hubs", [])
    for code in hubs:
        ledger = _team_ledger(code)
        try:
            if _project_name(project) in ledger.names():
                sharing._save({"team_hubs": [c for c in hubs if c != code]})
                return {"left": ledger.where}
        finally:
            ledger.db.close()
    raise ValueError(f"PROJECT_NOT_FOUND: no joined team hub holds '{project}'")


def _team_url(value: dict) -> str:
    import socket
    import urllib.parse

    from turnbook import sharing
    host = value["h"][0] if value["h"] else "127.0.0.1"
    for candidate in value["h"]:
        try:
            socket.create_connection((candidate, value["p"]), timeout=1.5).close()
            host = candidate
            break
        except OSError:
            continue
    return (f"postgresql://{value['u']}:{urllib.parse.quote(value['k'], safe='')}"
            f"@{host}:{value['p']}/{HUB_DATABASE}" + sharing.tls_options(value["c"]))


# -- which ledgers ------------------------------------------------------------------------------
class HubUnavailable(Exception):
    """A project hub that cannot be reached: its latest state is unknown, not empty."""


def _team_ledger(code: str) -> Ledger:
    import psycopg

    from turnbook import sharing
    value = _decode_team(code)
    host = value["h"][0] if value["h"] else "?"
    try:
        connection = psycopg.connect(_team_url(value), autocommit=True, **sharing.PG_LIMITS)
    except psycopg.OperationalError as error:
        raise HubUnavailable(f"team hub at {host}") from error
    named = connection.execute("SELECT member FROM ledger_accounts WHERE member_role = "
                               "current_user").fetchall()
    name = named[0][0] if named else value["n"]
    return Ledger(connection, postgres=True, where=f"team hub at {host} (as {name})", by=name,
                  role=value["u"])


def _hosts_here() -> bool:
    from turnbook import sharing
    settings = sharing._config()
    return bool(settings.get("hosts_projects")) or settings.get("memory_folder") == "local"


def own_ledger() -> Ledger:
    """This person's own project brains: on the hub this PC hosts or the person's memory uses,
    else on this PC alone (~/.turnbook/ledger.sqlite)."""
    import psycopg

    from turnbook import sharing
    settings = sharing._config()
    url = None
    if settings.get("hosts_projects"):
        url = sharing.local_postgres()
    else:
        shared = sharing.folder()
        if shared and sharing.is_database(shared):
            url = shared
    if url:
        if "@127.0.0.1:" in url:
            prepare_hub()
        try:
            connection = psycopg.connect(_hub_url(url), autocommit=True, **sharing.PG_LIMITS)
        except psycopg.OperationalError as error:
            raise HubUnavailable("your project hub") from error
        # the account's name, the same on every PC of this person (set_display_name sets it)
        named = connection.execute("SELECT member FROM ledger_accounts WHERE member_role = "
                                   "current_user").fetchall()
        return Ledger(connection, postgres=True, where="your project hub",
                      by=named[0][0] if named else actor(), role=OWNER_ROLE)
    path = _home() / "ledger.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return Ledger(sqlite3.connect(path), by=actor(),
                  where="this PC only (no hub yet: host or join one so others see it)")


def home_ledger(hub: str | None = None) -> Ledger:
    """Where a new project goes: the person's own hub, or this PC alone, unless they work only
    on team hubs (a company server): then the team hub, or the one `hub` names."""
    from turnbook import sharing
    settings = sharing._config()
    codes = settings.get("team_hubs", [])
    own = settings.get("hosts_projects") or sharing.is_database(sharing.folder())
    if hub or (codes and not own):
        label = lambda c: ", ".join(_decode_team(c)["h"])  # noqa: E731
        matches = [c for c in codes if not hub or hub in label(c)]
        if len(matches) == 1:
            return _team_ledger(matches[0])
        raise ValueError("HUB_REQUIRED: say which hub the project goes on (`hub`): "
                         + "; ".join(label(c) for c in codes))
    return own_ledger()


def ledgers() -> tuple[list[Ledger], list[str]]:
    """Every project brain this person takes part in, and the hubs that cannot be reached."""
    from turnbook.sharing import _config
    found, unreachable = [], []
    try:
        found.append(own_ledger())
    except HubUnavailable as error:
        unreachable.append(str(error))
    for code in _config().get("team_hubs", []):
        try:
            found.append(_team_ledger(code))
        except HubUnavailable as error:
            unreachable.append(str(error))
    return found, unreachable


def ledger_for(project: str) -> Ledger:
    """The one ledger that holds `project`; the others are closed. If it is in none that answers
    and some hub is down, the answer is "unknown", never "no such project"."""
    name = _project_name(project)
    found, unreachable = ledgers()
    chosen = None
    for ledger in found:
        if chosen is None and name in ledger.names():
            chosen = ledger
        else:
            ledger.db.close()
    if chosen is None:
        if unreachable:
            raise HubUnavailable(", ".join(unreachable))
        raise ValueError(f"PROJECT_NOT_FOUND: '{name}' is in none of your project brains")
    return chosen


def folder_project(cwd=None) -> str | None:
    """The project this working folder is bound to (config project_folders), if any: the
    default when the user says "공유해" without naming one. Sharing still needs the request."""
    from turnbook.sharing import _config
    here = Path(cwd or os.getcwd()).resolve()
    best = None
    for folder_path, project in _config().get("project_folders", {}).items():
        root = Path(folder_path)
        if (here == root or root in here.parents) and (
                best is None or len(str(root)) > len(str(best[0]))):
            best = (root, project)
    return best[1] if best else None


def bind_folder(folder_path, project) -> dict:
    from turnbook.sharing import _config, _save
    bound = dict(_config().get("project_folders", {}))
    resolved = str(Path(folder_path).expanduser().resolve())
    bound[resolved] = _project_name(project)
    _save({"project_folders": bound})
    return {"folder": resolved, "project": project}


def hub_main(argv=None) -> int:
    """turnbook-hub: run a hub on a server or NAS with no AI on it. The operator makes it a
    hub and manages accounts; owners manage their projects from their own AI."""
    import argparse
    parser = argparse.ArgumentParser(prog="turnbook-hub", description=hub_main.__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("host", help="make this machine a hub (once; it stays one)")
    add = commands.add_parser("add-user", help="an account for a person, and their code")
    add.add_argument("name")
    add.add_argument("--reset", action="store_true", help="a new code (the old one stops)")
    commands.add_parser("users", help="accounts and their roles per project")
    remove = commands.add_parser("remove-user", help="close an account now")
    remove.add_argument("name")
    remove.add_argument("--transfer-to", help="who takes over projects only they own")
    drop = commands.add_parser("delete-project", help="remove a project whole (cannot be undone)")
    drop.add_argument("name")
    drop.add_argument("--confirm", required=True, help="the project's name again")
    log = commands.add_parser("audit", help="who did what, newest first")
    log.add_argument("limit", nargs="?", type=int, default=50)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "host":
            result = host_projects()
        elif arguments.command == "add-user":
            if not _hosts_here():
                host_projects()
            result = add_account(arguments.name, reset=arguments.reset)
        elif arguments.command == "users":
            result = accounts()
        elif arguments.command == "remove-user":
            result = remove_account(arguments.name, transfer_to=arguments.transfer_to)
        elif arguments.command == "delete-project":
            result = delete_project(arguments.name, confirm=arguments.confirm)
        else:
            result = hub_audit(arguments.limit)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
