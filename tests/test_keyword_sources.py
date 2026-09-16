"""Keyword searches: tags spelled Jobicy's way, the region from any spelling of the rule."""

from conftest import make_candidate

from jobfinder import discovery
from jobfinder.config import LocationRule, Preferences


def _cand(rules, **digest):
    c = make_candidate(**digest)
    c.prefs = Preferences(based_in="Poland", titles=["CTO"], location_rules=rules)
    return c


def test_terms_become_hyphenated_tags_and_regions_match_loosely():
    c = _cand([LocationRule(country="Poland"), LocationRule(region="EU / EEA"), LocationRule(country="United States of America")],
              search_keywords=["Head of Product", "CTO", "c++"], core_skills=["Python"])
    out = discovery.keyword_sources(c)
    assert [s["keyword"] for s in out][:4] == ["python", "head-of-product", "cto", "c++"]
    assert {s["region"] for s in out} == {"europe", "usa"}
    assert out[0]["company"] == '"python" · europe' and out[0]["id"] == "kw-python-europe"


def test_unknown_region_means_no_filter_and_says_so():
    c = _cand([LocationRule(country="Atlantis")])
    out = discovery.keyword_sources(c)
    assert out[0]["geo"] == "anywhere" and out[0]["region"] == "anywhere"
    assert out[0]["company"] == '"python" · anywhere'.replace("python", out[0]["keyword"])


def test_stale_keyword_rows_are_pruned_when_the_cv_changes(tmp_db):
    from jobfinder import db
    db.upsert_source({"id": "kw-old-anywhere", "type": "jobicy", "tag": "old", "geo": "anywhere"}, origin="keyword", followers=[1])
    db.upsert_source({"id": "kw-keep-europe", "type": "jobicy", "tag": "keep", "geo": "europe"}, origin="keyword", followers=[1])
    db.follow_source(1, "kw-keep-europe", False)                    # switched off by the user
    assert db.prune_keyword_sources(1, ["kw-keep-europe", "kw-new-europe"]) == 1
    assert db.get_source("kw-old-anywhere") is None
    assert db.follows(1, "kw-keep-europe") is False                 # still off after the regeneration
    db.upsert_source({"id": "kw-keep-europe", "type": "jobicy", "tag": "keep", "geo": "europe", "company": "relabelled"}, origin="keyword", followers=[1])
    assert db.follows(1, "kw-keep-europe") is False and "relabelled" in db.get_source("kw-keep-europe")["config"]
