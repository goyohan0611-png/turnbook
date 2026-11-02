"""One brain across devices: the shared folder holds the memory, each machine holds an index.

A database file in a synced folder breaks: OneDrive and Dropbox copy files, they do not order two
machines' writes. So the memory itself is kept as plain append-only text in the shared folder, and
every machine builds its own SQLite index from it:

    <TURNBOOK_FOLDER>/log/
      desktop.jsonl        <- only the desktop ever appends here
      laptop.jsonl         <- only the laptop ever appends here

Each line is one record (a memory, or one conversation turn), never edited, never removed. A file
has exactly one writer, so syncing cannot interleave two machines' writes, and a line half-written
or half-synced is simply not read until its newline arrives. Reading imports every device's new
lines into the local index, in the order they were written, so a memory on the laptop can
supersede one written on the desktop: by then both are in the same database.

The index lives outside the folder and can be deleted at any time; `sync` rebuilds it.

The same log can live in Postgres instead of a folder ("memory_folder": "local" or a postgres://
URL). A synced folder reaches other PCs when the sync client gets round to it, often a minute
later; a Postgres table is there the moment the row commits, so a PC asking right after another
answered already sees it. "local" runs Postgres on this PC (fetched once, no setup); other PCs reach
it over Tailscale with its postgres:// URL. Rows are only ever inserted, one writer at a time
(an advisory lock), so reading by sequence number never skips a row committed late.

    python sharing.py            # self-check with two simulated devices
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import atexit
import base64
import functools
import hashlib
import json
import os
import platform
import re
import sqlite3
import subprocess
import time

from turnbook.packing import row_text

KINDS = ("memory", "turn", "forget")   # forget: ids another PC erased (forget.py)


def device_name() -> str:
    """This machine's name. TURNBOOK_DEVICE overrides it (tests, two accounts on one PC)."""
    raw = os.environ.get("TURNBOOK_DEVICE") or platform.node() or "device"
    return re.sub(r"[^a-z0-9]+", "-", raw.casefold()).strip("-") or "device"


def log_name() -> str:
    """This install's log file in the brain folder: the machine's name and a suffix drawn once.
    Two PCs named alike (DESKTOP-1, user-pc) appended to one file, and the sync client kept one
    PC's copy of it: the other's memories were lost."""
    if os.environ.get("TURNBOOK_DEVICE"):
        return device_name()
    name = _config().get("log_name")
    if not name:
        import secrets
        try:
            _save({"log_name": f"{device_name()}-{secrets.token_hex(3)}"}, keep=True)
            name = _config().get("log_name")
        except OSError:
            pass
    if not isinstance(name, str) or not name:
        import uuid  # config not writable: this machine's network address tells PCs apart
        name = f"{device_name()}-{uuid.getnode():012x}"
    return name


def config_path() -> Path:
    """Where the chosen brain folder is remembered for every client on this machine."""
    override = os.environ.get("TURNBOOK_CONFIG")
    if override:
        return Path(override)
    from turnbook.paths import turnbook_home
    return turnbook_home() / "config.json"            # ~/.turnbook unless TURNBOOK_HOME


def _config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_database(shared) -> bool:
    return isinstance(shared, str) and shared.startswith(("postgres://", "postgresql://"))


_LOCAL_URI = {}
_JOINED = {}
PORT = int(os.environ.get("TURNBOOK_PG_PORT", "54329"))   # fixed: a connection code stays valid
CODE_PREFIX = "gm1."
TAILSCALE = "100.64.0.0/10"


def _pg_data() -> Path:
    from turnbook.paths import turnbook_home
    data = turnbook_home() / "postgres"
    # this PC's own logins read their passwords here (libpq's password file): in no URL, so
    # never shown or saved anywhere else
    os.environ["PGPASSFILE"] = str(data.parent / "pgpass")
    # ...which libpq skips when PGPASSWORD is set: another Postgres's (GitHub's Windows runners
    # set PGPASSWORD=root) would be sent instead. Other hubs carry theirs in the URL.
    os.environ.pop("PGPASSWORD", None)
    return data


def _require_passwords(data: Path) -> None:
    """initdb made the cluster with --auth=trust: any program or other account on this PC could
    log in as the superuser with no password at all. Every login now needs one; this PC's own
    programs find theirs in PGPASSFILE, a file in the user's own home folder. A cluster from
    before is changed the first time it is used."""
    hba = data / "pg_hba.conf"
    before = hba.read_text(encoding="utf-8").splitlines()
    after = [line if line.lstrip().startswith("#") else re.sub(r"\btrust\s*$", "scram-sha-256",
                                                                     line) for line in before]
    if after == before:
        return
    import secrets

    import psycopg
    passfile = Path(os.environ["PGPASSFILE"])
    known = _passwords()                       # a run that stopped half way chose them already
    # the share password is already graphmind's when other PCs log in with it: keep it
    words = {"postgres": known.get("postgres") or secrets.token_urlsafe(18),
             "graphmind": known.get("graphmind") or _config().get("share_password")
             or secrets.token_urlsafe(18)}
    passfile.write_text("".join(f"127.0.0.1:{PORT}:*:{role}:{word}\n"
                                for role, word in words.items()), encoding="utf-8")
    passfile.chmod(0o600)                      # libpq ignores a file others can read (Unix)
    with psycopg.connect(_admin_uri(), autocommit=True) as admin:
        for role, word in words.items():        # token_urlsafe: nothing in it closes the quote
            admin.execute(f"ALTER ROLE {role} PASSWORD '{word}'")
        hba.write_text("\n".join(after) + "\n", encoding="utf-8")
        admin.execute("SELECT pg_reload_conf()")
    import time
    time.sleep(0.5)                          # ponytail: the reload is a signal with no "done" flag


def _passwords() -> dict:
    """role -> password, from this PC's password file."""
    try:
        lines = Path(os.environ["PGPASSFILE"]).read_text(encoding="utf-8").splitlines()
    except (OSError, KeyError):
        return {}
    return {line.split(":")[3]: line.split(":", 4)[4] for line in lines if line.count(":") >= 4}


def local_postgres() -> str:
    """This PC's own Postgres, started on first use and left running for every other client
    (Claude, Codex and the capture service find the running server and share it).

    The server is started here on a port that stays put, with a password for anyone not on this
    PC (postgres_bin says where its programs come from). The memory lives in database
    "graphmind" under role "graphmind", which is not a superuser: that is the role other PCs log
    in as."""
    data = _pg_data()
    # Checked on every call, not once per process: a server that stopped (reboot, a crash, a
    # console closing on it) is started again by whoever needs it next, instead of every
    # client failing until someone notices. A server still running on programs from before
    # (pgserver's, built without TLS) is restarted on the current ones, once.
    if not _current(_server_exe(data)):
        import fasteners
        data.parent.mkdir(parents=True, exist_ok=True)
        with fasteners.InterProcessLock(str(data.parent / "postgres.lock")):
            fresh = not (data / "PG_VERSION").exists()
            if fresh:
                _initdb_with_retries(data)
            running = _server_exe(data)
            if running and not _current(running):
                stop_postgres(data)
            if not _current(_server_exe(data)):
                _start(data, PORT)
            # not only when fresh: a start that failed after initdb left a cluster without them
            import psycopg
            with psycopg.connect(_admin_uri(), autocommit=True) as admin:
                if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname='graphmind'").fetchone():
                    admin.execute("CREATE ROLE graphmind LOGIN")
                if not admin.execute("SELECT 1 FROM pg_database WHERE datname='graphmind'"
                                     ).fetchone():
                    admin.execute("CREATE DATABASE graphmind OWNER graphmind")
    if re.search(r"(?m)^[^#\n]*\btrust\s*$", (data / "pg_hba.conf").read_text(encoding="utf-8")):
        import fasteners
        with fasteners.InterProcessLock(str(data.parent / "postgres.lock")):
            _require_passwords(data)
    _LOCAL_URI["uri"] = f"postgresql://graphmind@127.0.0.1:{PORT}/graphmind"
    # project brains' tables and file functions, once per process: a teammate or the owner's
    # other PC may use them without this PC ever opening a project itself
    try:
        from turnbook.project_brains import prepare_hub
        prepare_hub(_LOCAL_URI["uri"])
    except Exception:
        pass                                  # the personal memory works without them
    return _LOCAL_URI["uri"]


