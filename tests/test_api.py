import pytest
from conftest import make_candidate
"""Contract the web UI relies on."""


def test_list_defaults_to_score_order_and_hides_nothing_unarchived(client, seeded):
    r = client.get("/jobs")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    ids = [j["id"] for j in body["jobs"]]
    # deep-dive score (92) beats triage (55) beats unevaluated
    assert ids == [seeded["a"], seeded["b"], seeded["c"]]
    top = body["jobs"][0]
    assert top["score"] == 92 and top["summary"] == "Great C++ role"
    assert top["tech_stack"] == ["C++", "Qt"]  # decoded from JSON text


def test_list_fields_are_always_lists(client, seeded):
    """Unevaluated jobs have NULL tech_stack/concerns in SQL; the UI gets []."""
    for job in client.get("/jobs").json()["jobs"]:
        assert isinstance(job["tags"], list)
        assert isinstance(job["tech_stack"], list)
        assert isinstance(job["concerns"], list)
    unevaluated = client.get(f"/jobs/{seeded['c']}").json()
    assert unevaluated.get("score") is None
    assert unevaluated["tech_stack"] == [] and unevaluated["concerns"] == []


def test_deepdive_supersedes_triage_score(client, seeded):
    job = client.get(f"/jobs/{seeded['a']}").json()
    assert [e["stage"] for e in job["evaluations"]] == ["deepdive", "triage"]
    listed = client.get("/jobs", params={"min_score": 90}).json()
    assert [j["id"] for j in listed["jobs"]] == [seeded["a"]]


def test_text_filter_matches_title_company_and_location(client, seeded):
    assert client.get("/jobs", params={"q": "c++"}).json()["total"] == 1
    assert client.get("/jobs", params={"q": "globex"}).json()["total"] == 1
    assert client.get("/jobs", params={"q": "amsterdam"}).json()["total"] == 1
    assert client.get("/jobs", params={"q": "nothing-matches"}).json()["total"] == 0


def test_remote_source_and_sort_filters(client, seeded):
    assert client.get("/jobs", params={"remote": "hybrid"}).json()["total"] == 1
    assert client.get("/jobs", params={"source": "gh-globex"}).json()["total"] == 1
    assert client.get("/jobs", params={"remote": "bogus"}).status_code == 400
    by_company = client.get("/jobs", params={"sort": "company"}).json()["jobs"]
    assert [j["company"] for j in by_company] == ["Acme", "Globex", "Initech"]


def test_pagination_reports_total(client):
    page = client.get("/jobs", params={"limit": 2, "offset": 0}).json()
    assert page["count"] == 2 and page["total"] == 3
    page2 = client.get("/jobs", params={"limit": 2, "offset": 2}).json()
    assert page2["count"] == 1 and page2["total"] == 3


def test_state_change_round_trips(client, seeded):
    r = client.patch(f"/jobs/{seeded['b']}/state", json={"status": "shortlisted", "notes": "call them"})
    assert r.status_code == 200
    job = client.get(f"/jobs/{seeded['b']}").json()
    assert job["status"] == "shortlisted" and job["notes"] == "call them"
    assert client.get("/jobs", params={"status": "shortlisted"}).json()["total"] == 1
    assert client.patch("/jobs/999999/state", json={"status": "applied"}).status_code == 404
    assert client.patch(f"/jobs/{seeded['b']}/state", json={"status": "nope"}).status_code == 422


def test_archive_hides_by_default_and_is_reversible(client, seeded):
    r = client.patch(f"/jobs/{seeded['c']}/state", json={"status": "archived"})
    assert r.status_code == 200
    assert client.get("/jobs").json()["total"] == 2
    hidden = client.get("/jobs", params={"hidden": "true"}).json()
    assert hidden["total"] == 1 and hidden["jobs"][0]["status"] == "archived"   # only the hidden ones
    assert client.get("/jobs", params={"status": "archived"}).json()["total"] == 1
    client.patch(f"/jobs/{seeded['c']}/state", json={"status": "new"})
    assert client.get("/jobs").json()["total"] == 3


def test_bulk_archive_by_ids_and_by_age(client, seeded):
    r = client.post("/jobs/archive", json={"ids": [seeded["a"], seeded["c"]]})
    assert r.status_code == 200 and r.json()["archived"] == 2
    assert client.get("/jobs").json()["total"] == 1
    # age rule: nothing is older than 30 days in a fresh db
    assert client.post("/jobs/archive", json={"older_than_days": 30}).json()["archived"] == 0
    # age rule with 0 days archives every remaining 'new' job seen before now
    r = client.post("/jobs/archive", json={"older_than_days": 0})
    assert r.json()["archived"] == 1
    assert client.get("/jobs").json()["total"] == 0
    assert client.post("/jobs/archive", json={}).status_code == 400


