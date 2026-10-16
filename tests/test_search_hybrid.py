"""Hybrid keyword + CLAP search and find_similar, with the fake CLAP module."""

from __future__ import annotations

import _fake_clap as fc
import pytest
from _catalog_builder import add_asset, add_decision

from foley import search, status, vectors
from foley.catalog import db
from foley.search import SearchFilters, SearchSettings


@pytest.fixture(autouse=True)
def _no_status(monkeypatch):
    def nope(conn):
        raise NotImplementedError

    monkeypatch.setattr(status, "staleness_note", nope)


@pytest.fixture
def api(monkeypatch):
    return fc.install(monkeypatch)


@pytest.fixture
def conn(cfg, api):
    c = db.open_catalog(cfg)
    yield c
    c.close()


def feats(**kw):
    base = {"duration_s": 1.0, "lufs_integrated": -20.0, "attack_s": 0.01, "is_music": 0}
    base.update(kw)
    return base


def build(conn, embed=True):
    """Names that keyword search can and cannot find; embeddings say what they sound like."""
    ids = {}
    specs = {
        # rel_path: (sounds like, features)
        "sfx/dog/Dog_Bark_01.wav": ("dog bark animal", feats()),
        "sfx/misc/xq_17.wav": ("dog bark animal growl", feats(duration_s=0.8)),  # no keywords
        "sfx/misc/zz_04.wav": ("glass shatter break", feats(duration_s=2.0)),
        "sfx/impacts/Metal_Hit_03.wav": ("metal impact clang", feats(duration_s=0.5)),
        "sfx/impacts/Metal_Hit_03_copy.wav": ("metal impact clang", feats(duration_s=0.5)),
        "music/Battle Loop.mp3": ("orchestral drums battle", feats(duration_s=30, is_music=1)),
        "sfx/misc/aa_99.wav": ("wind howl ambience", feats(duration_s=6.0)),
    }
    for rel, (words, f) in specs.items():
        h = "hash-metal" if "Metal_Hit_03" in rel else "auto"
        ids[rel] = add_asset(conn, rel, features=f, content_hash=h)
        if embed:
            fc.add_embedding(conn, ids[rel], words)
    return ids


def paths(result):
    return [r["path"] for r in result["results"]]


def test_no_embeddings_is_keyword_only_with_note(conn):
    build(conn, embed=False)
    out = search.search(conn, "dog bark")
    assert out["mode"] == "keyword" and "model" not in out
    assert "no CLAP embeddings" in out["note"]
    assert paths(out) == ["sfx/dog/Dog_Bark_01.wav"]
    assert out["results"][0]["similarity"] is None


def test_semantic_only_hit_surfaces(conn):
    ids = build(conn)
    out = search.search(conn, "barking dog")
    assert out["mode"] == "hybrid" and out["model"] == "fake-a"
    assert out["note"] is None
    got = paths(out)
    assert got[0] == "sfx/dog/Dog_Bark_01.wav"  # keyword + vector
    assert "sfx/misc/xq_17.wav" in got  # vector only: no keyword in its name
    assert "sfx/misc/aa_99.wav" not in got  # unrelated sounds are not dragged in
    by = {r["path"]: r for r in out["results"]}
    assert 0 < by["sfx/misc/xq_17.wav"]["score"] < by["sfx/dog/Dog_Bark_01.wav"]["score"] <= 1
    assert by["sfx/misc/xq_17.wav"]["similarity"] > 0.3
    # the ranking behind it keeps ids and scores
    ranked = dict(search.rank(conn, "barking dog"))
    assert ids["sfx/misc/xq_17.wav"] in ranked


def test_pure_semantic_query_without_keyword_matches(conn):
    build(conn)
    out = search.search(conn, "shatter")  # only the embedding of zz_04 has it
    assert paths(out)[0] == "sfx/misc/zz_04.wav"


def test_weight_zero_and_semantic_off_are_keyword_only(conn):
    build(conn)
    keyword = search.search(conn, "growling animal", settings=SearchSettings(semantic=False))
    zero = search.search(conn, "growling animal", settings=SearchSettings(blend_weight=0.0))
    assert keyword["mode"] == zero["mode"] == "keyword"
    assert paths(keyword) == paths(zero) == []  # no file name has these words
    assert "sfx/misc/xq_17.wav" in paths(search.search(conn, "growling animal"))
    assert keyword["note"] is None and zero["note"] is None  # by choice: no complaint
    assert search.rank(conn, "dog", settings=SearchSettings(blend_weight=0)) == search.rank(
        conn, "dog", settings=SearchSettings(semantic=False))


