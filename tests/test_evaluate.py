from __future__ import annotations

import json

import _fake_clap as fc
import pytest
from _catalog_builder import add_asset

from foley import cli, evaluate, search, status, vectors
from foley.catalog import db


@pytest.fixture(autouse=True)
def _no_status(monkeypatch):
    monkeypatch.setattr(status, "staleness_note", lambda conn: None)


@pytest.fixture
def api(monkeypatch):
    return fc.install(monkeypatch)


SOUNDS = {
    "sfx/dog/Dog_Bark_01.wav": ("dog bark animal", 1.0),
    "sfx/misc/xq_17.wav": ("dog bark animal growl", 0.8),
    "sfx/misc/zz_04.wav": ("glass shatter break", 2.0),
    "sfx/impacts/Metal_Hit_03.wav": ("metal impact clang", 0.5),
    "sfx/impacts/Metal_Hit_03_copy.wav": ("metal impact clang", 0.5),
    "sfx/misc/aa_99.wav": ("wind howl ambience", 6.0),
    "sfx/gone.wav": ("wind", 1.0),
}

TESTSET = """# synthetic test set
# header kept

[[query]]
text = "dog bark"
cue = "sfx:dog"
good = ["sfx/dog/Dog_Bark_01.wav"]

[[query]]
text = "growling animal"
good = ["sfx/misc/xq_17.wav"]

[[query]]
text = "metal clang impact"
good = ["sfx/impacts/Metal_Hit_03_copy.wav"]

[[query]]
text = "sci-fi shotgun blast"
cue = "sfx:fire_scatter"
good = []

[[query]]
text = "howling wind ambience"
good = ["sfx/misc/aa_99.wav", "sfx/not_there.wav", "sfx/gone.wav"]

[[query]]
text = "dog bark"
good = ["sfx/misc/xq_17.wav"]
filters = { max_duration_s = 0.9 }
"""


@pytest.fixture
def conn(cfg, api):
    c = db.open_catalog(cfg)
    for rel, (words, dur) in SOUNDS.items():
        h = "hash-metal" if "Metal_Hit_03" in rel else "auto"
        aid = add_asset(c, rel, features={"duration_s": dur, "is_music": 0}, content_hash=h,
                        status="missing" if rel == "sfx/gone.wav" else "active")
        fc.add_embedding(c, aid, words, "fake-a")
        fc.add_embedding(c, aid, words, "fake-b")
    yield c
    c.close()


@pytest.fixture
def testset_path(tmp_path):
    p = tmp_path / "testset.toml"
    p.write_text(TESTSET, encoding="utf-8")
    return p


def by_name(report, name):
    return next(c for c in report["configs"] if c["name"] == name)


def test_run_reports_keyword_and_each_checkpoint_weight(cfg, conn, testset_path):
    report = evaluate.run(cfg, conn, testset_path, weights=[0, 0.5, 1])
    assert report["queries_total"] == 6 and report["queries_answered"] == 5
    assert report["skipped"] == ["sci-fi shotgun blast"]
    assert [c["name"] for c in report["configs"]] == [
        "keyword only", "fake-a w=0.5", "fake-a w=1", "fake-b w=0.5", "fake-b w=1"]
    kw = by_name(report, "keyword only")
    ranks = {p["index"]: p["rank"] for p in kw["per_query"]}
    assert ranks[1] == 1  # filename match
    assert ranks[2] is None  # no file name says "growling"
    assert ranks[3] == 1  # the listed copy collapsed into its twin: counts via content_hash
    assert ranks[6] is None  # the filter drops Dog_Bark_01 (1.0 s); xq_17 has no keyword
    hy = by_name(report, "fake-a w=0.5")
    hy_ranks = {p["index"]: p["rank"] for p in hy["per_query"]}
    assert hy_ranks[2] is not None and hy_ranks[6] == 1  # filters apply to vector hits too
    assert hy["recall"] > kw["recall"] and report["hybrid_beats_keyword"]
    assert report["recommendation"]["clap_model"] in ("fake-a", "fake-b")
    assert report["recommendation"]["blend_weight"] > 0
    assert report["models"]["fake-a"] == {"key": fc.model_key("fake-a"), "embeddings": 7,
                                          "status": "ok"}
    w = "\n".join(report["warnings"])
    assert "not in the catalog: sfx/not_there.wav" in w and "missing" in w
    assert "sfx/gone.wav" in w
    json.dumps(report)  # serializable


def test_format_report(cfg, conn, testset_path):
    report = evaluate.run(cfg, conn, testset_path, clap=["fake-a"], weights=[0, 0.5])
    text = evaluate.format_report(report)
    assert "5 of 6 queries have answers (1 skipped)" in text
    assert "keyword only" in text and "fake-a w=0.5" in text and "recall@10" in text
    assert "beats keyword-only" in text
    assert "misses for" in text and "warnings:" in text


