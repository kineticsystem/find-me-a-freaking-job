from jobfinder.pipeline import progress


def test_progress_reports_stage_counts_and_an_estimate():
    assert progress.snapshot() == {"active": False}
    progress.begin(7)
    progress.stage("fetch", "fetching")
    progress.note(fetched=120, sources=9)
    s = progress.snapshot()
    assert s["active"] and s["run_id"] == 7 and s["stage"] == "fetch" and s["fetched"] == 120 and s["eta_seconds"] is None
    progress.stage("triage", "scoring 60 postings", total=5)
    progress.unit_done(40.0); progress.unit_done(60.0)
    s = progress.snapshot()
    assert (s["current"], s["total"]) == (2, 5)
    assert s["eta_seconds"] == 150            # 3 left x mean(40, 60)
    progress.finish()
    assert progress.snapshot() == {"active": False}


def test_progress_ignores_updates_when_no_scan_is_active():
    progress.finish()
    progress.stage("triage", "x", total=3); progress.unit_done(1.0); progress.note(fetched=5)
    assert progress.snapshot() == {"active": False}


def test_stop_ends_triage_between_batches_and_keeps_what_was_scored(seeded, criteria, monkeypatch, tmp_path):
    """Three jobs, batch size 1: the first batch is scored, then a stop is
    requested from inside the model call, so the second is not started."""
    from jobfinder import db, opencode
    from jobfinder.models import ProfileDigest, TriageBatch, TriageResult
    from jobfinder.pipeline import progress, triage
    import jobfinder.pipeline.triage as tri

    monkeypatch.setattr(tri.settings().limits, "triage_batch_size", 1)
    calls = []
    def fake_session(prompt, workdir, schema, **kw):
        calls.append(1)
        if len(calls) == 2:
            progress.request_stop()                       # user presses Stop mid-scan
            raise opencode.SessionCancelled("killed")
        return TriageBatch(results=[TriageResult(ref=1, score=70, verdict="strong")])
    monkeypatch.setattr(tri, "run_session", fake_session)
    with db.connect() as conn:
        conn.execute("DELETE FROM evaluations")          # all three need triage
    progress.begin(1)
    try:
        digest = ProfileDigest(headline="Engineer", core_skills=["a"], summary="s" * 40, search_keywords=["a", "b", "c"])
        stats = triage.run_triage(digest, "test-criteria", tmp_path, run_id=None)
        assert stats["stopped"] is True and stats["triaged"] == 1 and len(calls) == 2
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 1   # the first batch survived
    finally:
        progress.finish()


def test_stop_endpoint(client, monkeypatch):
    from jobfinder.pipeline import progress
    progress.finish()
    assert client.post("/runs/stop").status_code == 409     # nothing running
    progress.begin(3)
    monkeypatch.setattr("jobfinder.opencode.kill_current", lambda: None)
    assert client.post("/runs/stop").json() == {"ok": True, "stopping": True}
    assert client.get("/health").json()["progress"]["stopping"] is True
    progress.finish()