def test_bulk_archive_by_age_spares_shortlisted(client, seeded):
    client.patch(f"/jobs/{seeded['a']}/state", json={"status": "shortlisted"})
    client.post("/jobs/archive", json={"older_than_days": 0})
    left = client.get("/jobs").json()
    assert [j["id"] for j in left["jobs"]] == [seeded["a"]]


def test_delete_is_permanent_and_cascades(client, seeded):
    assert client.delete(f"/jobs/{seeded['a']}").status_code == 200
    assert client.get(f"/jobs/{seeded['a']}").status_code == 404
    assert client.delete(f"/jobs/{seeded['a']}").status_code == 404
    assert client.get("/jobs").json()["total"] == 2
    from jobfinder import db
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM evaluations WHERE job_id=?", (seeded["a"],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM user_state WHERE job_id=?", (seeded["a"],)).fetchone()[0] == 0


def test_facets_reflect_state(client, seeded):
    client.patch(f"/jobs/{seeded['a']}/state", json={"status": "applied"})
    f = client.get("/jobs/facets").json()
    assert f["status"] == {"applied": 1, "new": 2}
    assert f["remote"] == {"hybrid": 1, "onsite": 1, "remote": 1}
    assert f["source"]["test"] == 2 and f["source"]["gh-globex"] == 1


def test_archived_jobs_are_skipped_by_the_pipeline(seeded, criteria):
    from jobfinder import db
    db.set_user_state(seeded["c"], "archived")
    pending = db.jobs_needing("triage", criteria, 100)
    assert [r["id"] for r in pending] == []  # a, b evaluated; c archived
    db.set_user_state(seeded["c"], "new")
    assert [r["id"] for r in db.jobs_needing("triage", criteria, 100)] == [seeded["c"]]


def test_dismiss_with_reason_round_trips_and_hides(client, seeded):
    r = client.patch(f"/jobs/{seeded['b']}/state", json={"status": "dismissed", "reason": "agency work"})
    assert r.status_code == 200
    job = client.get(f"/jobs/{seeded['b']}").json()
    assert job["status"] == "dismissed" and job["reason"] == "agency work"
    # hidden from the active list, visible by status and in the hidden view
    assert client.get("/jobs").json()["total"] == 2
    assert client.get("/jobs", params={"status": "dismissed"}).json()["jobs"][0]["reason"] == "agency work"
    assert client.get("/jobs", params={"hidden": "true"}).json()["total"] == 1
    assert client.get("/rejections").json()["rejections"][0]["reason"] == "agency work"


def test_reason_is_cleared_when_the_job_is_restored(client, seeded):
    client.patch(f"/jobs/{seeded['a']}/state", json={"status": "dismissed", "reason": "on-site"})
    client.patch(f"/jobs/{seeded['a']}/state", json={"status": "new"})
    assert client.get(f"/jobs/{seeded['a']}").json()["reason"] is None
    assert client.get("/rejections").json()["rejections"] == []
    # a reason on a non-dismissal is ignored rather than stored
    client.patch(f"/jobs/{seeded['a']}/state", json={"status": "shortlisted", "reason": "nonsense"})
    assert client.get(f"/jobs/{seeded['a']}").json()["reason"] is None


def test_rejection_reasons_reach_the_prompts(client, seeded, monkeypatch):
    from jobfinder.prompts import deepdive_prompt, triage_prompt

    digest = make_candidate()
    entry = [{"ref": 1, "company": "X", "title": "Y", "location": "Z", "remote_type": "remote",
              "salary": "", "excerpt": "..."}]
    assert "REJECTED" not in triage_prompt(digest, entry)

    client.patch(f"/jobs/{seeded['b']}/state", json={"status": "dismissed", "reason": "consultancy, no product"})
    prompt = triage_prompt(digest, entry)
    assert "POSTINGS THE CANDIDATE REJECTED" in prompt
    assert 'Python Backend Developer at Globex' in prompt and '"consultancy, no product"' in prompt
    assert "guidance about taste, not as rules" in prompt
    assert "consultancy, no product" in deepdive_prompt(digest, {"company": "X", "title": "Y", "location": "Z", "url": ""}, "...")

    # bounded: only the most recent N are included
    monkeypatch.setattr("jobfinder.prompts.settings", lambda: type("S", (), {"limits": type("L", (), {"rejections_in_prompt": 1, "notes_chars": 6000})()})())
    client.patch(f"/jobs/{seeded['c']}/state", json={"status": "dismissed", "reason": "not eligible"})
    prompt = triage_prompt(digest, entry)
    assert "not eligible" in prompt and "consultancy, no product" not in prompt


