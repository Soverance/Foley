"""The shared MCP endpoint that `foley browse` serves at /mcp (foley.mcp_http)."""

from __future__ import annotations

import asyncio
import http.client
import json
import threading
from pathlib import Path

import pytest
from _catalog_builder import add_asset

from foley import cli, config, images, mcp_http, status
from foley.catalog import db
from foley.config import Config


@pytest.fixture(autouse=True)
def _quiet_status(monkeypatch):
    monkeypatch.setattr(status, "staleness_note", lambda conn: None)


def _serve(cfg):
    from test_browse import fake_scans

    from foley import browse

    server = browse.make_server(cfg, "127.0.0.1", 0, scans=fake_scans(cfg))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _stop(server):
    server.shutdown()
    if server.app.mcp is not None:
        server.app.mcp.stop()
    server.server_close()


@pytest.fixture
def shared(cfg, tmp_path):
    cfg.mcp_enabled = True
    cfg.mcp_project_roots = [tmp_path / "projects"]
    conn = db.open_catalog(cfg)
    a = add_asset(conn, "sfx/NailScratch19.wav",
                  features={"duration_s": 1.0, "lufs_integrated": -20.0, "is_music": 0},
                  caption="1.00 s nail scratch")
    b = add_asset(conn, "music/Battle Loop.mp3", features={"duration_s": 8.0, "is_music": 1})
    conn.close()
    server = _serve(cfg)
    try:
        yield cfg, server, (a, b)
    finally:
        _stop(server)


def _url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/mcp"


async def _session_call(url, fn, headers=None):
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    async with create_mcp_http_client(headers=headers) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)


def call(url, fn, headers=None):
    return asyncio.run(_session_call(url, fn, headers))


def raw(server, method="POST", body=b"{}", headers=None):
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=60)
    try:
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream", **(headers or {})}
        c.request(method, "/mcp", body=body if method == "POST" else None, headers=h)
        r = c.getresponse()
        return r, r.read()
    finally:
        c.close()


# ----------------------------------------------------------------------------- config


def test_mcp_config_section(tmp_path):
    base = {"library": {"root": str(tmp_path / "lib")},
            "storage": {"data_dir": str(tmp_path / "data")}}
    cfg = config.from_dict(base)
    assert cfg.mcp_enabled is False and cfg.mcp_project_roots == []
    cfg = config.from_dict({**base, "mcp": {"enabled": True,
                                            "project_roots": [str(tmp_path / "games")]}})
    assert cfg.mcp_enabled and cfg.mcp_project_roots == [tmp_path / "games"]
    assert config.in_project_roots(cfg, tmp_path / "games" / "my-game")
    assert config.in_project_roots(cfg, tmp_path / "games")
    assert not config.in_project_roots(cfg, tmp_path / "gamesX")
    assert not config.in_project_roots(cfg, tmp_path / "games" / ".." / "elsewhere")
    with pytest.raises(config.ConfigError, match="mcp.enabled"):
        config.from_dict({**base, "mcp": {"enabled": "yes"}})
    with pytest.raises(config.ConfigError, match="project_roots"):
        config.from_dict({**base, "mcp": {"project_roots": "C:/games"}})


# ----------------------------------------------------------------------------- token


def test_token_created_once_and_checked(tmp_path):
    cfg = Config(library_root=tmp_path / "lib", data_dir=tmp_path / "data")
    token = mcp_http.load_or_create_token(cfg)
    assert len(token) >= 32
    assert mcp_http.load_or_create_token(cfg) == token
    assert mcp_http.token_path(cfg).read_text().strip() == token
    assert mcp_http.authorized(f"Bearer {token}", token)
    assert mcp_http.authorized(f"bearer  {token} ", token)
    assert not mcp_http.authorized(f"Bearer {token}x", token)
    assert not mcp_http.authorized(token, token)
    assert not mcp_http.authorized(None, token)
    assert not mcp_http.authorized("Bearer ", "")


