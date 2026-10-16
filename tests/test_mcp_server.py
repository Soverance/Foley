from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from unittest.mock import patch

import _fake_clap as fc
import pytest
from _catalog_builder import add_asset

from foley import browse, images, importer, mcp_server, status
from foley.catalog import db


@pytest.fixture
def server(cfg, monkeypatch):
    mcp_server.set_config(cfg)
    conn = db.open_catalog(cfg)
    add_asset(
        conn,
        "sfx/NailScratch19.wav",
        features={"duration_s": 1.0, "lufs_integrated": -20.0, "attack_s": 0.01, "is_music": 0},
        caption="1.00 s nail scratch",
    )
    add_asset(conn, "music/Battle Loop.mp3", features={"duration_s": 8.0, "is_music": 1})
    conn.close()

    def nope(c):
        raise NotImplementedError

    monkeypatch.setattr(status, "staleness_note", nope)
    yield mcp_server.build_server()
    mcp_server.close()


def run(coro):
    return asyncio.run(coro)


def test_tools_registered_with_claude_facing_descriptions(server):
    tools = {t.name: t for t in run(server.list_tools())}
    assert set(tools) == {
        "search_audio",
        "find_similar",
        "describe",
        "render_comparison",
        "import_to_project",
        "catalog_status",
        "get_shortlist",
    }
    assert "foley browse" in tools["get_shortlist"].description
    assert "keyword" in tools["search_audio"].description.lower()
    assert (
        "LUFS" in tools["search_audio"].description
        and "seconds" in tools["search_audio"].description
    )
    assert "Read tool" in tools["render_comparison"].description
    assert "CLAP" in tools["find_similar"].description
    assert tools["find_similar"].input_schema["required"] == ["asset_id"]
    assert {"max_duration_s", "is_music", "project", "limit"} <= set(
        tools["find_similar"].input_schema["properties"])
    assert "dBTP" in tools["import_to_project"].description
    props = tools["search_audio"].input_schema["properties"]
    assert {
        "query",
        "category",
        "pack",
        "is_music",
        "min_duration_s",
        "max_attack_s",
        "project",
        "limit",
    } <= set(props)
    assert tools["search_audio"].input_schema["required"] == ["query"]
    assert set(tools["import_to_project"].input_schema["required"]) == {
        "asset_id",
        "project_root",
        "dest",
    }


def test_search_and_describe_through_server(server):
    res = run(server.call_tool("search_audio", {"query": "nail scratch", "limit": 5}))
    assert not res.is_error
    data = payload(res)
    assert data["results"][0]["path"] == "sfx/NailScratch19.wav"
    desc = payload(run(server.call_tool("describe", {"asset": "sfx/NailScratch19.wav"})))
    assert desc["caption"] == "1.00 s nail scratch"
    by_id = payload(run(server.call_tool("describe", {"asset": desc["id"]})))
    assert by_id["path"] == desc["path"]


def test_tool_functions_filters(server):
    out = mcp_server.tool_search_audio("", is_music=True)
    assert [r["path"] for r in out["results"]] == ["music/Battle Loop.mp3"]
    assert mcp_server.tool_search_audio("", min_duration_s=5, max_duration_s=9)["results"]


def test_errors_are_concise_tool_errors(server):
    with pytest.raises(mcp_server.ToolError, match="no such asset"):
        mcp_server.tool_describe(12345)
    with pytest.raises(mcp_server.ToolError, match="between 2 and 6"):
        mcp_server.tool_render_comparison([1])
    with pytest.raises(mcp_server.ToolError, match="no such asset"):
        mcp_server.tool_render_comparison([1, 999])
    with pytest.raises(mcp_server.ToolError, match="not available"):
        with patch.object(status, "catalog_status", side_effect=NotImplementedError):
            mcp_server.tool_catalog_status()


def test_hybrid_search_and_find_similar_through_server(server, cfg, monkeypatch):
    fc.install(monkeypatch)
    conn = db.open_catalog(cfg)
    ids = {r[1]: r[0] for r in conn.execute("SELECT id, rel_path FROM assets")}
    fc.add_embedding(conn, ids["sfx/NailScratch19.wav"], "nail scratch metal")
    fc.add_embedding(conn, ids["music/Battle Loop.mp3"], "orchestral drums battle")
    other = add_asset(conn, "sfx/zz_01.wav", features={"duration_s": 0.5, "is_music": 0})
    fc.add_embedding(conn, other, "metal nail scratch screech")
    conn.close()
    data = payload(run(server.call_tool("search_audio", {"query": "metal screech"})))
    assert data["mode"] == "hybrid" and data["model"] == "fake-a"
    assert "sfx/zz_01.wav" in [r["path"] for r in data["results"]]
    sim = payload(run(server.call_tool(
        "find_similar", {"asset_id": ids["sfx/NailScratch19.wav"], "is_music": False})))
    assert [r["path"] for r in sim["results"]] == ["sfx/zz_01.wav"]
    assert sim["results"][0]["similarity"] > 0.5
    assert sim["source"]["id"] == ids["sfx/NailScratch19.wav"]
    with pytest.raises(mcp_server.ToolError, match="no such asset"):
        mcp_server.tool_find_similar(99999)
    # [search] blend_weight from the config reaches search: 0 = keyword only, no note
    cfg.search_blend_weight = 0.0
    data = mcp_server.tool_search_audio("metal screech")
    assert data["mode"] == "keyword" and data["note"] is None


