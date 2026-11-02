"""Command line control for the Turnbook automatic capture service."""
from __future__ import annotations

import argparse
import json
import os

from turnbook.automatic_capture import (
    AutomaticCaptureService,
    _atomic_json,
    default_config_path,
    default_service_state_dir,
    load_config,
    make_config,
)


def _emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    import sys
    if list(argv if argv is not None else sys.argv[1:])[-1:] == ["run"]:
        # the service starts at login, before any app holds a file: the moment a memory from
        # before 0.11 can move to ~/.turnbook (before the paths below are worked out)
        from turnbook.paths import link_old_home, move_old_home
        move_old_home()
        link_old_home()
    parser = argparse.ArgumentParser(description="Turnbook automatic capture v0.2")
    parser.add_argument("--config", default=str(default_config_path()))
    parser.add_argument("--state-dir", default=str(default_service_state_dir()))
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure")
    configure.add_argument("--workspace", required=True)
    configure.add_argument("--project", required=True)
    configure.add_argument("--session", required=True)
    configure.add_argument("--sessions-root", required=True)
    configure.add_argument("--db")
    configure.add_argument("--poll-seconds", type=int, default=5)
    configure.add_argument("--code-poll-seconds", type=int, default=120)
    commands.add_parser("baseline")
    commands.add_parser("once")
    commands.add_parser("run")
    commands.add_parser("status")
    # the one brain's hand, off by default (worker_runner.py)
    commands.add_parser("workers-enable")
    disable = commands.add_parser("workers-disable")
    disable.add_argument("--stop", action="store_true", help="also stop a running worker")
    commands.add_parser("workers-status")
    grant = commands.add_parser("workers-grant")
    grant.add_argument("--workspace", required=True)
    grant.add_argument("--task", required=True)
    args = parser.parse_args(argv)
    if args.command.startswith("workers-"):
        from turnbook.mcp_server import configured_db_path
        from turnbook.worker_runner import cli
        db = (load_config(args.config)["db_path"] if os.path.isfile(args.config)
              else str(configured_db_path()))
        _emit(cli(args.command, args, args.state_dir, db))
        return 0
    if args.command == "configure":
        config = make_config(workspace=args.workspace, project_id=args.project,
            session_id=args.session, sessions_root=args.sessions_root,
            poll_seconds=args.poll_seconds, code_poll_seconds=args.code_poll_seconds,
            db_path=args.db)
        _atomic_json(args.config, config)
        _emit({"status": "CONFIGURED", "config": os.path.abspath(args.config),
               "value": config})
        return 0
    if args.command == "status":
        path = os.path.join(args.state_dir, "status.json")
        if not os.path.isfile(path):
            _emit({"status": "NOT_STARTED", "state": path})
        else:
            with open(path, encoding="utf-8") as handle:
                _emit(json.load(handle))
        return 0
    service = AutomaticCaptureService(load_config(args.config), state_dir=args.state_dir)
    if args.command == "baseline":
        result = service.baseline()
    elif args.command == "once":
        result = service.run_once()
    else:
        service.run_forever()
        return 0
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