# Postgres's own programs, built with OpenSSL for every machine a hub runs on: Windows, macOS,
# and Linux on x86-64 and 64-bit ARM (a NAS, a Raspberry Pi), from
# theseus-rs/postgresql-binaries (the Windows build is EnterpriseDB's). On Linux, zonky's build:
# theseus-rs's links the system's libxml2.so.2 and ICU 74, which Ubuntu 26.04 no longer has, and
# zonky's carries its own (lib/, found through $ORIGIN). They are fetched once,
# the first time a machine hosts,
# checked against the hashes pinned here and kept in the user's cache folder, apart from any
# memory; every Turnbook home on the machine shares them. Nothing here is a Python package, so
# hosting works on any Python. Same major version as before (16): existing data is used as is.
POSTGRES = "16.14.0"
_POSTGRES_BUILDS = {
    ("Windows", "x86_64"): ("x86_64-pc-windows-msvc.zip",
                            "69f1cf5060ab99190f98604312b09d80a691cbcd1836bd4a2eb2067a7ad9a89f"),
    ("Linux", "x86_64"): ("linux-amd64",
                          "3278331b124b46fb9f8bea30c7ff3bd6227a3934dad38a6ddb625eb7dcfa8ca8"),
    ("Linux", "aarch64"): ("linux-arm64v8",
                           "482f5ccdb15f68f4bfe524a5b9df734e169db9d1d29c32a513af44ac9efc127a"),
    ("Darwin", "x86_64"): ("x86_64-apple-darwin.tar.gz",
                           "c5ecdea2528e29503140e259c043002f6f8f2e9d1ee2f1decb44e8b394254820"),
    ("Darwin", "aarch64"): ("aarch64-apple-darwin.tar.gz",
                            "a7a4846456df26d27f815267dfe725b4ad4f46312c032e7b5939468250a4891c"),
}
_MACHINES = {"amd64": "x86_64", "x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


def postgres_bin() -> Path:
    """The folder holding this machine's Postgres programs, fetched the first time."""
    build = _POSTGRES_BUILDS.get((platform.system(),
                                  _MACHINES.get(platform.machine().lower(), "")))
    if build is None:
        raise RuntimeError(f"Hosting a hub on {platform.system()} {platform.machine()} is not "
                           "supported yet (Windows, macOS, and Linux on x86-64 or 64-bit ARM "
                           "are). Joining a hub another machine hosts works here.")
    from platformdirs import user_cache_dir
    cache = Path(os.environ.get("TURNBOOK_RUNTIME")
                 or user_cache_dir("graph-mind", appauthor=False))
    root = cache / f"postgresql-{POSTGRES}"
    linux = platform.system() == "Linux"
    if linux:
        if _runs(root / "bin" / "postgres"):        # theseus-rs's, fetched by 0.9.11 and before
            return root / "bin"
        root = cache / f"postgresql-{POSTGRES}-portable"
    if (root / "bin" / _exe("postgres")).exists():
        return root / "bin"
    import hashlib
    import io
    import shutil
    import tarfile
    import urllib.request
    import uuid
    import zipfile
    name = f"postgresql-{POSTGRES}-{build[0]}"
    url = ("https://github.com/theseus-rs/postgresql-binaries/releases/"
           f"download/{POSTGRES}/{name}")
    if linux:
        artifact = f"embedded-postgres-binaries-{build[0]}"
        url = ("https://repo1.maven.org/maven2/io/zonky/test/postgres/"
               f"{artifact}/{POSTGRES}/{artifact}-{POSTGRES}.jar")
    with urllib.request.urlopen(url, timeout=600) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != build[1]:
        raise RuntimeError("The Postgres download did not match its pinned hash; nothing was "
                           "installed. Try again later.")
    partial = root.with_name(f"{root.name}.partial-{uuid.uuid4().hex[:8]}")
    if linux:                                 # a jar holding one .txz: bin/, lib/, share/
        import lzma
        with zipfile.ZipFile(io.BytesIO(data)) as jar:
            data = lzma.decompress(jar.read(next(n for n in jar.namelist()
                                                 if n.endswith(".txz"))))
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            archive.extractall(partial)
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            if hasattr(tarfile, "data_filter"):
                archive.extractall(partial, filter="data")    # no path leaves the folder
            else:
                archive.extractall(partial)                   # ponytail: Python <3.10.12
    inner = partial / name.removesuffix(".zip").removesuffix(".tar.gz")
    try:
        os.replace(inner if inner.is_dir() else partial, root)
    except OSError:
        if not (root / "bin").is_dir():                       # not another process's copy
            raise
    shutil.rmtree(partial, ignore_errors=True)
    return root / "bin"


@functools.lru_cache(maxsize=None)
def _runs(program: Path) -> bool:
    """Whether this Postgres program starts on this machine (its libraries are all there)."""
    try:
        return subprocess.run([str(program), "-V"], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _exe(name: str) -> str:
    return name + ".exe" if os.name == "nt" else name


def _pg_tool(name: str, args: list, timeout: int = 120) -> str:
    """Run one of Postgres's programs, hidden, and return what it printed. The output goes to a
    file, not a pipe: the server pg_ctl starts keeps a pipe open and the call never returns."""
    import tempfile
    with tempfile.TemporaryFile() as output:
        try:
            subprocess.run([str(postgres_bin() / _exe(name)), *map(str, args)], check=True,
                           stdout=output, stderr=subprocess.STDOUT, timeout=timeout, **_DETACHED)
        except subprocess.CalledProcessError as error:
            output.seek(0)
            raise subprocess.CalledProcessError(error.returncode, error.cmd,
                                                output.read().decode(errors="replace")) from None
        output.seek(0)
        return output.read().decode(errors="replace")


def _server_exe(data: Path) -> str | None:
    """The program of the Postgres running on `data`, or None when none is."""
    import psutil
    try:
        pid = int((data / "postmaster.pid").read_text(encoding="utf-8").split()[0])
        process = psutil.Process(pid)
        return process.exe() if "postgres" in process.name().lower() else None
    except (OSError, ValueError, IndexError, psutil.Error):
        return None


def _current(exe: str | None) -> bool:
    return bool(exe) and (os.path.normcase(str(Path(exe).parent))
                          == os.path.normcase(str(postgres_bin())))


def _start(data: Path, port: int) -> None:
    """After a crash Postgres first syncs its whole folder to disk (measured 25-55 s on a laptop
    with antivirus), so the start may wait minutes. The log stays outside that folder: in it, the
    sync met the log the server was writing ("sharing violation") and waited 30 s more."""
    def start():
        if not _current(_server_exe(data)):                # a failed try may have started it
            # no line per checkpoint: two thirds of a log that only ever grew (60 KB a day)
            _pg_tool("pg_ctl", ["-D", data, "-w", "-t", "300",
                                "-o", f"-p {port} -c log_checkpoints=off",
                                "-l", data.parent / "postgres.log", "start"], timeout=330)
    _retry(start, attempts=10)
    _wait_accepting(port, seconds=300, data=data)


def _wait_accepting(port, seconds, data=None):
    """Return once the server takes connections. pg_ctl -w can come back while it is still in
    crash recovery ("not yet accepting connections", Windows CI after a kill), and a retried
    start finds it running and does not wait at all. "Connection refused" while the server for
    `data` is running means its port is not open yet (CI, twice in a day): wait for that too."""
    import time

    import psycopg
    _pg_data()                                  # its password file
    deadline = time.monotonic() + seconds
    while True:
        try:
            psycopg.connect(f"postgresql://postgres@127.0.0.1:{port}/postgres",
                            connect_timeout=10).close()
            return
        except psycopg.OperationalError as error:
            starting = ("not yet accepting" in str(error) or "starting up" in str(error)
                        or ("refused" in str(error).lower() and data is not None
                            and _server_exe(data) is not None))
            if not starting or time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def stop_postgres(data: Path | None = None) -> None:
    data = data or _pg_data()
    if _server_exe(data):
        _pg_tool("pg_ctl", ["-D", data, "-w", "-t", "300", "-m", "fast", "stop"], timeout=330)


def _retry(step, attempts, undo=None):
    """Postgres's own tools fail at random on some Windows PCs. initdb, pg_ctl and postgres.exe
    itself (checking its own version at start) run commands through cmd.exe, and on a PC with
    AhnLab V3 running Windows refused to start cmd.exe ("Access is denied") in 10-30% of plain
    `cmd /c ver` runs while other programs always started: Postgres reports it as "popen
    failure: Bad file descriptor". There is no way around cmd.exe short of patching Postgres,
    and every attempt is independent, so trying again is the fix: ten attempts at those odds
    fail together far less than once in a million."""
    import time
    for attempt in range(attempts):
        try:
            return step()
        except subprocess.CalledProcessError:
            if undo:
                undo()
            if attempt == attempts - 1:
                raise
            time.sleep(0.5)


def _initdb_with_retries(data):
    """Create the cluster, once per PC. A failed run leaves the folder half made; clear it."""
    import shutil
    _retry(lambda: _pg_tool("initdb", ["-D", data, "--auth=trust", "--encoding=UTF8",
                                       "--locale=C", "-U", "postgres"]),
           attempts=10, undo=lambda: shutil.rmtree(data, ignore_errors=True))


# Started from Claude's or Codex's process, the server shared that program's console, and closing
# it sent Ctrl+C to Postgres too (0xC000013A in its log): the hub went down with an app window.
# Its own process group, with a console of its own that is never shown, it outlives whoever
# happened to start it. Not DETACHED_PROCESS (no console at all): Postgres runs cmd.exe for its
# own checks, and each of those then opened a console window on the user's screen. Its stdin is
# the null device: Postgres must not hold on to the caller's (an MCP server's is the pipe its app
# talks through), and a caller may have none at all (pythonw).
_DETACHED = ({"creationflags": 0x08000000 | 0x00000200,     # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
              "stdin": subprocess.DEVNULL}
             if os.name == "nt" else {"start_new_session": True, "stdin": subprocess.DEVNULL})


def _admin_uri(database="postgres") -> str:
    _pg_data()                                  # its password file
    return f"postgresql://postgres@127.0.0.1:{PORT}/{database}"


def share(open_firewall: bool = True) -> dict:
    """Let the user's other PCs in: the ones on the same network, and the ones on Tailscale.

    Postgres starts listening beyond this PC, role "graphmind" gets a password, and only that
    role, only for its own database, may log in from elsewhere (and only with the password);
    this PC itself still connects without one. The result is a connection code that carries the
    addresses, the port and the password, so the other PC's user pastes one line and types none
    of it. The same code comes back every time; it is as good as the password, so it is shown
    to the user and never posted anywhere."""
    opened = open_to_network(open_firewall)
    set_folder("local")
    sealed = _seal_existing(local_postgres())
    code = CODE_PREFIX + base64.urlsafe_b64encode(json.dumps(
        {"h": opened["addresses"], "p": PORT, "k": opened["password"],
         "c": opened["certificate"]}).encode()).decode().rstrip("=")
    return {"connection_code": code, "addresses": opened["addresses"], "port": PORT,
            "lines_sealed": sealed, "firewall": opened["firewall"]}


def open_to_network(open_firewall: bool = True) -> dict:
    """Make this PC's Postgres reachable from the network, over TLS and with a password only,
    without moving this PC's personal memory there: a team hub on a NAS or a server needs only
    this. The hub's certificate goes into every code, so a PC checks it reached this hub."""
    import secrets

    import psycopg
    local_postgres()                        # first: it explains a machine it cannot host on
    password = (_config().get("share_password") or _passwords().get("graphmind")
                or secrets.token_urlsafe(18))
    _save({"share_password": password})          # before sealing: the line key derives from it
    data = _pg_data()
    certificate = _certificate(data)
    with psycopg.connect(_admin_uri(), autocommit=True) as admin:
        # token_urlsafe is [A-Za-z0-9_-]: nothing in it can close the quoted literal
        admin.execute(f"ALTER ROLE graphmind PASSWORD '{password}'")
        restart = admin.execute("SHOW listen_addresses").fetchone()[0] != "*"
        if restart:
            admin.execute("ALTER SYSTEM SET listen_addresses = '*'")
        if admin.execute("SHOW ssl").fetchone()[0] != "on":
            admin.execute("ALTER SYSTEM SET ssl = on")
    allow_from_network("graphmind graphmind", "other PCs, with the password")
    if restart:
        _retry(lambda: _pg_tool("pg_ctl", ["-D", data, "-w", "-m", "fast", "restart"]),
               attempts=10)
    return {"password": password, "addresses": _addresses(), "certificate": certificate,
            "firewall": _firewall() if open_firewall else "not changed"}


def allow_from_network(database_and_role: str, why: str) -> None:
    """A rule letting `database role` in from other machines: over TLS, with the password.
    The plain rules written before TLS become TLS-only. Then the server reads its settings."""
    import time

    import psycopg
    hba = _pg_data() / "pg_hba.conf"
    plain = f"host {database_and_role} all scram-sha-256"
    rule = "hostssl " + plain[len("host "):]
    before = hba.read_text(encoding="utf-8").splitlines()
    lines = [rule if line.strip() == plain else line for line in before]
    if rule not in lines:
        lines += ["", f"# Turnbook: {why}", rule]
    if lines != before:
        hba.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with psycopg.connect(_admin_uri(), autocommit=True) as admin:
        admin.execute("SELECT pg_reload_conf()")
    for _ in range(50):                      # the reload is a signal: wait until it took
        with psycopg.connect(_admin_uri(), autocommit=True) as admin:
            if admin.execute("SHOW ssl").fetchone()[0] == "on":
                break
        time.sleep(0.1)
    time.sleep(0.3)                          # ponytail: the rules have no "loaded" flag to read


def forbid_from_network(database_and_role: str) -> None:
    """Take back a rule allow_from_network wrote (teammates once reached the personal memory
    database's server entry; now only the hub's)."""
    import psycopg
    hba = _pg_data() / "pg_hba.conf"
    gone = {f"{kind} {database_and_role} all scram-sha-256" for kind in ("host", "hostssl")}
    before = hba.read_text(encoding="utf-8").splitlines()
    lines = [line for line in before if line.strip() not in gone]
    if lines != before:
        hba.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with psycopg.connect(_admin_uri(), autocommit=True) as admin:
            admin.execute("SELECT pg_reload_conf()")


def _certificate(data: Path) -> str:
    """This hub's TLS certificate (DER, base64url), made once: self-signed, and carried by every
    code for this hub, so a PC accepts this hub and nothing in between (sslmode=verify-ca)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    crt, key = data / "server.crt", data / "server.key"
    if not (crt.exists() and key.exists()):
        import datetime as dt

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        private = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Graph-MIND hub")])
        now = dt.datetime.now(dt.timezone.utc)
        made = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(private.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(days=1))
                .not_valid_after(now + dt.timedelta(days=365 * 30))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(private, hashes.SHA256()))
        key.write_bytes(private.private_bytes(serialization.Encoding.PEM,
                                              serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        if os.name != "nt":
            os.chmod(key, 0o600)             # Postgres refuses a key others can read
        crt.write_bytes(made.public_bytes(serialization.Encoding.PEM))
    der = x509.load_pem_x509_certificate(crt.read_bytes()).public_bytes(
        serialization.Encoding.DER)
    return base64.urlsafe_b64encode(der).decode().rstrip("=")


def tls_options(certificate: str | None) -> str:
    """The connection settings for a hub: its own certificate pinned when the code carries one.
    A code from before TLS has none; the connection is then still encrypted whenever the hub
    offers it (libpq's default), only without the check."""
    if not certificate:
        return ""
    import hashlib
    import ssl
    import urllib.parse

    from turnbook.paths import turnbook_home
    der = base64.urlsafe_b64decode(certificate + "=" * (-len(certificate) % 4))
    path = turnbook_home() / "hubs" / f"{hashlib.sha256(der).hexdigest()[:16]}.crt"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ssl.DER_cert_to_PEM_cert(der), encoding="ascii")
    return "?sslmode=verify-ca&sslrootcert=" + urllib.parse.quote(str(path), safe="")


def _addresses() -> list[str]:
    """This PC's IPv4 addresses other PCs can use: the local network first, then Tailscale."""
    import ipaddress
    import socket

    import psutil
    up = {name for name, stats in psutil.net_if_stats().items() if stats.isup}
    found = {entry.address for name, entries in psutil.net_if_addrs().items() if name in up
             for entry in entries if entry.family == socket.AF_INET}
    usable = [a for a in found if not (ipaddress.ip_address(a).is_loopback
                                       or ipaddress.ip_address(a).is_link_local)]
    tailscale = ipaddress.ip_network(TAILSCALE)
    return sorted(usable, key=lambda a: (ipaddress.ip_address(a) in tailscale,
                                         ipaddress.ip_address(a)))


def _firewall() -> str:
    """Windows blocks the port until a rule allows it. The rule admits only the local network
    and Tailscale, and adding it asks the user's permission once (an administrator prompt)."""
    if platform.system() != "Windows":
        return "not needed"
    import subprocess
    name = "Graph-MIND-Postgres"

    def exists():
        return subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                              capture_output=True).returncode == 0
    if exists():
        return "open"
    arguments = (f"advfirewall firewall add rule name={name} dir=in action=allow "
                 f"protocol=TCP localport={PORT} remoteip=LocalSubnet,{TAILSCALE}")
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Start-Process netsh -Verb RunAs -Wait -WindowStyle Hidden "
                    f"-ArgumentList '{arguments}'"], capture_output=True)
    return "open" if exists() else "blocked: the administrator prompt was declined"


