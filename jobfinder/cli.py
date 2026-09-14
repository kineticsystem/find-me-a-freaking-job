"""Command line entry point: python -m jobfinder <command>."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import db, discovery, opencode
from .config import ROOT, settings


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def cmd_init(args: argparse.Namespace) -> int:
    db.init_db()
    added = discovery.seed_from_config()
    print(f"database ready at {db.db_path()}")
    print(f"{added} source(s) registered from config/sources.yaml")
    print(f"drop your CV (cv.pdf / cv.md) in {settings().paths.resolve('profile')}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .pipeline.run import run_once

    db.init_db()
    stats = run_once(skip_llm=args.no_llm)
    print(json.dumps(stats, indent=2, default=str))
    return 1 if stats.get("error") else 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    cfg = settings()
    db.init_db()
    uvicorn.run(
        "jobfinder.api:app",
        host=args.host or cfg.api.host,
        port=args.port or cfg.api.port,
        log_level="debug" if args.verbose else "info",
    )
    return 0


def cmd_jobs(args: argparse.Namespace) -> int:
    rows, _ = db.list_jobs(min_score=args.min_score, status=args.status, limit=args.limit)
    if not rows:
        print("no jobs match (has a run finished yet?)")
        return 0
    for job in rows:
        score = job.get("score")
        print(f"[{job['id']:>5}] {str(score if score is not None else '--'):>3} "
              f"{job['title'][:48]:<48} {job['company'][:22]:<22} "
              f"{(job['location'] or '')[:18]:<18} {job['status']}")
        if args.long and job.get("summary"):
            print(f"        {job['summary']}")
            print(f"        {job.get('apply_url') or job['url']}")
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    for run in db.list_runs(args.limit):
        s = run["stats"] or {}
        print(f"#{run['id']:<4} {run['status']:<7} {run['started_at']} "
              f"raw={s.get('raw', '-')} new={s.get('new', '-')} "
              f"triaged={s.get('triaged', '-')} deep={s.get('deepdived', '-')} "
              f"{run.get('error') or ''}")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    for src in db.list_sources():
        flag = "on " if src["enabled"] else "off"
        print(f"{flag} {src['id']:<28} {src['type']:<15} {src['origin']:<11} "
              f"found={src['jobs_found']:<6} fails={src['fail_count']} {src['last_error'] or ''}")
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .pipeline.profile import load_digest

    workdir = Path(settings().paths.resolve("runs")) / "profile-cli"
    digest = load_digest(workdir, force=args.force)
    print(digest.model_dump_json(indent=2))
    return 0


def cmd_clean_descriptions(args: argparse.Namespace) -> int:
    """Re-run the HTML stripper over stored descriptions (after a fix to it)."""
    from .textutil import html_to_text

    changed = 0
    with db.connect() as conn:
        rows = conn.execute("SELECT id, description FROM jobs WHERE description LIKE '%<%>%' OR description LIKE '%&lt;%'").fetchall()
        for row in rows:
            cleaned = html_to_text(row["description"] or "")
            if cleaned != row["description"]:
                conn.execute("UPDATE jobs SET description = ? WHERE id = ?", (cleaned, row["id"]))
                changed += 1
    print(f"{len(rows)} descriptions looked like HTML; {changed} rewritten")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Validate the config files and nothing else. main() has already exited
    with the errors if there were any, so reaching here means they are fine."""
    from .config import preferences, settings

    settings(); preferences()
    print("configuration OK: config/settings.yaml and config/preferences.yaml are valid")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .pipeline.profile import cv_text, profile_dir

    print(f"root:      {ROOT}")
    print(f"database:  {db.db_path()} ({'exists' if db.db_path().exists() else 'missing'})")
    print(f"opencode:  {opencode.health_check()}")
    cv = cv_text()
    print(f"cv:        {len(cv)} chars from {profile_dir()}"
          + ("" if cv else "  <-- EMPTY, add a CV"))
    try:
        print(f"stats:     {db.stats()}")
    except Exception as exc:
        print(f"stats:     unavailable ({exc}); run `init` first")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobfinder", description="agentic job search")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and register configured sources").set_defaults(func=cmd_init)

    run = sub.add_parser("run-once", help="execute one pipeline run now")
    run.add_argument("--no-llm", action="store_true",
                     help="fetch and store only; skip every opencode session")
    run.set_defaults(func=cmd_run)

    serve = sub.add_parser("serve", help="run the scheduler and HTTP API")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=cmd_serve)

    jobs = sub.add_parser("jobs", help="list stored jobs")
    jobs.add_argument("--min-score", type=int, default=0)
    jobs.add_argument("--status", choices=["new", "shortlisted", "applied", "dismissed", "archived"])
    jobs.add_argument("--limit", type=int, default=40)
    jobs.add_argument("-l", "--long", action="store_true")
    jobs.set_defaults(func=cmd_jobs)

    runs = sub.add_parser("runs", help="show recent runs")
    runs.add_argument("--limit", type=int, default=15)
    runs.set_defaults(func=cmd_runs)

    sub.add_parser("sources", help="show every registered source").set_defaults(func=cmd_sources)

    prof = sub.add_parser("profile", help="show (or rebuild) the CV digest")
    prof.add_argument("--force", action="store_true", help="rebuild even if cached")
    prof.set_defaults(func=cmd_profile)

    sub.add_parser("doctor", help="check the environment").set_defaults(func=cmd_doctor)
    sub.add_parser("check", help="validate the config files; exit 2 with the reason if not").set_defaults(func=cmd_check)
    sub.add_parser("clean-descriptions", help="re-strip HTML from stored job descriptions").set_defaults(func=cmd_clean_descriptions)
    return p


def _check_config_or_exit() -> None:
    """Every command starts here. A broken config file is reported in full
    and nothing else runs: better a stopped process than one working on
    defaults it was never given."""
    from .config import check_config, ensure_user_files

    for rel in ensure_user_files():
        print(f"created {rel} from its example; edit it in the web app", file=sys.stderr)
    errors = check_config()
    if not errors:
        return
    print("\nCannot start: a configuration file is not usable.\n", file=sys.stderr)
    for err in errors:
        print(f"  {err.message}", file=sys.stderr)
    print(
        "\nFix the file and start again. Nothing has been started or changed.\n"
        "(A file saved from the web app is always valid; this happens after editing by hand.)\n",
        file=sys.stderr,
    )
    raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    _check_config_or_exit()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