def test_adopt_writes_meta_used_by_search(cfg, conn, testset_path):
    report = evaluate.run(cfg, conn, testset_path, clap=["fake-b"], weights=[0, 0.3])
    path = evaluate.write_report(cfg, report)
    assert path.parent == cfg.data_dir / "eval" and json.loads(path.read_text("utf-8"))
    rec = evaluate.adopt(conn, report)
    assert rec == {"clap_model": "fake-b", "blend_weight": 0.3,
                   "reason": "hybrid fake-b w=0.3 beats keyword-only"}
    assert db.get_meta(conn, "search_clap_model") == "fake-b"
    assert db.get_meta(conn, "search_blend_weight") == "0.3"
    summary = json.loads(db.get_meta(conn, "search_eval"))
    assert summary["report"] == str(path) and summary["best_hybrid"]["weight"] == 0.3
    r = search.resolve(conn)
    assert (r.clap_model, r.blend_weight, r.model_source) == ("fake-b", 0.3, "meta")


def test_keyword_only_kept_when_hybrid_does_not_beat_it(cfg, conn, tmp_path):
    p = tmp_path / "easy.toml"
    p.write_text('[[query]]\ntext = "dog bark"\ngood = ["sfx/dog/Dog_Bark_01.wav"]\n',
                 encoding="utf-8")
    report = evaluate.run(cfg, conn, p, clap=["fake-a"], weights=[0, 0.5])
    assert not report["hybrid_beats_keyword"]
    assert report["best"]["name"] == "keyword only"  # ties go to keyword-only
    rec = evaluate.adopt(conn, report)
    assert rec["blend_weight"] == 0.0 and "keyword-only" in rec["reason"]
    assert db.get_meta(conn, "search_blend_weight") == "0"
    out = search.search(conn, "growling animal")
    assert out["mode"] == "keyword" and out["note"] is None


def test_errors_and_unknown_checkpoints(cfg, conn, tmp_path, testset_path):
    with pytest.raises(evaluate.EvalError, match="no test set"):
        evaluate.run(cfg, conn, None)
    with pytest.raises(evaluate.EvalError, match="not found"):
        evaluate.run(cfg, conn, tmp_path / "nope.toml")
    empty = tmp_path / "empty.toml"
    empty.write_text('[[query]]\ntext = "x"\ncue = "c"\ngood = []\n', encoding="utf-8")
    with pytest.raises(evaluate.EvalError, match="none of the 1 queries"):
        evaluate.run(cfg, conn, empty)
    with pytest.raises(evaluate.EvalError, match="between 0 and 1"):
        evaluate.run(cfg, conn, testset_path, weights=[2])
    report = evaluate.run(cfg, conn, testset_path, clap=["fake-a", "bogus"], weights=[0, 1])
    assert report["models"]["bogus"]["status"] == "unknown checkpoint"
    assert any("bogus" in w for w in report["warnings"])
    cfg.testset = testset_path
    assert evaluate.run(cfg, conn, None, weights=[0])["configs"][0]["name"] == "keyword only"


def test_without_embeddings_reports_keyword_only(cfg, testset_path, api):
    c = db.open_catalog(cfg)
    try:
        add_asset(c, "sfx/dog/Dog_Bark_01.wav", features={"duration_s": 1.0})
        report = evaluate.run(cfg, c, testset_path)
        assert [x["name"] for x in report["configs"]] == ["keyword only"]
        assert report["recommendation"]["clap_model"] is None
        assert "none with embeddings" in evaluate.format_report(report)
    finally:
        c.close()


def test_cli_eval(cfg, conn, testset_path, tmp_path, capsys):
    toml = tmp_path / "foley.toml"
    toml.write_text(
        f'[library]\nroot = "{cfg.library_root.as_posix()}"\n'
        f'[storage]\ndata_dir = "{cfg.data_dir.as_posix()}"\n'
        f'[eval]\ntestset = "{testset_path.as_posix()}"\n'
        '[search]\nblend_weight = 0.5\n', encoding="utf-8")
    assert cli.main(["--config", str(toml), "eval", "--clap", "fake-a", "--weights",
                     "0,0.5", "--adopt"]) == 0
    out = capsys.readouterr().out
    assert "keyword only" in out and "report: " in out and "adopted: " in out
    assert "overrides the adopted value" in out
    assert cli.main(["--config", str(toml), "eval", "--weights", "0", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["queries_answered"] == 5 and data["adopted"] is None
    assert cli.main(["--config", str(toml), "eval", "--weights", "x"]) == 2


def test_without_clap_installed(cfg, conn, testset_path, monkeypatch):
    monkeypatch.setattr(vectors, "clap_api", lambda: None)  # no `models` extra
    report = evaluate.run(cfg, conn, testset_path, weights=[0, 0.5])
    assert [c["name"] for c in report["configs"]] == ["keyword only"]
    assert any("CLAP is not installed" in w for w in report["warnings"])
    assert report["recommendation"] == {"clap_model": None, "blend_weight": 0.0,
                                        "reason": "CLAP is not installed: keyword-only"}


def test_adopt_refuses_without_a_clap_comparison(cfg, conn, testset_path, monkeypatch):
    db.set_meta(conn, "search_clap_model", "fake-b")
    db.set_meta(conn, "search_blend_weight", "0.3")
    monkeypatch.setattr(vectors, "clap_api", lambda: None)  # a machine without the extra
    report = evaluate.run(cfg, conn, testset_path, weights=[0, 0.5])
    with pytest.raises(evaluate.EvalError, match="nothing to adopt"):
        evaluate.adopt(conn, report)
    assert db.get_meta(conn, "search_clap_model") == "fake-b"  # tuned values untouched
    assert db.get_meta(conn, "search_blend_weight") == "0.3"
