"""Sidecar JSON files beside audio (scan, sync, search, describe) and [library]
no_redistribute (config, MCP flags)."""

from __future__ import annotations

import json
import os

import pytest
from conftest import OLD_MTIME

from foley import config, mcp_server, scan, search, sync
from foley.catalog import db
from foley.config import Config

CLICK = "sfx/click.wav"
SIDECAR = "sfx/click.json"
PROMPT = "A wall of solid light rising: a bright glassy whoosh into a crystalline hum."


def _write_sidecar(library, data, rel=SIDECAR, t=OLD_MTIME):
    path = library / rel
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    os.utime(path, (t, t))
    return path


def _index(cfg, lines=None):
    return sync.run_index(cfg, stages=list(sync.CORE_STAGES), workers=1,
                          progress=None if lines is None else lines.append)


def _keyword(cfg, query):
    conn = db.open_catalog(cfg)
    try:
        res = search.search(conn, query, settings=search.SearchSettings(semantic=False))
        return [r["path"] for r in res["results"]]
    finally:
        conn.close()


def _describe(cfg, rel):
    conn = db.open_catalog(cfg)
    try:
        return search.describe(conn, rel)
    finally:
        conn.close()


@pytest.fixture
def reads(monkeypatch):
    seen = []
    real = scan.read_sidecar

    def counting(cfg, rel):
        seen.append(rel)
        return real(cfg, rel)

    monkeypatch.setattr(scan, "read_sidecar", counting)
    return seen


def test_scan_pairs_a_sidecar_with_its_audio_by_stem(cfg, library):
    _write_sidecar(library, {"prompt": PROMPT})
    _write_sidecar(library, {"prompt": "orphan"}, rel="sfx/nothing_here.json")
    _write_sidecar(library, {"prompt": "case"}, rel="sfx/lightsabers/LASRHIT3.json")
    files = {f.rel_path: f for f in scan.scan_library(cfg).audio}
    assert files[CLICK].sidecar.rel_path == SIDECAR
    assert files["sfx/lightsabers/lasrhit3.WAV"].sidecar.rel_path == \
        "sfx/lightsabers/LASRHIT3.json"
    assert sum(f.sidecar is not None for f in files.values()) == 2


def test_read_sidecar_and_text(cfg, library):
    _write_sidecar(library, {"prompt": PROMPT, "notes": ["a", " b "], "licence": "x",
                             "settings": {"prompt": "nested ignored"}})
    data, err = scan.read_sidecar(cfg, SIDECAR)
    assert err is None and data["prompt"] == PROMPT
    assert scan.sidecar_text(data) == f"{PROMPT} a b"
    _write_sidecar(library, "[1, 2]")
    assert scan.read_sidecar(cfg, SIDECAR) == (None, "not a JSON object")
    _write_sidecar(library, "{nope")
    assert scan.read_sidecar(cfg, SIDECAR)[1].startswith("not valid JSON")
    assert scan.read_sidecar(cfg, "sfx/missing.json")[1].startswith("unreadable")


def test_sidecar_prompt_is_searchable_and_described(cfg, library, reads):
    _write_sidecar(library, {"prompt": PROMPT, "notes": "Raise Barrier boss ability",
                             "licence": "use only inside a game"})
    _index(cfg)
    assert reads == [SIDECAR]
    assert _keyword(cfg, "crystalline whoosh")[:1] == [CLICK]
    assert _keyword(cfg, "barrier")[:1] == [CLICK]
    side = _describe(cfg, CLICK)["sidecar"]
    assert side["path"] == SIDECAR and side["data"]["licence"] == "use only inside a game"
    assert side["error"] is None
    assert _describe(cfg, "sfx/lightsabers/lasrhit3.WAV")["sidecar"] is None

    reads.clear()
    lines = []
    _index(cfg, lines)
    assert reads == []  # unchanged: not read again
    assert not any("sidecars read" in line for line in lines)


