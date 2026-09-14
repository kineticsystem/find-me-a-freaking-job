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