def test_dismissed_jobs_are_skipped_by_the_pipeline_too(seeded, criteria):
    from jobfinder import db
    db.set_user_state(seeded["c"], "dismissed", reason="wrong field")
    assert [r["id"] for r in db.jobs_needing("triage", criteria, 100)] == []


def test_schema_migration_adds_reason_to_an_old_database(tmp_path, monkeypatch):
    """A database created before the reason column existed gets it on init."""
    import sqlite3
    from jobfinder import db
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "db_path", lambda: path)
    old_schema = db.SCHEMA.replace(
        "    reason     TEXT,                          -- why it was dismissed; fed back into scoring\n", "")
    assert "reason" not in old_schema
    old = sqlite3.connect(path)
    old.executescript(old_schema)
    old.close()
    db.init_db()
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(user_state)")}
    assert "reason" in cols
    db.init_db()  # idempotent


def test_settings_change_persists_and_reschedules(client, tmp_path, monkeypatch):
    import jobfinder.config as config
    import jobfinder.scheduler as sched

    yaml_path = tmp_path / "settings.yaml"
    yaml_path.write_text("interval_minutes: 120        # how often\nrun_on_start: true\nlimits:\n  triage_batch_size: 12\n")
    monkeypatch.setattr(config, "settings_path", lambda: yaml_path)
    monkeypatch.setattr(config.Settings, "model_post_init", lambda self, ctx: None)
    monkeypatch.setattr(config, "_load_yaml", lambda path: __import__("yaml").safe_load(yaml_path.read_text()) if path.name == "settings.yaml" else {})
    config.reload()
    rescheduled = []
    monkeypatch.setattr(sched, "set_interval", lambda m: rescheduled.append(m) or True)
    monkeypatch.setattr(sched, "next_run", lambda: None)

    assert client.get("/settings").json()["interval_minutes"] == 120
    r = client.patch("/settings", json={"interval_minutes": 45})
    assert r.status_code == 200 and r.json()["interval_minutes"] == 45
    assert rescheduled == [45]
    text = yaml_path.read_text()
    assert "interval_minutes: 45" in text
    assert "# how often" in text and "triage_batch_size: 12" in text  # comments and the rest intact
    assert client.patch("/settings", json={"interval_minutes": 1}).status_code == 422  # below the floor
    assert client.patch("/settings", json={}).status_code == 400
    config.reload()


@pytest.fixture()
def profile_dir(tmp_path, monkeypatch):
    """An empty legacy profile/ folder, so nothing on disk is imported."""
    from jobfinder.pipeline import profile as prof
    d = tmp_path / "profile"; d.mkdir()
    monkeypatch.setattr(prof, "profile_dir", lambda: d)
    return d