def test_cli_prints_token(cfg, capsys, monkeypatch):
    monkeypatch.setattr(config, "load", lambda explicit=None: cfg)
    assert cli.main(["mcp", "token"]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed == mcp_http.token_path(cfg).read_text().strip()


# ----------------------------------------------------------------------------- endpoint


def test_session_lists_and_calls_tools(shared):
    cfg, server, (a, _b) = shared

    async def go(session):
        tools = await session.list_tools()
        found = await session.call_tool("search_audio", {"query": "nail scratch"})
        status_ = await session.call_tool("catalog_status", {})
        return tools, found, status_

    tools, found, status_ = call(_url(server), go)
    assert {t.name for t in tools.tools} >= {"search_audio", "render_comparison",
                                             "import_to_project", "get_shortlist"}
    assert not found.is_error
    data = json.loads(found.content[0].text)
    assert [r["id"] for r in data["results"]][:1] == [a]
    assert not status_.is_error


def test_render_comparison_inline_without_server_path(shared, tmp_path, monkeypatch):
    cfg, server, (a, b) = shared
    png = tmp_path / "grid.png"
    png.write_bytes(b"\x89PNG fake")
    monkeypatch.setattr(images, "render_comparison", lambda c, conn, ids: png)

    res = call(_url(server), lambda s: s.call_tool("render_comparison", {"asset_ids": [a, b]}))
    assert not res.is_error
    image, text = res.content
    assert image.type == "image" and image.mime_type == "image/png"
    import base64

    assert base64.b64decode(image.data) == png.read_bytes()
    info = json.loads(text.text)
    assert [x["id"] for x in info["assets"]] == [a, b]
    assert "image" not in info  # no server-side path over the shared endpoint


def test_import_only_into_project_roots(shared, tmp_path, monkeypatch):
    cfg, server, (a, _b) = shared
    from foley import importer

    seen = []
    monkeypatch.setattr(importer, "import_to_project",
                        lambda c, conn, aid, root, dest, **kw: seen.append(root) or
                        {"project_path": dest})
    inside = tmp_path / "projects" / "game"
    outside = tmp_path / "elsewhere" / "game"

    async def go(session):
        ok = await session.call_tool("import_to_project", {
            "asset_id": a, "project_root": str(inside), "dest": "res://sfx/a.wav"})
        no = await session.call_tool("import_to_project", {
            "asset_id": a, "project_root": str(outside), "dest": "res://sfx/a.wav"})
        return ok, no

    ok, no = call(_url(server), go)
    assert not ok.is_error and seen == [Path(inside)]
    assert no.is_error and "project_roots" in no.content[0].text
    assert "Traceback" not in no.content[0].text
    assert len(seen) == 1


def test_token_required_when_set(shared):
    cfg, server, _ = shared
    server.app.mcp_token = "s3cret-token"  # what a non-loopback bind sets up
    r, body = raw(server, body=b'{"jsonrpc":"2.0","id":1,"method":"ping"}')
    assert r.status == 401 and r.getheader("WWW-Authenticate", "").startswith("Bearer")
    assert "foley mcp token" in json.loads(body)["error"]
    r, _ = raw(server, headers={"Authorization": "Bearer wrong"})
    assert r.status == 401
    tools = call(_url(server), lambda s: s.list_tools(),
                 headers={"Authorization": "Bearer s3cret-token"})
    assert tools.tools


def test_methods_origin_host_and_size(shared):
    cfg, server, _ = shared
    r, _ = raw(server, method="GET")
    assert r.status == 405 and r.getheader("Allow") == "POST"
    r, _ = raw(server, method="DELETE")
    assert r.status == 405
    r, _ = raw(server, headers={"Origin": "http://evil.example"})
    assert r.status == 403
    r, _ = raw(server, headers={"Host": "evil.example"})
    assert r.status == 403
    # refused from the header alone, before any body is read
    r, _ = raw(server, headers={"Content-Length": str(mcp_http.MAX_BODY_BYTES + 1)})
    assert r.status == 413 and r.getheader("Connection") == "close"
    r, body = raw(server, body=b"not json")
    assert 400 <= r.status < 500 or b"error" in body


def test_endpoint_off_by_default(cfg):
    server = _serve(cfg)
    try:
        r, body = raw(server, body=b'{"jsonrpc":"2.0","id":1,"method":"ping"}')
        assert r.status == 404 and "[mcp] enabled" in json.loads(body)["error"]
        assert server.app.mcp is None
    finally:
        _stop(server)


def test_no_token_needed_on_loopback_bind(shared):
    cfg, server, _ = shared
    assert server.app.mcp_token is None
    assert not mcp_http.token_path(cfg).exists()


def test_worker_restarts_after_stop(shared):
    cfg, server, _ = shared
    call(_url(server), lambda s: s.list_tools())
    server.app.mcp.stop()
    tools = call(_url(server), lambda s: s.list_tools())
    assert tools.tools