def test_no_semantic_signal_adds_nothing(conn):
    build(conn)
    # nothing within 0.9 s sounds like an orchestra: equal (zero) cosines everywhere must
    # not turn every short sound into a "perfect" semantic hit
    out = search.search(conn, "orchestral strings", SearchFilters(max_duration_s=0.9))
    assert paths(out) == []


def test_weight_one_ranks_by_vector(conn):
    build(conn)
    out = search.search(conn, "growl", settings=SearchSettings(blend_weight=1.0))
    assert paths(out)[0] == "sfx/misc/xq_17.wav"


def test_filters_restrict_vector_candidates(conn):
    build(conn)
    out = search.search(conn, "dog bark", SearchFilters(max_duration_s=0.9))
    assert paths(out) == ["sfx/misc/xq_17.wav"]
    out = search.search(conn, "dog bark", SearchFilters(folder="sfx/dog"))
    assert paths(out) == ["sfx/dog/Dog_Bark_01.wav"]
    out = search.search(conn, "orchestral battle drums", SearchFilters(is_music=False))
    assert "music/Battle Loop.mp3" not in paths(out)
    ranked = search.rank(conn, "dog bark", restrict=[1])
    assert [aid for aid, _ in ranked] in ([], [1])


def test_missing_and_rejected_assets_stay_hidden(conn):
    ids = build(conn)
    conn.execute("UPDATE assets SET status = 'missing' WHERE id = ?",
                 (ids["sfx/misc/xq_17.wav"],))
    assert "sfx/misc/xq_17.wav" not in paths(search.search(conn, "barking dog"))
    add_decision(conn, "hash-sfx/misc/zz_04.wav", "er", "rejected")
    assert "sfx/misc/zz_04.wav" not in paths(search.search(conn, "shatter", project="er"))
    assert "sfx/misc/zz_04.wav" in paths(search.search(conn, "shatter", project="other"))


def test_duplicates_collapse_in_hybrid(conn):
    build(conn)
    out = search.search(conn, "metal clang impact")
    got = paths(out)
    assert sum(p.startswith("sfx/impacts/Metal_Hit_03") for p in got) == 1
    assert out["results"][0]["duplicates"] == 1


def test_unembedded_assets_keep_keyword_rank_and_coverage_note(conn):
    ids = build(conn)
    late = add_asset(conn, "sfx/dog/Dog_Bark_02_new.wav", features=feats())
    out = search.search(conn, "dog bark")
    assert "sfx/dog/Dog_Bark_02_new.wav" in paths(out)
    assert "cover 7 of 8" in out["note"]
    by = {r["id"]: r for r in out["results"]}
    assert by[late]["similarity"] is None and by[late]["score"] > 0.4
    assert by[ids["sfx/dog/Dog_Bark_01.wav"]]["similarity"] is not None


def test_resolve_order(conn, api):
    r = search.resolve(conn)
    assert (r.clap_model, r.model_source, r.blend_weight, r.weight_source) == (
        "fake-a", "default", search.DEFAULT_BLEND_WEIGHT, "default")
    r = search.resolve(conn, SearchSettings(models_clap=("fake-b", "fake-a")))
    assert (r.clap_model, r.model_source) == ("fake-b", "models")
    db.set_meta(conn, search.META_CLAP_MODEL, "fake-a")
    db.set_meta(conn, search.META_BLEND_WEIGHT, "0.3")
    r = search.resolve(conn, SearchSettings(models_clap=("fake-b",)))
    assert (r.clap_model, r.model_source, r.blend_weight, r.weight_source) == (
        "fake-a", "meta", 0.3, "meta")
    r = search.resolve(conn, SearchSettings(clap_model="fake-b", blend_weight=0.8))
    assert (r.clap_model, r.model_source, r.blend_weight, r.weight_source) == (
        "fake-b", "config", 0.8, "config")
    assert r.model_key == fc.model_key("fake-b")
    db.set_meta(conn, search.META_BLEND_WEIGHT, "garbage")
    assert search.resolve(conn).blend_weight == search.DEFAULT_BLEND_WEIGHT