def test_cv_upload_replaces_previous_and_invalidates_digest(client, profile_dir):
    from jobfinder import db
    db.save_cv(1, "cv.pdf", b"%PDF-old", "old text")
    db.save_digest(1, "stale-hash", "{}")
    r = client.post("/profile/cv", files={"file": ("My Resume.md", b"# Jane Doe\nSenior engineer, C++ and Python.", "text/markdown")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["saved"] == "cv.md" and body["cv"]["name"] == "cv.md"
    assert body["cv_files"] == ["cv.md"]                 # the old cv.pdf is gone
    assert body["cv_chars"] > 0 and body["digest_current"] is False   # digest will be rebuilt
    assert db.get_cv_blob(1) == ("cv.md", b"# Jane Doe\nSenior engineer, C++ and Python.")
    dl = client.get("/profile/cv")
    assert dl.status_code == 200 and dl.content.startswith(b"# Jane Doe") and 'filename="cv.md"' in dl.headers["content-disposition"]


def test_cv_upload_rejects_bad_files(client, profile_dir):
    assert client.post("/profile/cv", files={"file": ("cv.docx", b"PK..", "application/octet-stream")}).status_code == 400
    assert client.post("/profile/cv", files={"file": ("cv.pdf", b"not a pdf", "application/pdf")}).status_code == 400
    assert client.post("/profile/cv", files={"file": ("cv.txt", b"", "text/plain")}).status_code == 400
    assert client.get("/profile").json()["cv"] is None    # nothing was written
    assert client.get("/profile/cv").status_code == 404


def test_notes_round_trip(client, profile_dir):
    r = client.put("/profile/notes", json={"text": "I want remote C++ work.\n\nNo agencies."})
    assert r.status_code == 200
    assert client.get("/profile").json()["notes"] == "I want remote C++ work.\n\nNo agencies.\n"


def test_source_toggle_survives_a_config_resync(tmp_db):
    from jobfinder import db
    cfg = {"id": "gh-acme", "type": "greenhouse", "slug": "acme", "enabled": True}
    assert db.upsert_source(cfg, origin="config") is True
    assert db.set_user_state  # (sanity: module loaded)
    db.set_source_enabled("gh-acme", False)
    db.upsert_source(cfg, origin="config")          # what every run does
    assert db.get_source("gh-acme")["enabled"] == 0  # stays off
    assert db.delete_source("gh-acme") is False      # config sources are not deletable


def test_add_source_from_a_careers_url(client, tmp_db, monkeypatch):
    import jobfinder.api as api
    from jobfinder.models import RawJob

    class FakeBoard:
        def __init__(self, cfg): self.cfg = cfg
        def fetch(self): return [RawJob(source_id=self.cfg["id"], title="Engineer", company="Acme")] * 3
    monkeypatch.setattr("jobfinder.sources.build", lambda cfg: FakeBoard(cfg))

    r = client.post("/sources", json={"url": "https://jobs.lever.co/acme/some-posting-id"})
    assert r.status_code == 200, r.text
    assert r.json()["source_id"] == "le-acme" and r.json()["open_positions"] == 3
    listed = {s["id"]: s for s in client.get("/sources").json()["sources"]}
    assert listed["le-acme"]["origin"] == "user" and listed["le-acme"]["deletable"] is True
    assert client.post("/sources", json={"url": "https://jobs.lever.co/acme"}).status_code == 409
    # not an ATS URL, nothing behind the page, and the page is empty -> refused
    from jobfinder import discovery, render
    monkeypatch.setattr(discovery, "sniff_ats", lambda url: None)
    monkeypatch.setattr(render, "render", lambda url: render.Rendered(url=url, text="", rendered=True))
    assert client.post("/sources", json={"url": "https://example.com/careers"}).status_code == 400

    class EmptyBoard(FakeBoard):
        def fetch(self): return []
    monkeypatch.setattr("jobfinder.sources.build", lambda cfg: EmptyBoard(cfg))
    assert client.post("/sources", json={"url": "https://boards.greenhouse.io/nobody"}).status_code == 400
    assert client.get("/sources").json()["sources"][0]["id"] == "le-acme"  # nothing else was added

    assert client.post("/sources/le-acme/enabled", params={"enabled": "false"}).status_code == 200
    assert client.delete("/sources/le-acme").status_code == 200
    assert client.delete("/sources/le-acme").status_code == 404


@pytest.fixture()
def prefs_doc(tmp_db):
    """A preferences document saved for the default user in the throwaway db."""
    import jobfinder.config as config
    config.save_preferences(config.Preferences(based_in="Netherlands", titles=["Engineer"],
                                               min_salary=config.Salary(amount=70000, currency="EUR", period="year")))
    yield
    config.preferences.cache_clear()


def test_preferences_form_save_validates_and_orders_markets(client, prefs_doc):
    assert client.get("/preferences").json()["preferences"]["based_in"] == "Netherlands"
    body = {
        "based_in": "Netherlands", "citizenship": ["Netherlands", "EU"], "titles": ["C++ Engineer"],
        "must_have": ["C++"], "dealbreakers": ["security clearance"],
        "min_salary": {"amount": 90000, "currency": "EUR", "period": "year"},
        "location_rules": [
            {"country": "US", "remote": "required", "priority": 99},
            {"region": "EU", "remote": "any"},
        ],
        "market_priority": ["stale", "values"],
    }
    r = client.put("/preferences", json=body)
    assert r.status_code == 200, r.text
    p = r.json()["preferences"]
    assert [x["priority"] for x in p["location_rules"]] == [1, 2]     # order wins over the sent numbers
    assert p["market_priority"] == ["US", "EU"]                        # derived, not the stale list
    assert p["min_salary"]["amount"] == 90000
    # stored as one document per user in the database, not in a file
    from jobfinder import db
    doc = db.get_preferences_doc(db.DEFAULT_USER_ID)
    assert doc["titles"] == ["C++ Engineer"] and doc["market_priority"] == ["US", "EU"]
    # the pipeline sees the new values
    from jobfinder.config import preferences
    assert preferences().must_have == ["C++"] and preferences().fingerprint != ""
    # validation: a bad remote value is refused and nothing is written
    bad = dict(body, location_rules=[{"country": "US", "remote": "sometimes"}])
    assert client.put("/preferences", json=bad).status_code == 422
    assert db.get_preferences_doc(db.DEFAULT_USER_ID)["location_rules"][0]["remote"] == "required"


def test_reset_jobs_keeps_sources_and_needs_the_word(client, seeded, tmp_db):
    from jobfinder import db
    db.upsert_source({"id": "gh-acme", "type": "greenhouse", "slug": "acme"}, origin="discovered")
    client.patch(f"/jobs/{seeded['a']}/state", json={"status": "shortlisted"})
    assert client.post("/reset/jobs", json={"confirm": "delete"}).status_code == 400   # wrong word
    assert client.get("/jobs").json()["total"] == 3                                    # nothing happened
    r = client.post("/reset/jobs", json={"confirm": "DELETE"})
    assert r.status_code == 200
    assert r.json()["deleted"]["jobs"] == 3 and r.json()["deleted"]["decisions"] == 1
    assert client.get("/jobs").json()["total"] == 0
    assert [s["id"] for s in client.get("/sources").json()["sources"]] == ["gh-acme"]  # sources kept


def test_reset_all_clears_sources_and_reseeds(client, seeded, tmp_db, monkeypatch):
    from jobfinder import db, discovery
    db.upsert_source({"id": "gh-acme", "type": "greenhouse", "slug": "acme"}, origin="discovered")
    db.mark_discovery("source:gh-acme", "source")
    monkeypatch.setattr(discovery, "seed_from_config", lambda: db.upsert_source({"id": "seed", "type": "remoteok"}, origin="config"))
    r = client.post("/reset/all", json={"confirm": "DELETE"})
    assert r.status_code == 200 and r.json()["deleted"]["sources"] == 1
    assert [s["id"] for s in client.get("/sources").json()["sources"]] == ["seed"]
    assert client.get("/jobs").json()["total"] == 0
    assert not db.seen_discovery("source:gh-acme")


def test_reset_refused_while_a_scan_runs(client, seeded, monkeypatch):
    import jobfinder.api as api
    monkeypatch.setattr(api.run_mod, "is_running", lambda: True)
    assert client.post("/reset/jobs", json={"confirm": "DELETE"}).status_code == 409
    assert client.get("/jobs").json()["total"] == 3


def test_preferences_yaml_is_imported_once_then_renamed(tmp_db):
    """The pre-database file becomes the default user's document on first
    read, and is renamed so nobody edits a file that is no longer read."""
    import jobfinder.config as config
    path = config.preferences_path()
    path.write_text("based_in: Ireland\ntitles: [Dev]\nlocation_rules:\n  - {country: US, remote: required}\n")
    config.preferences.cache_clear()
    p = config.preferences()
    assert p.based_in == "Ireland" and p.titles == ["Dev"]
    assert not path.exists() and path.with_name("preferences.yaml.imported").exists()
    from jobfinder import db
    assert db.get_preferences_doc(db.DEFAULT_USER_ID)["based_in"] == "Ireland"
    # a second load comes from the database, not the (now absent) file
    config.preferences.cache_clear()
    assert config.preferences().based_in == "Ireland"


def test_a_broken_pending_preferences_yaml_still_stops_the_start(tmp_db):
    import jobfinder.config as config
    config.preferences_path().write_text("location_rules:\n  - {country: US, remote: sometimes}\n")
    [err] = config.check_config()
    assert err.file == "preferences.yaml" and "remote" in err.detail
    config.preferences.cache_clear()
    with pytest.raises(config.ConfigError):
        config.preferences()


def test_no_file_and_no_document_means_empty_preferences(tmp_db):
    import jobfinder.config as config
    config.preferences_path().unlink(missing_ok=True)
    config.preferences.cache_clear()
    assert config.preferences().titles == [] and config.check_config() == []


def test_cli_refuses_to_start_on_a_broken_config(tmp_path, monkeypatch, capsys):
    import jobfinder.config as config
    from jobfinder.cli import main
    config.settings_path().write_text("interval_minutes: [oops\n")
    config.settings.cache_clear(); config.preferences.cache_clear()
    with pytest.raises(SystemExit) as exit_:
        main(["doctor"])
    assert exit_.value.code == 2
    err = capsys.readouterr().err
    assert "Cannot start" in err and "config/settings.yaml cannot be used: not valid YAML (line" in err
    assert "Fix the file and start again" in err
    config.settings.cache_clear(); config.preferences.cache_clear()


def test_notes_go_into_every_judgement_verbatim(client, profile_dir, monkeypatch):
    from jobfinder.pipeline import profile as prof
    from jobfinder.prompts import deepdive_prompt, triage_prompt
    entry = [{"ref": 1, "company": "X", "title": "Y", "location": "Z", "remote_type": "remote", "salary": "", "excerpt": "..."}]
    assert "OWN NOTES" not in triage_prompt(make_candidate(), entry)           # no notes yet
    client.put("/profile/notes", json={"text": "I know ROS2 well but I am **not a roboticist**."})
    digest = make_candidate(notes=prof.notes_text(1))                    # what the pipeline loads
    t = triage_prompt(digest, entry)
    assert "THE CANDIDATE'S OWN NOTES" in t and "**not a roboticist**" in t
    assert "**not a roboticist**" in deepdive_prompt(digest, {"company": "X", "title": "Y", "location": "Z", "url": ""}, "...")
    # bounded
    monkeypatch.setattr("jobfinder.prompts.settings", lambda: type("S", (), {"limits": type("L", (), {"notes_chars": 20, "rejections_in_prompt": 20})()})())
    assert "[... truncated ...]" in triage_prompt(digest, entry)


def test_user_files_are_created_from_examples(tmp_path, monkeypatch):
    import shutil
    import jobfinder.config as config
    root = tmp_path / "repo"
    for rel in ("config/settings.example.yaml", "config/preferences.example.yaml"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(config.ROOT / rel, root / rel)
    monkeypatch.setattr(config, "ROOT", root)
    created = config.ensure_user_files()
    assert created == ["config/settings.yaml"]   # preferences, notes and the CV live in the database
    assert config.ensure_user_files() == []           # idempotent
    assert (root / "config/settings.yaml").read_text() == (root / "config/settings.example.yaml").read_text()


def test_example_files_are_valid_and_empty_of_personal_data():
    import jobfinder.config as config
    prefs = config.Preferences.model_validate(__import__("yaml").safe_load((config.ROOT / "config/preferences.example.yaml").read_text()))
    assert prefs.based_in == "" and prefs.titles == [] and prefs.citizenship == [] and prefs.min_salary is None
    config.Settings.model_validate(__import__("yaml").safe_load((config.ROOT / "config/settings.example.yaml").read_text()))
    from jobfinder.pipeline.profile import _HTML_COMMENT
    from jobfinder.textutil import clean
    example_notes = (config.ROOT / "profile/notes.example.md").read_text()
    assert clean(_HTML_COMMENT.sub("", example_notes)) == ""  # reads as "not written yet"


def test_readiness_checklist_and_no_scan_until_ready(client, profile_dir, tmp_db, monkeypatch):
    import jobfinder.config as config
    from jobfinder.pipeline import profile as prof
    from jobfinder.pipeline import run as run_mod
    config.preferences_path().unlink(missing_ok=True)
    config.save_preferences(config.Preferences())
    client.put("/profile/notes", json={"text": "<!-- just the template comment -->\n"})
    r = prof.readiness(1)
    assert r == {"cv": False, "notes": False, "preferences": False, "ready": False}
    assert client.get("/health").json()["setup"]["ready"] is False
    assert client.get("/health", headers={}).json()["setup"]["ready"] is False
    # no scan at all: not from the API, not from the scheduler (run_once)
    resp = client.post("/runs")
    assert resp.status_code == 409 and "missing: cv, notes, preferences" in resp.json()["detail"]
    monkeypatch.setattr(run_mod.db, "start_run", lambda: (_ for _ in ()).throw(AssertionError("a run was started")))
    assert run_mod.run_once() == {"skipped": "not set up", "not_ready": ["cv", "notes", "preferences"]}
    client.post("/profile/cv", files={"file": ("cv.md", b"# Jane Doe\nEngineer.", "text/markdown")})
    client.put("/profile/notes", json={"text": "I want remote work."})
    client.put("/preferences", json={"based_in": "Denmark", "titles": ["Engineer"]})
    assert prof.readiness(1)["ready"] is True
    assert client.get("/health").json()["setup"] == {"cv": True, "notes": True, "preferences": True, "ready": True}
    assert [c.user_id for c in prof.candidates()] == [1]


def test_health_without_a_token_has_no_per_user_parts(anon_client):
    h = anon_client.get("/health").json()
    assert h["ok"] is True and h["setup"] is None and h["stats"] is None and h["stale_scores"] is None


def test_legacy_profile_folder_is_imported_once_for_the_first_user(profile_dir, tmp_db):
    import json
    from jobfinder import db
    from jobfinder.pipeline import profile as prof
    (profile_dir / "notes.example.md").write_text("<!-- template -->")
    (profile_dir / "README.md").write_text("# docs")
    (profile_dir / "random.txt").write_text("not a cv")
    assert prof.import_legacy_files() == [] and prof.readiness(1)["cv"] is False   # only cv.<ext> counts
    (profile_dir / "cv.md").write_text("# Jane Doe\nEngineer.")
    (profile_dir / "notes.md").write_text("<!-- template -->\nI want remote work.\n")
    (profile_dir / ".cache").mkdir()
    from jobfinder.config import preferences
    src_hash = prof.Candidate(1, "x", preferences(1), prof.extract_cv_text("cv.md", b"# Jane Doe\nEngineer."), "I want remote work.").source_hash
    (profile_dir / ".cache" / "profile.json").write_text(json.dumps({"source_hash": src_hash, "digest": {
        "headline": "Engineer", "core_skills": ["a"], "summary": "s" * 40, "search_keywords": ["a", "b", "c"]}}))
    assert prof.import_legacy_files() == ["cv.md", "notes.md", ".cache/profile.json"]
    cand = prof.load(1)
    assert cand.readiness["cv"] and cand.readiness["notes"] and cand.digest is not None and cand.digest.headline == "Engineer"
    assert db.get_cv_blob(1) == ("cv.md", b"# Jane Doe\nEngineer.")
    assert sorted(p.name for p in profile_dir.iterdir() if p.name.endswith(".imported")) == ["cv.md.imported", "notes.md.imported"]
    assert (profile_dir / ".cache" / "profile.json.imported").exists()
    assert prof.import_legacy_files() == []                                  # once


def test_add_source_finds_the_board_behind_a_careers_page(client, tmp_db, monkeypatch):
    """A custom careers page that loads its jobs from Greenhouse in JavaScript
    is registered as that Greenhouse board, not as a page to read."""
    from jobfinder import discovery
    from jobfinder.models import RawJob
    monkeypatch.setattr(discovery, "sniff_ats", lambda url: ("greenhouse", "examplecorp"))
    class FakeBoard:
        def __init__(self, cfg): self.cfg = cfg
        def fetch(self): return [RawJob(source_id=self.cfg["id"], title="Engineer", company="Example Corp")] * 5
    monkeypatch.setattr("jobfinder.sources.build", lambda cfg: FakeBoard(cfg))
    r = client.post("/sources", json={"url": "https://www.examplecorp.com/careers#careers"})
    assert r.status_code == 200, r.text
    assert r.json()["source_id"] == "gr-examplecorp" and r.json()["how"] == "found behind the page"
    assert r.json()["open_positions"] == 5


def test_add_source_falls_back_to_a_rendered_page(client, tmp_db, monkeypatch):
    from jobfinder import discovery, render
    monkeypatch.setattr(discovery, "sniff_ats", lambda url: None)
    monkeypatch.setattr(render, "render", lambda url: render.Rendered(url=url, text="Open roles\n" + "Senior Engineer, Acme. " * 20, links=[("Senior Engineer", "https://acme.example/jobs/1")], rendered=True))
    r = client.post("/sources", json={"url": "acme.example/careers"})
    assert r.status_code == 200, r.text
    assert r.json()["type"] == "webpage" and r.json()["source_id"] == "web-acme-example"
    src = {s["id"]: s for s in client.get("/sources").json()["sources"]}["web-acme-example"]
    assert src["config"]["url"] == "https://acme.example/careers" and src["origin"] == "user"
    # an empty page is refused
    monkeypatch.setattr(render, "render", lambda url: render.Rendered(url=url, text="Loading…", rendered=True))
    assert client.post("/sources", json={"url": "https://blank.example/"}).status_code == 400


def test_webpage_source_yields_one_entry_for_extraction(monkeypatch):
    from jobfinder import render
    from jobfinder.sources import build
    page = render.Rendered(url="https://acme.example/careers", text="Roles: Engineer; Designer. " + "Acme builds robots for factories. " * 10, links=[("Engineer", "https://acme.example/jobs/eng"), ("About", "https://acme.example/about")], rendered=True)
    monkeypatch.setattr("jobfinder.sources.webpage.render", lambda url: page)
    jobs = build({"id": "web-acme-example", "type": "webpage", "url": page.url, "company": "Acme"}).fetch()
    assert len(jobs) == 1 and jobs[0].needs_extraction and jobs[0].company == "Acme"
    assert "Roles: Engineer" in jobs[0].description and "https://acme.example/jobs/eng" in jobs[0].description
    monkeypatch.setattr("jobfinder.sources.webpage.render", lambda url: render.Rendered(url=url, text="", rendered=True))
    assert build({"id": "web-x", "type": "webpage", "url": "https://x.example"}).fetch() == []


def test_workday_urls_are_recognised_and_configured():
    from jobfinder.discovery import detect_ats, source_config_for
    assert detect_ats("https://examplecorp.wd1.myworkdayjobs.com/en-US/Example_Corp/introduceYourself") == ("workday", "examplecorp/wd1/Example_Corp")
    assert detect_ats("https://acme.wd5.myworkdayjobs.com/External/job/Berlin/Engineer_R123") == ("workday", "acme/wd5/External")
    assert detect_ats("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs") is None   # the API path, not a site
    cfg = source_config_for("workday", "examplecorp/wd1/Example_Corp", "https://x")
    assert cfg["id"] == "wd-examplecorp" and cfg["tenant"] == "examplecorp" and cfg["site"] == "Example_Corp" and cfg["company"] == "Example Corp"
    assert source_config_for("greenhouse", "acme")["id"] == "gr-acme"   # first two letters of the type


def test_workday_adapter_pages_and_reads_details(monkeypatch):
    import httpx
    from jobfinder.sources import build
    from jobfinder.sources import base
    listing = {0: {"total": 45, "jobPostings": [{"title": f"Role {i}", "externalPath": f"/job/X/Role-{i}", "locationsText": "Berlin"} for i in range(20)]},
               20: {"total": 0, "jobPostings": [{"title": f"Role {i}", "externalPath": f"/job/X/Role-{i}", "locationsText": "Berlin"} for i in range(20, 40)]},
               40: {"total": 0, "jobPostings": [{"title": f"Role {i}", "externalPath": f"/job/X/Role-{i}", "locationsText": "Berlin"} for i in range(40, 45)]}}
    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, json, headers):
            return httpx.Response(200, json=listing[json["offset"]], request=httpx.Request("POST", url))
        def get(self, url, headers):
            return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "<p>Build robots</p>", "externalUrl": "https://wd/" + url.rsplit("/", 1)[-1]}}, request=httpx.Request("GET", url))
    monkeypatch.setattr(base, "http_client", FakeClient)
    monkeypatch.setattr("jobfinder.sources.workday.http_client", FakeClient)
    jobs = build({"id": "wd-acme", "type": "workday", "tenant": "acme", "wd": "wd1", "site": "Ext"}).fetch()
    assert len(jobs) == 45                                   # all three pages, despite total only on the first
    assert jobs[0].description == "Build robots" and jobs[0].url.startswith("https://wd/") and jobs[0].location == "Berlin"