def test_tool_error_over_protocol_has_no_traceback(server):
    try:
        res = run(server.call_tool("describe", {"asset": 12345}))
    except Exception as e:  # noqa: BLE001 - SDK versions differ in raise-vs-result
        assert "Traceback" not in str(e) and "no such asset" in str(e)
    else:
        assert res.is_error
        assert "Traceback" not in json.dumps(res.model_dump(), default=str)


def test_render_comparison_and_import_delegate(server, cfg, tmp_path, monkeypatch):
    conn = db.open_catalog(cfg)
    ids = [r[0] for r in conn.execute("SELECT id FROM assets ORDER BY id")]
    conn.close()
    png = tmp_path / "grid.png"
    png.write_bytes(b"png")
    seen = {}

    def fake_render(c, conn, asset_ids):
        seen["ids"] = asset_ids
        return png

    monkeypatch.setattr(images, "render_comparison", fake_render)
    out = mcp_server.tool_render_comparison(ids)
    assert out["image"] == str(png.resolve()) and seen["ids"] == ids
    assert [a["id"] for a in out["assets"]] == ids

    def fake_import(c, conn, aid, root, dest, **kw):
        seen["import"] = (aid, str(root), dest, kw)
        return {"project_path": dest}

    monkeypatch.setattr(importer, "import_to_project", fake_import)
    res = run(
        server.call_tool(
            "import_to_project",
            {
                "asset_id": ids[0],
                "project_root": str(tmp_path),
                "dest": "res://a.wav",
                "usage_key": "sfx.x",
            },
        )
    )
    assert payload(res) == {"project_path": "res://a.wav"}
    assert seen["import"][3] == {
        "usage_key": "sfx.x",
        "target_lufs": None,
        "overwrite": False,
    }


def test_get_shortlist_returns_starred_items(server, cfg):
    assert payload(run(server.call_tool("get_shortlist", {}))) == {"count": 0, "items": []}
    conn = db.open_catalog(cfg)
    aid = conn.execute(
        "SELECT id FROM assets WHERE rel_path = 'sfx/NailScratch19.wav'"
    ).fetchone()[0]
    conn.close()
    browse.Shortlist(browse.shortlist_path(cfg)).set(
        "hash-sfx/NailScratch19.wav", "old/place/NailScratch19.wav", True, "for boss hit?"
    )
    browse.Shortlist(browse.shortlist_path(cfg)).set(
        "hash-gone", "sfx/deleted.wav", True, None
    )
    data = payload(run(server.call_tool("get_shortlist", {})))
    assert data["count"] == 2
    by_hash = {it["content_hash"]: it for it in data["items"]}
    item = by_hash["hash-sfx/NailScratch19.wav"]
    assert item["id"] == aid and item["path"] == "sfx/NailScratch19.wav"  # follows the move
    assert item["note"] == "for boss hit?" and item["caption"] == "1.00 s nail scratch"
    assert item["duration_s"] == 1.0 and item["lufs"] == -20.0
    gone = by_hash["hash-gone"]
    assert gone["id"] is None and gone["path"] == "sfx/deleted.wav"

    browse.shortlist_path(cfg).write_text("{not json", encoding="utf-8")
    with pytest.raises(mcp_server.ToolError, match="shortlist file is unreadable"):
        mcp_server.tool_get_shortlist()


def test_connection_reopens_when_catalog_replaced(server, cfg):
    first = mcp_server.get_conn()
    assert mcp_server.get_conn() is first
    mcp_server.close()
    cfg.catalog_path.unlink()
    for suffix in ("-wal", "-shm"):
        p = cfg.catalog_path.with_name(cfg.catalog_path.name + suffix)
        p.unlink(missing_ok=True)
    second = mcp_server.get_conn()
    assert second is not first
    assert second.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0


def test_foley_mcp_starts_and_answers_initialize(cfg, tmp_path):
    config = tmp_path / "foley.toml"
    config.write_text(
        f'[library]\nroot = "{cfg.library_root.as_posix()}"\n'
        f'[storage]\ndata_dir = "{cfg.data_dir.as_posix()}"\n',
        encoding="utf-8",
    )
    env = {**os.environ, "FOLEY_CONFIG": str(config)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "foley.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        }
        proc.stdin.write((json.dumps(init) + "\n").encode())
        proc.stdin.flush()
        deadline = time.time() + 30
        line = b""
        import threading

        holder = {}
        t = threading.Thread(
            target=lambda: holder.setdefault("line", proc.stdout.readline()), daemon=True
        )
        t.start()
        t.join(max(0.1, deadline - time.time()))
        line = holder.get("line", b"")
        assert line, "no initialize response within 30 s"
        reply = json.loads(line)
        assert reply["id"] == 1
        assert reply["result"]["serverInfo"]["name"] == "foley"
    finally:
        proc.kill()
        proc.wait(timeout=10)


def payload(res):
    if res.structured_content is not None:
        return res.structured_content
    return json.loads(res.content[0].text)