def test_settings_from_config(cfg):
    cfg.search_clap_model, cfg.search_blend_weight = "fake-b", 0.25
    s = SearchSettings.from_config(cfg)
    assert (s.clap_model, s.blend_weight) == ("fake-b", 0.25)
    assert SearchSettings.from_config(None) == SearchSettings()


def test_model_without_embeddings_or_unknown(conn):
    build(conn)
    out = search.search(conn, "dog", settings=SearchSettings(clap_model="fake-b"))
    assert out["mode"] == "keyword" and "no CLAP embeddings for fake-b" in out["note"]
    out = search.search(conn, "dog", settings=SearchSettings(clap_model="nope"))
    assert out["mode"] == "keyword" and "unknown" in out["note"]


def test_encoder_failure_falls_back_to_keyword(cfg, monkeypatch):
    fc.install(monkeypatch, fail="CUDA exploded")
    conn = db.open_catalog(cfg)
    try:
        build(conn)
        out = search.search(conn, "dog bark")
        assert out["mode"] == "keyword" and "CUDA exploded" in out["note"]
        assert paths(out) == ["sfx/dog/Dog_Bark_01.wav"]
    finally:
        conn.close()


def test_models_not_installed(conn, monkeypatch):
    build(conn)
    monkeypatch.setattr(vectors, "clap_api", lambda: None)
    out = search.search(conn, "dog bark")
    assert out["mode"] == "keyword" and "not installed" in out["note"]


def test_candidates_order_sweeps_weights(conn):
    build(conn)
    c = search.candidates(conn, "growling animal", settings=SearchSettings(blend_weight=0),
                          semantic=True)
    assert c.mode == "hybrid"
    assert c.order(0.0) == []  # keyword-only: no file name matches
    assert c.order(0.5) and c.order(1.0)
    assert [h.score for h in c.order(0.5)] == sorted((h.score for h in c.order(0.5)),
                                                     reverse=True)


def test_empty_query_lists_by_path(conn):
    build(conn)
    out = search.search(conn, "", SearchFilters(folder="sfx/misc"))
    assert out["mode"] == "filter" and paths(out) == sorted(paths(out))


# ----------------------------------------------------------------------------- similar


def test_find_similar(conn):
    ids = build(conn)
    out = search.find_similar(conn, ids["sfx/dog/Dog_Bark_01.wav"], limit=3)
    assert out["source"]["path"] == "sfx/dog/Dog_Bark_01.wav" and out["model"] == "fake-a"
    got = paths(out)
    assert got[0] == "sfx/misc/xq_17.wav" and "sfx/dog/Dog_Bark_01.wav" not in got
    assert len(got) == 3
    r = out["results"][0]
    assert r["score"] == r["similarity"] and 0.5 < r["similarity"] <= 1
    sims = [x["similarity"] for x in out["results"]]
    assert sims == sorted(sims, reverse=True)


def test_find_similar_excludes_own_copies_and_collapses(conn):
    ids = build(conn)
    src = ids["sfx/impacts/Metal_Hit_03.wav"]
    got = paths(search.find_similar(conn, src, limit=10))
    assert not any(p.startswith("sfx/impacts/Metal_Hit_03") for p in got)
    other = add_asset(conn, "sfx/x/clang.wav", features=feats())
    fc.add_embedding(conn, other, "metal impact clang")
    got = paths(search.find_similar(conn, other, limit=10))
    assert sum(p.startswith("sfx/impacts/Metal_Hit_03") for p in got) == 1


def test_find_similar_filters_and_notes(conn):
    ids = build(conn)
    src = ids["sfx/dog/Dog_Bark_01.wav"]
    out = search.find_similar(conn, src, SearchFilters(is_music=True))
    assert paths(out) == ["music/Battle Loop.mp3"]
    plain = add_asset(conn, "sfx/plain.wav", features=feats())
    out = search.find_similar(conn, plain)
    assert out["results"] == [] and "no CLAP embedding yet" in out["note"]
    with pytest.raises(LookupError):
        search.find_similar(conn, 99999)
