"""One fetch, a score for every user with a complete profile, each under their own criteria."""

from __future__ import annotations

from jobfinder import auth, db
from jobfinder.config import DEFAULT_USER_ID, Preferences, save_preferences
from jobfinder.pipeline import profile as prof


def _complete(user_id: int, cv: str, notes: str, titles: list[str]) -> None:
    db.save_cv(user_id, "cv.md", cv.encode(), prof.extract_cv_text("cv.md", cv.encode()))
    db.save_notes(user_id, notes)
    save_preferences(Preferences(based_in="Testland", titles=titles), user_id=user_id)


def test_candidates_are_the_users_with_a_complete_profile(tmp_db):
    db.claim_user(DEFAULT_USER_ID, "a@example.com", auth.hash_password("password-1"), is_admin=True)
    b = db.create_user("b@example.com", auth.hash_password("password-1"))
    c = db.create_user("c@example.com", auth.hash_password("password-1"))
    _complete(DEFAULT_USER_ID, "# A\nC++ engineer.", "Remote only.", ["C++ Engineer"])
    _complete(b, "# B\nHead gardener.", "Outdoors.", ["Gardener"])
    _complete(c, "# C\nSomeone.", "", ["Anything"])            # no notes: not ready
    cands = prof.candidates()
    assert [x.user_id for x in cands] == [DEFAULT_USER_ID, b]
    assert cands[0].criteria != cands[1].criteria                  # their own CV + preferences
    assert cands[0].name == "a@example.com"
    # a digest stored under the current CV is loaded; under an old one it is not
    db.save_digest(b, cands[1].source_hash, '{"headline": "Gardener", "core_skills": ["pruning"], "summary": "' + "x" * 40 + '", "search_keywords": ["a","b","c"]}')
    db.save_digest(DEFAULT_USER_ID, "old-hash", "{}")
    fresh = {x.user_id: x for x in prof.candidates()}
    assert fresh[b].digest is not None and fresh[b].digest.headline == "Gardener"
    assert fresh[DEFAULT_USER_ID].digest is None


def test_run_scores_each_ready_user_and_fetches_once(tmp_db, monkeypatch, tmp_path):
    from jobfinder.pipeline import run as run_mod

    db.claim_user(DEFAULT_USER_ID, "a@example.com", auth.hash_password("password-1"), is_admin=True)
    b = db.create_user("b@example.com", auth.hash_password("password-1"))
    _complete(DEFAULT_USER_ID, "# A\nC++ engineer.", "Remote only.", ["C++ Engineer"])
    _complete(b, "# B\nHead gardener.", "Outdoors.", ["Gardener"])

    fetches: list[int] = []
    scored: list[tuple[str, int, str]] = []

    def fake_load_digest(cand, workdir, force=False):
        from jobfinder.models import ProfileDigest
        cand.digest = ProfileDigest(headline=cand.name, core_skills=["x"], summary="s" * 40, search_keywords=["a", "b", "c"])
        return cand.digest

    monkeypatch.setattr(run_mod.opencode, "llm_reachable", lambda wait_seconds=0: (True, ""))
    monkeypatch.setattr(run_mod.profile, "load_digest", fake_load_digest)
    monkeypatch.setattr(run_mod.fetch, "fetch_all", lambda workdir, digest: (fetches.append(1), ([], {"sources_run": 0}))[1])
    monkeypatch.setattr(run_mod.settings().discovery, "enabled", False)
    monkeypatch.setattr(run_mod.discovery, "harvest", lambda raw: [])
    monkeypatch.setattr(run_mod.extract, "extract", lambda raw, workdir: [])
    monkeypatch.setattr(run_mod.triage, "run_triage", lambda cand, workdir, run_id: (scored.append(("triage", cand.user_id, cand.criteria)), {"triaged": 1})[1])
    monkeypatch.setattr(run_mod.deepdive, "run_deepdive", lambda cand, workdir, run_id: (scored.append(("deepdive", cand.user_id, cand.criteria)), {"deepdived": 1})[1])
    monkeypatch.setattr(run_mod.settings().paths, "runs", str(tmp_path))
    monkeypatch.setattr(run_mod, "_new_workdir", lambda: tmp_path)

    stats = run_mod.run_once()
    assert "error" not in stats, stats
    assert fetches == [1]                                              # once for everyone
    assert [(k, u) for k, u, _ in scored] == [("triage", 1), ("deepdive", 1), ("triage", b), ("deepdive", b)]
    assert scored[0][2] != scored[2][2]                                # each under their own criteria
    assert stats["triaged"] == 2 and set(stats["users"]) == {1, b}
    assert (tmp_path / "digest.md").read_text().count("# ") >= 3        # a section per user