def _decode(code: str) -> dict:
    body = code.strip()[len(CODE_PREFIX):]
    try:
        value = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        decoded = {"h": [str(h) for h in value["h"]], "p": int(value["p"]),
                   "k": str(value["k"]), "c": value.get("c")}
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError("INVALID_CONNECTION_CODE") from error
    if (not isinstance(decoded["c"], (str, type(None)))
            or not all(re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", h) for h in decoded["h"])
            or not 0 < decoded["p"] < 65536):
        raise ValueError("INVALID_CONNECTION_CODE")
    return decoded


def _joined(code: str) -> str:
    """The URL for a connection code: the first of its addresses that answers right now, so a
    laptop reaches the same brain over the office network and over Tailscale from home."""
    import socket
    import time
    import urllib.parse
    cached = _JOINED.get(code)
    if cached and time.monotonic() - cached[0] < 300:
        return cached[1]
    value = _decode(code)
    host = value["h"][0] if value["h"] else "127.0.0.1"
    for candidate in value["h"]:
        try:
            socket.create_connection((candidate, value["p"]), timeout=1.5).close()
            host = candidate
            break
        except OSError:
            continue
    url = (f"postgresql://graphmind:{urllib.parse.quote(value['k'], safe='')}"
           f"@{host}:{value['p']}/graphmind" + tls_options(value["c"]))
    _JOINED[code] = (time.monotonic(), url)
    return url


def folder():
    """Where the shared brain lives: a folder (Path), or a Postgres URL (str). TURNBOOK_FOLDER
    if set, otherwise the one chosen in conversation; "local" means this PC's own Postgres and a
    connection code means another PC's."""
    value = os.environ.get("TURNBOOK_FOLDER") or _config().get("memory_folder")
    if not value:
        return None
    if value == "local":
        return local_postgres()
    if value.startswith(CODE_PREFIX):
        return _joined(value)
    return value if is_database(value) else Path(value).expanduser()


def _save(changes: dict, keep: bool = False) -> None:
    """It holds the share password and join codes: this user only (mkstemp makes it 0600), and
    one writer at a time, or two saves lost one setting or raced the same temporary file."""
    import tempfile

    import fasteners
    target = config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with fasteners.InterProcessLock(str(target.with_suffix(".lock"))):
        settings = {**changes, **_config()} if keep else {**_config(), **changes}
        fd, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(settings, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)


def set_folder(path, password=None) -> Path:
    """Choose the brain folder for this machine. A synced folder (OneDrive, Google Drive, Dropbox)
    makes the same brain available on every PC that chooses it too. "local" is this PC's own
    Postgres; a connection code (from `share` on another PC) joins that PC's.

    A folder's lines are sealed: the first PC makes a password (lock_folder), the others need
    it (`password`), and a wrong or missing one raises ValueError before anything changes."""
    path = str(path).strip()
    if path == "local" or is_database(path):
        chosen = path
    elif path.startswith(CODE_PREFIX):
        chosen = path
        _decode(chosen)                          # refuse a mangled code before saving it
    else:
        chosen = Path(path).expanduser().resolve()
        (chosen / "log").mkdir(parents=True, exist_ok=True)
        lock_folder(chosen, password)
    if str(chosen) != str(_config().get("memory_folder")):
        # the queue was for the old hub and would go to the new one; everything in it is in this
        # PC's store, which `share_existing` sends on purpose
        _outbox().unlink(missing_ok=True)
    _save({"memory_folder": str(chosen)})
    return chosen


def append(shared: Path, kind: str, record: dict, *, require_delivery=False) -> None:
    """Add one record to this device's log. Only ever appends, and flushes it to disk."""
    if require_delivery:
        # For callers with their own durable job: return only after the transport commits.
        target = str(shared)
        if time.monotonic() < _HUB_DOWN.get(target, 0.0):
            raise ConnectionError("the shared log was unreachable a moment ago")
        send = _pg_append if is_database(shared) else _folder_append
        try:
            send(shared, _lines([(kind, record)]))
        except Exception:
            _HUB_DOWN[target] = time.monotonic() + 60
            raise
        _HUB_DOWN.pop(target, None)
        return
    append_many(shared, [(kind, record)])


def _lines(items) -> list[str]:
    lines = []
    for kind, record in items:
        if kind not in KINDS:
            raise ValueError("UNKNOWN_LOG_KIND")
        lines.append(json.dumps({"kind": kind, "device": device_name(),
                                 "written_at": datetime.now().isoformat(timespec="microseconds"),
                                 "record": record}, ensure_ascii=False, sort_keys=True))
    return lines


def append_many(shared: Path, items) -> int:
    """Append several records in one write and one flush (an export can be thousands)."""
    lines = _lines(items)
    if not lines:
        return 0
    # a log OneDrive holds open fails like a downed hub does: queued, not dropped
    send = _pg_append if is_database(shared) else _folder_append
    return _append_or_queue(shared, send, lines)


def _folder_append(shared, lines) -> int:
    if _marker(shared).exists() and _line_key(shared) is None:
        # a sealed folder this PC has no password for: queued here, never written in clear
        raise PermissionError("FOLDER_PASSWORD_NEEDED")
    lines = _seal(shared, lines)
    log = Path(shared).expanduser() / "log" / f"{log_name()}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    torn = False
    try:
        with log.open("rb") as tail:
            tail.seek(-1, os.SEEK_END)
            torn = tail.read(1) != b"\n"
    except OSError:                                  # new or empty file
        pass
    with log.open("a", encoding="utf-8") as stream:
        # a crash mid-write left half a line: without this newline the next record was glued
        # to it, and the reader skipped both as one corrupt line
        stream.write(("\n" if torn else "") + "\n".join(lines) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return len(lines)


def _shared_ids(shared) -> set:
    """(kind, id) of every turn and memory the shared log holds, forgotten ones included."""
    opened = _opener(shared)
    if is_database(shared):
        with _pg(shared) as connection:
            lines = [line for line in (opened(row[0]) for row in
                     connection.execute("SELECT line FROM sharing")) if line is not None]
    else:
        lines = [line for line in (
            opened(line) for log in (Path(shared) / "log").glob("*.jsonl")
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines())
            if line is not None]
    ids = set()
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        record = item.get("record", {})
        if item.get("kind") == "turn":
            ids.add(("turn", record.get("turn_id")))
        elif item.get("kind") == "memory":
            ids.add(("memory", record.get("memory_id")))
        elif item.get("kind") == "forget":           # never resend what was deleted
            ids |= {("turn", t) for t in record.get("turn_ids", [])}
            ids |= {("memory", m) for m in record.get("memory_ids", [])}
    return ids


def export_history(shared: Path, index: Path) -> dict:
    """Put what this machine remembered before the folder existed into its log, once.

    This uploads existing memories to wherever the folder syncs, so it runs only when the user
    asks for it. Each record is marked as exported and never written twice; memories go out in
    the order they were stored so a replacement always follows what it replaces.
    """
    db = _cursor_db(index)
    db.row_factory = sqlite3.Row
    try:
        db.execute("CREATE TABLE IF NOT EXISTS brain_log_exported("
                   "kind TEXT NOT NULL, id TEXT NOT NULL, PRIMARY KEY(kind, id))")
        # what this destination's log already holds, whoever wrote it, decides; not what was
        # exported somewhere before (brain_log_exported names no destination: after moving to
        # an empty folder, sharing what this PC had sent nothing). A turn published as it was
        # captured is in the log too, and a PC whose turns missed a downed hub sends just those.
        done = _shared_ids(shared)
        items = _history(db, done)
        written = append_many(shared, items)
        with db:
            db.executemany("INSERT OR IGNORE INTO brain_log_exported VALUES(?,?)",
                           [(kind, record["memory_id"] if kind == "memory" else record["turn_id"])
                            for kind, record in items])
        return {"memories": sum(kind == "memory" for kind, _ in items),
                "turns": sum(kind == "turn" for kind, _ in items), "lines_written": written}
    finally:
        db.close()


def _history(db: sqlite3.Connection, done: set) -> list:
    """Every memory and turn of this index not in `done`, as log records, oldest first so a
    replacement always follows what it replaces."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    items = []
    if "local_brain_memories" in tables:
        # the profile carries attributes.fact: without it a fact arrives as a plain memory
        profiled = "local_brain_memory_profiles" in tables
        for row in db.execute(
                "SELECT m.*" + (", p.memory_class, p.modality, p.retention, p.importance, "
                                "p.confidence, p.attributes_json, p.related_memory_ids_json"
                                if profiled else "") +
                " FROM local_brain_memories m" +
                (" LEFT JOIN local_brain_memory_profiles p USING(memory_id)" if profiled else "") +
                " ORDER BY m.revision"):
            if ("memory", row["memory_id"]) in done:
                continue
            record = {
                "memory_id": row["memory_id"], "scope": row["scope"],
                "memory_type": row["memory_type"], "title": row["title"],
                "content": row["content"], "effective_at": row["effective_at"],
                "known_at": row["known_at"], "actor": row["actor"],
                "tags": json.loads(row["tags_json"]),
                "entities": json.loads(row["entities_json"]),
                "provenance": json.loads(row["provenance_json"]),
                "supersedes_memory_id": row["supersedes_memory_id"]}
            if profiled and row["memory_class"] is not None:
                record.update({
                    "memory_class": row["memory_class"], "modality": row["modality"],
                    "retention": row["retention"], "importance": row["importance"],
                    "confidence": row["confidence"],
                    "attributes": json.loads(row["attributes_json"]),
                    "related_memory_ids": json.loads(row["related_memory_ids_json"])})
            items.append(("memory", record))
    if "conversation_turns" in tables:
        for row in db.execute("SELECT * FROM conversation_turns ORDER BY revision"):
            if ("turn", row["turn_id"]) in done:
                continue
            items.append(("turn", {**{key: row[key] for key in (
                "turn_id", "client", "client_session_id", "scope", "cwd", "happened_at",
                "known_at", "role", "raw_sha256", "redaction_count",
                "source_path", "source_ordinal")}, "content_redacted": row_text(row)}))
    return items


def _in_project(db, items, project) -> list:
    """The items of one project folder: turns whose working folder is it or inside it, and the
    memories the capture service drew from those turns."""
    root = os.path.normcase(os.path.abspath(os.path.expanduser(str(project))))
    if not os.path.isdir(root):
        raise ValueError("PROJECT_FOLDER_NOT_FOUND")

    def inside(cwd):
        # a folder recorded on another kind of PC (C:\... read on a Mac) is not absolute here:
        # made absolute against the current folder it could land inside the project
        if not cwd or not os.path.isabs(cwd):
            return False
        folder = os.path.normcase(os.path.normpath(cwd))
        return folder == root or folder.startswith(root.rstrip(os.sep) + os.sep)

    turns = {record["turn_id"] for kind, record in items
             if kind == "turn" and inside(record.get("cwd"))}
    drawn = set()
    if turns and db.execute("SELECT 1 FROM sqlite_master WHERE name='semantic_ingestion_audit'"
                            ).fetchone():
        drawn = {memory_id for turn_id, memory_id in db.execute(
            "SELECT turn_id, memory_id FROM semantic_ingestion_audit WHERE memory_id IS NOT NULL")
            if turn_id in turns}
    return [(kind, record) for kind, record in items
            if (kind == "turn" and record["turn_id"] in turns)
            or (kind == "memory" and record["memory_id"] in drawn)]


def export_file(index: Path, target, project=None) -> dict:
    """The whole memory as one file another PC or person can import: every memory and turn, in
    the shared log's format. Secrets were masked when saved; everything else is in it.

    `project` (a folder): only the conversations held in that folder or below it, and the
    memories drawn from them. Facts saved on request ("remember that ...") carry no folder and
    stay behind."""
    db = _cursor_db(index)
    db.row_factory = sqlite3.Row
    try:
        items = _history(db, set())
        if project is not None:
            items = _in_project(db, items, project)
    finally:
        db.close()
    target = Path(target).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text("".join(line + "\n" for line in _lines(items)), encoding="utf-8")
    os.replace(temporary, target)
    return {"file": str(target), "memories": sum(kind == "memory" for kind, _ in items),
            "turns": sum(kind == "turn" for kind, _ in items)}


def import_file(source, index: Path) -> dict:
    """Add an exported memory file to this index. What is already here is skipped, so importing
    the same file twice adds nothing."""
    import shutil
    import tempfile
    import uuid
    name = f"import-{uuid.uuid4().hex}.jsonl"      # the read cursor is kept per file name
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder) / "log").mkdir()
        shutil.copyfile(Path(source).expanduser(), Path(folder) / "log" / name)
        result = sync(Path(folder), index)
    db = _cursor_db(index)
    try:
        with db:
            db.execute("DELETE FROM brain_log_cursor WHERE source=?", (name,))
    finally:
        db.close()
    return result


def _cursor_db(index: Path) -> sqlite3.Connection:
    db = sqlite3.connect(index)
    db.executescript("""
      CREATE TABLE IF NOT EXISTS brain_log_cursor(
        source TEXT PRIMARY KEY, offset INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS brain_log_pending(
        line TEXT PRIMARY KEY);""")
    if "mark" not in {row[1] for row in db.execute("PRAGMA table_info(brain_log_cursor)")}:
        try:
            db.execute("ALTER TABLE brain_log_cursor ADD COLUMN mark TEXT")
        except sqlite3.OperationalError as error:    # another process added it just now
            if "duplicate column" not in str(error):
                raise
    return db


MARK_BYTES = 4096
BATCH_BYTES = 8 * 1024 * 1024        # read per sync step: a whole unread history ran out of RAM
BATCH_ROWS = 2000


def _mark(handle, end: int) -> str:
    """What a log holds just before `end`: a cursor whose mark no longer matches points into
    another file (the sync client put a different copy there, or another folder's log has the
    same name), and the log is read again from the start (imports are idempotent)."""
    handle.seek(max(0, end - MARK_BYTES))
    return hashlib.sha256(handle.read(min(end, MARK_BYTES))).hexdigest()


SEALED = "gme1:"
_KEYS = {}


def _line_key(url):
    """The key that seals this hub's lines, or None when it has none.

    The connection to a hub is TLS (since 0.7; before, it was not encrypted at all), and each
    line is sealed besides, so the hub's disk holds ciphertext only and a hub reached by a code
    from before TLS still never carries memory in clear. Each line is sealed (Fernet: AES with
    an HMAC) under a key derived from the password in the connection code, which every PC that
    joined holds and nobody else does.
    A joined PC finds the password in its URL; the hub PC in its own config."""
    import urllib.parse
    if not is_database(url):                      # a synced folder: its password, from lock_folder
        password = (_config().get("folder_keys") or {}).get(_folder_id(url))
    else:
        password = urllib.parse.urlsplit(url).password
        password = (urllib.parse.unquote(password) if password
                    else _config().get("share_password"))
    if not password:
        return None                               # a hub never shared: nothing leaves this PC
    return _fernet(password)


def _fernet(password):
    if password not in _KEYS:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"graph-mind sharing",
                   info=b"line key v1").derive(password.encode())
        _KEYS[password] = Fernet(base64.urlsafe_b64encode(raw))
    return _KEYS[password]


def _seal(url, lines):
    key = _line_key(url)
    return [SEALED + key.encrypt(line.encode()).decode() if key else line for line in lines]


def _unseal(url, line):
    return _opener(url)(line)


def _opener(url):
    """line -> the plain line, or None when it cannot be opened with this hub's key. On a hub
    with a key an unsealed line is refused: sharing seals the lines from before (share), and
    anyone able to write the hub's rows could otherwise add memories or forget notices without
    the key. The key is looked up once: per line, it read the config file each time (20,000
    lines of a folder took 10 s)."""
    from cryptography.fernet import InvalidToken
    key = _line_key(url)
    # a hub never shared: nothing was sealed. A folder may still hold lines from before it was
    # sealed (a PC that has not joined with the password yet): only the user's own cloud can
    # write there, so those are read
    plain_ok = key is None or not is_database(url)

    def opened(line):
        if not line.startswith(SEALED):
            return line if plain_ok else None
        if key is None:
            return None
        try:
            return key.decrypt(line[len(SEALED):].encode()).decode()
        except InvalidToken:
            return None
    return opened


def _folder_id(path) -> str:
    return str(Path(path).expanduser().resolve())


def _marker(shared) -> Path:
    """In a sealed folder: a check value that tells a PC whether its password is the right one."""
    return Path(shared).expanduser() / "sealed.json"


def lock_folder(shared, password=None):
    """Seal this synced folder's lines (Fernet, as a hub's) so the cloud service holding the
    folder sees only ciphertext. The first PC makes the password; every other PC must give it,
    and keeps it in its own config, never in the folder. Returns the password when it was made
    just now (to show the user once), otherwise None. ValueError: FOLDER_PASSWORD_NEEDED or
    FOLDER_PASSWORD_WRONG."""
    import secrets

    from cryptography.fernet import InvalidToken
    marker, made = _marker(shared), None
    keys = dict(_config().get("folder_keys") or {})
    if marker.exists():
        password = password or keys.get(_folder_id(shared))
        if not password:
            raise ValueError("FOLDER_PASSWORD_NEEDED")
        try:
            _fernet(password).decrypt(json.loads(marker.read_text("utf-8"))["check"].encode())
        except (InvalidToken, ValueError, KeyError):
            raise ValueError("FOLDER_PASSWORD_WRONG") from None
    elif keys.get(_folder_id(shared)) and not password:
        # the marker is gone (a sync conflict, a cleanup) but this PC knows the password: keep
        # it, or every line sealed so far would become unreadable everywhere
        password = keys[_folder_id(shared)]
    else:
        password = made = password or secrets.token_urlsafe(12)
    changes = {}
    if keys.get(_folder_id(shared)) != password:          # new to this PC: read it all again
        changes["folder_reread"] = _folder_id(shared)
    keys[_folder_id(shared)] = password
    _save({"folder_keys": keys, **changes})
    if not marker.exists():
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"check": _fernet(password).encrypt(b"turnbook").decode()}),
                          encoding="utf-8")
    _seal_own_log(shared)
    return made


def folder_password(shared):
    """This PC's password for a sealed folder (for the user's own terminal only), or None."""
    if shared is None or is_database(shared):
        return None
    return (_config().get("folder_keys") or {}).get(_folder_id(shared))


def _seal_own_log(shared) -> int:
    """Seal the lines this PC wrote to the folder before it was sealed. Only this PC writes its
    log, and appends take the same lock, so nothing appended meanwhile is lost."""
    import fasteners
    log = Path(shared).expanduser() / "log" / f"{log_name()}.jsonl"
    outbox = _outbox()
    outbox.parent.mkdir(parents=True, exist_ok=True)
    with fasteners.InterProcessLock(str(outbox.with_suffix(".lock"))):
        if not log.exists():
            return 0
        lines = log.read_text(encoding="utf-8").splitlines()
        plain = [line for line in lines if line.strip() and not line.startswith(SEALED)]
        if not plain:
            return 0
        seal = iter(_seal(shared, plain))           # one key lookup, not one per line
        sealed = [line if line.startswith(SEALED) else next(seal)
                  for line in lines if line.strip()]
        temporary = log.with_suffix(".sealing")
        temporary.write_text("\n".join(sealed) + "\n", encoding="utf-8")
        os.replace(temporary, log)
    return len(plain)


def _seal_existing(url) -> int:
    """Seal the lines the hub held before it was shared, so none crosses the network in clear."""
    if _line_key(url) is None:
        return 0
    with _pg(url) as connection:
        rows = connection.execute("SELECT seq, line FROM sharing WHERE line NOT LIKE %s",
                                  (SEALED + "%",)).fetchall()
        with connection.cursor() as cursor:
            cursor.executemany("UPDATE sharing SET line = %s WHERE seq = %s",
                               [(_seal(url, [line])[0], seq) for seq, line in rows])
        connection.commit()
    return len(rows)


# A hub that stops answering (Wi-Fi or VPN dropped mid-call, a writer holding the lock) must
# fail the call, so the lines are queued, not hang capture behind it: only connecting had a limit
PG_LIMITS = {"connect_timeout": 5, "keepalives": 1, "keepalives_idle": 30,
             "keepalives_interval": 10, "keepalives_count": 3,
             "options": "-c statement_timeout=120000 -c lock_timeout=30000"}


def _pg(url):
    import psycopg
    connection = psycopg.connect(url, autocommit=False, **PG_LIMITS)
    try:
        connection.execute("""CREATE TABLE IF NOT EXISTS sharing(
            seq BIGSERIAL PRIMARY KEY, device TEXT NOT NULL, line TEXT NOT NULL)""")
        # which hub this is, for the read cursor (one row; the first PC to look draws it)
        connection.execute("""CREATE TABLE IF NOT EXISTS sharing_hub(
            one BOOLEAN PRIMARY KEY DEFAULT true CHECK (one), id TEXT NOT NULL)""")
        import uuid
        connection.execute("INSERT INTO sharing_hub(id) VALUES (%s) ON CONFLICT DO NOTHING",
                           (uuid.uuid4().hex,))
        connection.commit()
    except BaseException:
        connection.close()
        raise
    return connection


_READERS: dict = {}


def close_readers():
    """Release cached read connections at shutdown (or before stopping a disposable test hub)."""
    connections = list(_READERS.values())
    _READERS.clear()
    for connection in connections:
        try:
            connection.close()
        except Exception:
            pass                        # shutdown must still close the other connections


atexit.register(close_readers)


def _read_rows(url, query, parameters):
    """A read on one connection kept open per hub: the sync before every answer paid a fresh
    connection (password handshake included) each time, 0.2 s of a 0.9 s answer. A connection
    the hub dropped (it restarted) is replaced once."""
    import psycopg
    for attempt in (1, 2):
        connection = _READERS.get(url)
        if connection is None or connection.closed:
            connection = _READERS[url] = _pg(url)
            connection.autocommit = True
        try:
            return connection.execute(query, parameters).fetchall()
        except psycopg.OperationalError:
            if _READERS.get(url) is connection:
                _READERS.pop(url, None)
            connection.close()
            if attempt == 2:
                raise


_HUB_DOWN: dict = {}


def _outbox() -> Path:
    from turnbook.paths import turnbook_home
    return turnbook_home() / "outbox.jsonl"


def _append_or_queue(url, send, lines) -> int:
    """Append to the hub or folder; when it cannot be reached, keep the lines in a local outbox
    and send them with the next append that gets through. The hub was down for four days once and
    every turn of those days stayed on the PC that captured it until it was sent by hand."""
    url = str(url)
    import fasteners
    outbox = _outbox()
    outbox.parent.mkdir(parents=True, exist_ok=True)
    with fasteners.InterProcessLock(str(outbox.with_suffix(".lock"))):
        # down a minute ago: queue without trying (each turn re-read the whole outbox and waited
        # out another connection attempt while the hub was off for days)
        down = time.monotonic() < _HUB_DOWN.get(url, 0.0)
        waiting = ([line for line in outbox.read_text(encoding="utf-8").split("\n")
                    if line.strip()]
                   if outbox.exists() and not down else [])
        try:
            if down:
                raise ConnectionError("the hub was unreachable a moment ago")
            send(url, waiting + lines)
            _HUB_DOWN.pop(url, None)
        except Exception:
            if not down:
                _HUB_DOWN[url] = time.monotonic() + 60
            # a line cut off by a crash or a full disk would swallow the next one: end it first
            torn = False
            if outbox.exists() and outbox.stat().st_size:
                with outbox.open("rb") as handle:
                    handle.seek(-1, os.SEEK_END)
                    torn = handle.read(1) != b"\n"
            with outbox.open("a", encoding="utf-8") as stream:
                stream.write(("\n" if torn else "") + "\n".join(lines) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return len(lines)                     # queued, not lost
        if waiting:
            outbox.unlink()
    return len(lines)


def _pg_append(url, lines) -> int:
    with _pg(url) as connection:
        # one writer at a time: a row can then never commit after a later sequence number has
        # been read, which would make a reader's cursor step over it for good
        connection.execute("SELECT pg_advisory_xact_lock(4242)")
        with connection.cursor() as cursor:
            cursor.executemany("INSERT INTO sharing(device, line) VALUES (%s, %s)",
                               [(device_name(), line) for line in _seal(url, lines)])
        connection.commit()
    return len(lines)


def devices(shared) -> list[str]:
    """Every device that has written to this brain."""
    if is_database(shared):
        with _pg(shared) as connection:
            return [row[0] for row in connection.execute(
                "SELECT DISTINCT device FROM sharing ORDER BY device")]
    return sorted(p.stem for p in (Path(shared) / "log").glob("*.jsonl"))


def _new_lines(db: sqlite3.Connection, shared) -> tuple[list[str], list[tuple], bool]:
    """Complete lines added to any device's log since this machine last read it, at most a
    batch of them; the cursors to save once they are imported (saved before, a crash in
    between skipped them for good); and whether more is waiting."""
    cursors = []
    if is_database(shared):
        # the cursor names the hub: as plain "postgres", joining hub B after reading hub A up to
        # row 100 skipped B's first 100 rows. A cursor from before is taken as this hub's.
        source = "postgres:" + _read_rows(shared, "SELECT id FROM sharing_hub", ())[0][0]
        row = db.execute("SELECT offset FROM brain_log_cursor WHERE source=?",
                         (source,)).fetchone()
        if row is None:
            row = db.execute("SELECT offset FROM brain_log_cursor WHERE source='postgres'"
                             ).fetchone()
        rows = _read_rows(shared, "SELECT seq, line FROM sharing WHERE seq > %s ORDER BY seq "
                          "LIMIT %s", (row[0] if row else 0, BATCH_ROWS))
        if rows:
            cursors.append((source, rows[-1][0], None))
        opener = _opener(shared)
        opened = [opener(line) for _, line in rows]
        return [line for line in opened if line is not None], cursors, len(rows) == BATCH_ROWS
    lines, budget, more = [], BATCH_BYTES, False
    opener = _opener(shared)
    if _marker(shared).exists() and _line_key(shared) is None:
        return [], [], False            # sealed and no password yet: read nothing, move no cursor
    if _config().get("folder_reread") == _folder_id(shared):
        # this PC just got the folder's password: an older version had read past the sealed
        # lines as corrupt ones, so read every log again (imports skip what is already here)
        db.execute("DELETE FROM brain_log_cursor WHERE source NOT LIKE 'postgres%'")
        db.commit()
        _save({"folder_reread": None})
    for log in sorted((Path(shared) / "log").glob("*.jsonl")):
        if budget <= 0:
            more = True
            break
        row = db.execute("SELECT offset, mark FROM brain_log_cursor WHERE source=?",
                         (log.name,)).fetchone()
        start, mark = row if row else (0, None)
        size = log.stat().st_size
        with log.open("rb") as handle:
            # shorter: replaced by the sync client; a mark that differs: another file in its
            # place, also when it is as long or longer (both used to be read on from the offset)
            if size < start or (start and mark and _mark(handle, start) != mark):
                start = 0
            if size == start:
                continue
            handle.seek(start)
            data = handle.read(budget)
            if start + len(data) < size:
                data += handle.readline()           # the line the batch ends in, whole
            complete = data[:data.rfind(b"\n") + 1]  # an unfinished last line waits for its newline
            end = start + len(complete)
            more = more or start + len(data) < size  # stopped short (then on a whole line)
            budget -= len(complete)
            if complete:
                cursors.append((log.name, end, _mark(handle, end)))
        lines += [line for line in (opener(line.strip()) for line in
                                    complete.decode("utf-8", "replace").splitlines()
                                    if line.strip()) if line is not None]
    return lines, cursors, more


def sync(shared: Path, index: Path) -> dict:
    """Bring this machine's index up to date with every device's log, a batch at a time: the
    whole unread history read at once ran a fresh PC out of memory before anything landed."""
    from turnbook.forget import retry_deletions
    retry_deletions(index, wait=True)
    total = {"imported": 0, "conflicts": 0, "waiting": 0}
    while True:
        result, more = _sync_batch(shared, index)
        total = {"imported": total["imported"] + result["imported"],
                 "conflicts": total["conflicts"] + result["conflicts"],
                 "waiting": result["waiting"]}
        if not more:
            return total


def _land(items, brain, turns):
    """Import these; returns (imported, conflicts, the ones still waiting for their target)."""
    imported, conflicts = 0, 0
    # A supersession can arrive before the memory it replaces when two devices' logs are read
    # in one batch; keep retrying while anything is still landing.
    while items:
        progress, retry = False, []
        for item, line in items:
            try:
                if item["kind"] == "memory":
                    outcome = brain.remember(item["record"])
                else:
                    outcome = turns.record(item["record"])
            except (sqlite3.OperationalError, OSError):
                raise                       # locked or I/O: retried, the cursor stays
            except Exception as error:      # a malformed line must not stop sync
                if str(error) == "SUPERSEDED_LOCAL_MEMORY_NOT_FOUND":
                    retry.append((item, line))
                    continue
                conflicts += 1                       # e.g. an id reused with other content
                continue
            progress = True
            imported += outcome.get("disposition") not in ("ALREADY_RECORDED", "FORGOTTEN")
        items = retry
        if not progress:
            break
    return imported, conflicts, items


def _sync_batch(shared: Path, index: Path) -> tuple[dict, bool]:
    from turnbook.conversation_memory import ConversationMemoryStore
    from turnbook.local_brain import LocalBrainStore

    db = _cursor_db(index)
    try:
        pending = [row[0] for row in db.execute("SELECT line FROM brain_log_pending")]
        fresh, cursors, more = _new_lines(db, shared)
        lines = pending + fresh
        if not lines and not cursors:
            return {"imported": 0, "conflicts": 0, "waiting": 0}, False
        items = []
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue                                  # a corrupt line is skipped, not fatal
            # the shape is checked here: a "written_at": 1 or a forget without its record
            # raised below on every sync, and no cursor ever moved again
            if (isinstance(item, dict) and item.get("kind") in KINDS
                    and isinstance(item.get("record"), dict)
                    and isinstance(item.get("written_at", ""), str)):
                items.append((item, line))
        items.sort(key=lambda pair: pair[0].get("written_at", ""))
        forgets = [item["record"] for item, _ in items if item.get("kind") == "forget"]
        items = [(item, line) for item, line in items if item.get("kind") != "forget"]
        with LocalBrainStore(index) as brain, ConversationMemoryStore(index) as turns:
            imported, conflicts, waiting = _land(items, brain, turns)
            if forgets:                     # after the imports: a forgotten turn may be in them
                from turnbook.forget import forget_ids, scrub_shared
                for record in forgets:      # here too: this PC's outbox and staged lines
                    forget_ids(record, index)
                try:
                    # the hub too: when the PC that forgot could not reach it, its scrub never
                    # ran again and the forgotten text stayed in the hub's rows
                    scrub_shared(shared, {
                        "turn_ids": [t for r in forgets for t in r.get("turn_ids") or []],
                        "memory_ids": [m for r in forgets for m in r.get("memory_ids") or []]})
                except Exception:
                    pass                    # unreachable now: the next PC to read it tries
                if waiting:                 # a replacement of a memory just forgotten lands now
                    more_imported, more_conflicts, waiting = _land(waiting, brain, turns)
                    imported, conflicts = imported + more_imported, conflicts + more_conflicts
        with db:                            # imports are idempotent: a crash before this re-reads
            if any(source.startswith("postgres:") for source, _, _ in cursors):
                # here, not when read: an uncommitted delete held the write lock the imports wait on
                db.execute("DELETE FROM brain_log_cursor WHERE source='postgres'")
            db.executemany("INSERT OR REPLACE INTO brain_log_cursor(source, offset, mark) "
                           "VALUES(?,?,?)", cursors)
            db.execute("DELETE FROM brain_log_pending")
            db.executemany("INSERT OR IGNORE INTO brain_log_pending VALUES(?)",
                           [(line,) for _, line in waiting])
        return {"imported": imported, "conflicts": conflicts, "waiting": len(waiting)}, more
    finally:
        db.close()


def _self_check():
    import tempfile

    root = Path(tempfile.mkdtemp())
    shared, desk_index, lap_index = root / "OneDrive" / "Graph-MIND", root / "desk.sqlite", \
        root / "lap.sqlite"
    from turnbook.conversation_memory import ConversationMemoryStore
    from turnbook.local_brain import LocalBrainStore

    def memory(mid, text, when, supersedes=None):
        return {"memory_id": mid, "scope": "global", "memory_type": "fact", "title": text,
                "content": text, "effective_at": when, "known_at": when, "actor": "assistant",
                "tags": [], "entities": ["사는 곳"],
                "provenance": {"source_type": "test", "source_ref": "self-check"},
                "supersedes_memory_id": supersedes}

    os.environ["TURNBOOK_DEVICE"] = "desktop"
    append(shared, "memory", memory("m1", "사는 곳: 서울", "2026-03-01T09:00:00"))
    append(shared, "turn", {"turn_id": "t1", "client": "claude-code", "client_session_id": "s",
                            "scope": "global", "role": "user",
                            "content_redacted": "콩이를 제주도에 데려갔어", "raw_sha256": "x",
                            "source_path": "p", "happened_at": "2026-03-01T09:00:00",
                            "known_at": "2026-03-01T09:00:00", "source_ordinal": 1})
    os.environ["TURNBOOK_DEVICE"] = "laptop"
    # the laptop has not seen the desktop yet, so this lands in its log first in file order
    append(shared, "memory", memory("m2", "사는 곳: 부산", "2026-10-01T09:00:00", supersedes="m1"))

    first = sync(shared, desk_index)
    assert first == {"imported": 3, "conflicts": 0, "waiting": 0}, first
    assert sync(shared, desk_index)["imported"] == 0, "re-sync imports nothing twice"

    with LocalBrainStore(desk_index) as brain:
        found = brain.recall("사는 곳", as_of="2026-12-01T00:00:00")
    assert [m["memory_id"] for m in found["matches"]] == ["m2"], found
    with ConversationMemoryStore(desk_index) as turns:
        assert turns.search("콩이")["status"] == "KNOWN", "the turn from the other device arrived"

    log = shared / "log" / "laptop.jsonl"
    with log.open("a", encoding="utf-8") as stream:
        stream.write('{"kind": "memory", "half": ')       # a line still syncing: no newline yet
    assert sync(shared, lap_index)["imported"] == 3, "the torn line is not read"
    assert sync(shared, lap_index)["conflicts"] == 0

    os.environ.pop("TURNBOOK_DEVICE")
    print("sharing self-check ok")


if __name__ == "__main__":
    _self_check()