def test_sidecar_added_changed_and_removed_later(cfg, library, reads, monkeypatch):
    _index(cfg)
    assert _keyword(cfg, "crystalline") == []
    conn = db.open_catalog(cfg)
    aid = conn.execute("SELECT id FROM assets WHERE rel_path=?", (CLICK,)).fetchone()[0]
    features_before = conn.execute(
        "SELECT version FROM asset_stages WHERE asset_id=? AND stage='features'", (aid,)
    ).fetchone()
    conn.close()

    audio_reads = []
    real_read = sync._read
    monkeypatch.setattr(sync, "_read", lambda fn, *a: audio_reads.append(a) or real_read(fn, *a))

    _write_sidecar(library, {"prompt": PROMPT})  # added after the audio was indexed
    lines = []
    _index(cfg, lines)
    assert reads == [SIDECAR] and audio_reads == []  # only the sidecar was read
    assert any("1 sidecars read" in line and "1 to analyze" in line for line in lines)
    assert _keyword(cfg, "crystalline")[:1] == [CLICK]

    _write_sidecar(library, {"prompt": "a muddy footstep squelch"}, t=OLD_MTIME + 60)
    _index(cfg)
    assert _keyword(cfg, "crystalline") == []
    assert _keyword(cfg, "squelch")[:1] == [CLICK]

    os.remove(library / SIDECAR)
    _index(cfg)
    assert _keyword(cfg, "squelch") == []
    assert _describe(cfg, CLICK)["sidecar"] is None
    conn = db.open_catalog(cfg)
    try:
        assert conn.execute(
            "SELECT version FROM asset_stages WHERE asset_id=? AND stage='features'", (aid,)
        ).fetchone() == features_before
        assert conn.execute("SELECT status FROM assets WHERE id=?", (aid,)).fetchone()[0] == \
            "active"
    finally:
        conn.close()


def test_bad_sidecar_is_recorded_not_fatal(cfg, library):
    _write_sidecar(library, "{broken")
    summary = _index(cfg)
    assert not summary.interrupted
    side = _describe(cfg, CLICK)["sidecar"]
    assert side["data"] is None and side["error"].startswith("not valid JSON")
    assert _describe(cfg, CLICK)["status"] == "active"


# ----------------------------------------------------------------------------- licence


def test_no_redistribute_config(tmp_path):
    base = {"library": {"root": str(tmp_path / "lib")},
            "storage": {"data_dir": str(tmp_path / "data")}}
    cfg = config.from_dict(base)
    assert cfg.no_redistribute == [] and config.redistributable(cfg, "generated/x.wav")
    cfg = config.from_dict({**base, "library": {**base["library"],
                                                "no_redistribute": ["generated/ElevenLabs"]}})
    assert not config.redistributable(cfg, "generated/elevenlabs/sfx/a.wav")
    assert not config.redistributable(cfg, "Generated\\ElevenLabs\\a.wav")
    assert config.redistributable(cfg, "generated/elevenlabs2/a.wav")
    assert config.redistributable(cfg, "generated/suno/a.wav")
    with pytest.raises(config.ConfigError, match="no_redistribute"):
        config.from_dict({**base, "library": {**base["library"], "no_redistribute": "x"}})


def test_mcp_tools_flag_files_that_must_not_be_redistributed(cfg, library, tmp_path,
                                                             monkeypatch):
    _write_sidecar(library, {"prompt": PROMPT})
    _index(cfg)
    cfg.no_redistribute = ["sfx"]
    mcp_server.set_config(cfg)
    monkeypatch.setattr(mcp_server, "_settings",
                        lambda: search.SearchSettings(semantic=False))
    try:
        found = mcp_server.tool_search_audio("crystalline")
        assert found["results"][0]["path"] == CLICK
        assert found["results"][0]["no_redistribute"] is True
        assert mcp_server.tool_describe(CLICK)["no_redistribute"] is True
        cfg.no_redistribute = []
        assert "no_redistribute" not in mcp_server.tool_search_audio("crystalline")["results"][0]
        cfg.no_redistribute = ["sfx"]
        aid = mcp_server.tool_describe(CLICK)["id"]
        (tmp_path / "game").mkdir()
        out = mcp_server.tool_import_to_project(aid, str(tmp_path / "game"), "res://a.wav")
        assert out["no_redistribute"] is True
        assert any("never distribute" in w for w in out["warnings"])
    finally:
        mcp_server.close()


def test_default_config_flags_nothing(tmp_path):
    cfg = Config(library_root=tmp_path, data_dir=tmp_path / "d")
    assert config.redistributable(cfg, "generated/elevenlabs/a.wav")