def test_scores_from_before_a_preferences_change_show_as_stale(client, seeded, criteria, monkeypatch):
    """Changing preferences must not blank the list: the previous score stays,
    flagged stale and still used for sorting, until the next scan."""
    from jobfinder.pipeline import criteria as crit
    before = client.get("/jobs").json()["jobs"]
    assert before[0]["score"] == 92 and before[0]["score_stale"] == 0
    monkeypatch.setattr(crit, "current_criteria_hash", lambda user_id=1: "new-criteria")   # preferences changed
    after = client.get("/jobs", params={"sort": "score"}).json()["jobs"]
    assert [(j["score"], j["score_stale"]) for j in after] == [(92, 1), (55, 1), (None, 0)]   # same order, now stale
    assert after[0]["summary"] == "Great C++ role"                                        # the old deep dive still shows
    assert client.get("/health").json()["stale_scores"] == 2
    assert client.get("/jobs", params={"min_score": 90}).json()["total"] == 1             # filters use the stale score too


def test_add_source_with_an_unreachable_host_is_a_clear_refusal(client, tmp_db, monkeypatch):
    from jobfinder import discovery, render
    from jobfinder.sources import base
    def boom(url, **kw): raise ConnectionError("Name or service not known")
    monkeypatch.setattr(base, "fetch_url", boom)
    monkeypatch.setattr(render, "sync_playwright", None, raising=False)
    monkeypatch.setattr(discovery, "probe_ats_by_name", lambda url: None)
    r = client.post("/sources", json={"url": "https://careers.zzqx-nonexistent-domain.invalid/jobs"})
    assert r.status_code == 400, r.text
    assert "nothing to read" in r.json()["detail"] or "Could not reach" in r.json()["detail"]
