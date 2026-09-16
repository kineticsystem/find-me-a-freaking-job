"""Command line entry point: python -m jobfinder <command>."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import db, discovery, opencode
from .pipeline import profile
from .config import ROOT, settings


def _setup_logging(verbose: bool) -> None:
    from logging.handlers import RotatingFileHandler

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler(); console.setFormatter(fmt); root.addHandler(console)
    # Also to runs/app.log on the host, so the trail survives a container
    # restart -- docker logs die with the container.
    try:
        logdir = settings().paths.resolve("runs"); logdir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(logdir / "app.log", maxBytes=5_000_000, backupCount=3)
        fh.setFormatter(fmt); root.addHandler(fh)
    except Exception as exc:  # noqa: BLE001 - logging must never stop the app
        root.warning("no file log: %s", exc)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def cmd_init(args: argparse.Namespace) -> int:
    db.init_db()
    added = discovery.seed_from_config()
    print(f"database ready at {db.db_path()}")
    print(f"{added} source(s) registered from config/sources.yaml")
    print("next: open the web app, create your account and upload your CV under Settings")
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
        log_config=None,   # keep our handlers (console + runs/app.log) for uvicorn's access log too
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

    db.init_db()
    cand = profile.load(args.user)
    print(f"# {cand.name}: {cand.readiness}")
    if not cand.ready:
        return 1
    workdir = Path(settings().paths.resolve("runs")) / "profile-cli"
    digest = profile.load_digest(cand, workdir, force=args.force)
    print(digest.model_dump_json(indent=2))
    return 0


def cmd_create_user(args: argparse.Namespace) -> int:
    """Admin's way in without the UI. The first account claims the pre-login
    default user so existing decisions and scores are kept."""
    import getpass

    from . import auth
    from .config import DEFAULT_USER_ID

    db.init_db()
    email = args.email.strip().lower()
    if db.get_user_by_email(email):
        print(f"{email} already exists", file=sys.stderr)
        return 1
    password = args.password or getpass.getpass("password: ")
    if len(password) < 8:
        print("password must be at least 8 characters", file=sys.stderr)
        return 1
    if not args.password and password != getpass.getpass("again: "):
        print("passwords differ", file=sys.stderr)
        return 1
    first = not db.any_user_can_login()
    if first:
        db.claim_user(DEFAULT_USER_ID, email, auth.hash_password(password), is_admin=True)
        uid = DEFAULT_USER_ID
    else:
        uid = db.create_user(email, auth.hash_password(password), is_admin=args.admin)
    role = "admin" if (first or args.admin) else "user"
    print(f"created {role} {email} (id {uid})" + (" — owns the existing data" if first else ""))
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

    settings()
    db.init_db()
    preferences()   # imports a pending config/preferences.yaml into the database
    print("configuration OK: config/settings.yaml is valid; preferences are in the database")
    return 0


def cmd_seed_demo(args: argparse.Namespace) -> int:
    """Fill an EMPTY database with fictional postings and scores, so a test
    instance has something to show without the network or the model."""
    import random

    from .models import NormalizedJob, fingerprint

    db.init_db()
    with db.connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]:
            print("database is not empty; seed-demo only fills an empty one", file=sys.stderr)
            return 1
    rng = random.Random(42)
    companies = ["Acme Robotics", "Globex", "Initech", "Umbrella Software", "Hooli", "Vandelay Systems", "Stark Labs", "Wayne Tech"]
    titles = ["Senior Software Engineer", "Backend Engineer", "Platform Engineer", "C++ Developer", "Python Developer",
              "Staff Engineer, Infrastructure", "Full Stack Engineer", "Site Reliability Engineer", "Embedded Software Engineer"]
    locations = [("Remote, Europe", "remote"), ("Berlin, Germany", "hybrid"), ("Dublin, Ireland", "onsite"), ("Remote, US", "remote"), ("Amsterdam", "hybrid")]
    # The admin gets a CV and notes so the seeded scores are under their real criteria.
    from .config import DEFAULT_USER_ID
    db.save_cv(DEFAULT_USER_ID, "cv.md", b"# Jane Doe\nSenior engineer. Python, C++, Kubernetes. Ten years of services.\n",
               "--- cv.md ---\n# Jane Doe\nSenior engineer. Python, C++, Kubernetes. Ten years of services.")
    db.save_notes(DEFAULT_USER_ID, "I want remote backend work at a product company. No agencies.\n")
    criteria = profile.load(DEFAULT_USER_ID).criteria
    n = 0
    with db.connect() as conn:
        for i in range(40):
            company = companies[i % len(companies)]; title = titles[i % len(titles)]
            location, remote = locations[i % len(locations)]
            job = NormalizedJob(
                fingerprint=fingerprint(company, title, location), source_id="demo",
                company=company, title=title, location=location, remote_type=remote,  # type: ignore[arg-type]
                url=f"https://example.com/{company.lower().replace(' ', '-')}/jobs/{i}", apply_url="",
                description=f"{company} is hiring a {title} in {location}. You will build and run services in Python and C++, "
                            f"work with Kubernetes and PostgreSQL, and own what you ship.\n\n• 5+ years of experience\n• Strong fundamentals\n• Fluent English",
                salary_raw="EUR 80000 - 110000" if i % 3 == 0 else "", tags=["python", "c++"],
            )
            job_id, _ = db.upsert_job(conn, job); n += 1
            if i % 5 != 4:                                  # most are scored, a few are not
                score = rng.randint(20, 95)
                db.record_evaluation(conn, job_id=job_id, run_id=None, stage="triage", criteria_hash=criteria,
                                     score=score, verdict="strong" if score >= 70 else "maybe" if score >= 45 else "reject",
                                     rationale="demo score", model="demo")
                if score >= 70:
                    db.record_evaluation(conn, job_id=job_id, run_id=None, stage="deepdive", criteria_hash=criteria,
                                         score=score, verdict="strong", model="demo", eligible=True,
                                         summary=f"A {title.lower()} role at {company}; a demo posting.",
                                         eligibility="Demo: eligible.", salary=job.salary_raw, tech_stack=["Python", "C++"],
                                         concerns=["demo data"], rationale="demo")
    # Accounts for the browser suite: the admin owns the seeded scores.
    from . import auth
    if not db.any_user_can_login():
        db.claim_user(DEFAULT_USER_ID, "admin@example.com", auth.hash_password("demo-admin-password"), is_admin=True)
        db.create_user("user@example.com", auth.hash_password("demo-user-password"))
    # Both follow the demo source (so they see its postings); the registry
    # switch stays off so no scan ever fetches it.
    db.upsert_source({"id": "demo", "type": "remoteok", "enabled": False, "company": "Demo"}, origin="user")
    for u in db.list_users():
        db.follow_source(u["id"], "demo", True)
    db.set_source_enabled("demo", False)
    print(f"seeded {n} fictional postings under criteria {criteria}; accounts admin@example.com / user@example.com")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"root:      {ROOT}")
    print(f"database:  {db.db_path()} ({'exists' if db.db_path().exists() else 'missing'})")
    print(f"opencode:  {opencode.health_check()}")
    try:
        db.init_db()
        for u in db.list_users():
            c = profile.load(u["id"])
            r = c.readiness
            print(f"user {u['id']:<3} {c.name:<30} cv {len(c.cv_text):>6} chars · notes {len(c.notes):>5} chars · "
                  f"preferences {'ok' if r['preferences'] else 'MISSING'} · {'ready' if r['ready'] else 'NOT READY'}")
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

    prof = sub.add_parser("profile", help="show (or rebuild) a user's CV digest")
    prof.add_argument("--user", type=int, default=1, help="user id (default 1)")
    prof.add_argument("--force", action="store_true", help="rebuild even if cached")
    prof.set_defaults(func=cmd_profile)

    sub.add_parser("doctor", help="check the environment").set_defaults(func=cmd_doctor)
    sub.add_parser("check", help="validate the config files; exit 2 with the reason if not").set_defaults(func=cmd_check)
    sub.add_parser("seed-demo", help="fill an empty database with fictional postings (for a test instance)").set_defaults(func=cmd_seed_demo)
    user = sub.add_parser("create-user", help="add an account (the first one becomes the admin and owns existing data)")
    user.add_argument("email")
    user.add_argument("--admin", action="store_true")
    user.add_argument("--password", help="for scripts; interactive prompt otherwise")
    user.set_defaults(func=cmd_create_user)
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
