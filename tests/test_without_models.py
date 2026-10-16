"""Without the optional `models` extra (torch, transformers): nothing imports torch, the
model stages are skipped with one note, search stays keyword-only and status/eval say why.

These run whether or not the extra is installed: the "not installed" cases replace
foley.models.missing_packages, so the real factory reports the extra as missing."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest
from conftest import LIBRARY_FILES

from foley import models, search, status, sync, vectors
from foley.catalog import db

N_AUDIO = len(LIBRARY_FILES)


def test_importing_every_module_never_imports_torch():
    code = textwrap.dedent("""
        import importlib, pkgutil, sys
        import foley
        names = [m.name for m in pkgutil.walk_packages(foley.__path__, "foley.")]
        for name in names:
            importlib.import_module(name)
        from foley import models, vectors
        models.missing_packages(); models.factory().unavailable(); vectors.clap_api()
        loaded = sorted(m for m in ("torch", "transformers") if m in sys.modules)
        print(len(names), loaded)
        sys.exit(1 if loaded else 0)
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr
    count = int(out.stdout.split()[0])
    assert count > 20  # every foley module, including browse, mcp_server and evaluate


@pytest.fixture
def no_extra(monkeypatch):
    """The real model factory on a machine without torch/transformers."""
    monkeypatch.setattr(models, "missing_packages", lambda: ["torch", "transformers"])
    models.set_factory(None)


def test_missing_packages_does_not_import():
    before = "torch" in sys.modules
    models.missing_packages()
    assert ("torch" in sys.modules) == before


def test_dependency_error_says_how_to_install(no_extra):
    msg = models.dependency_error()
    assert "torch, transformers" in msg and "uv sync --extra models" in msg
    assert models.factory().unavailable() == msg


def test_index_skips_model_stages_with_one_note(cfg, no_extra):
    lines: list[str] = []
    s = sync.run_index(cfg, progress=lines.append, workers=1)
    assert s.analyzed == N_AUDIO and s.errors == 0
    assert len(s.notes) == 1 and "models` extra" in s.notes[0]
    assert "embeddings" in s.notes[0] and "tags" in s.notes[0]
    assert sum(line.startswith("warning: model stages skipped") for line in lines) == 1
    conn = db.open_catalog(cfg)
    try:
        assert conn.execute("SELECT count(*) FROM assets WHERE status='active'").fetchone()[0] \
            == N_AUDIO
        note = conn.execute("SELECT note FROM index_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert "uv sync --extra models" in note
        st = status.catalog_status(cfg, conn)["models"]
        assert st["installed"] is False and "uv sync --extra models" in st["note"]
        assert st["tags"]["pending"] == N_AUDIO
    finally:
        conn.close()


def test_status_when_installed(cfg, fake_models):
    sync.run_index(cfg, workers=1)
    conn = db.open_catalog(cfg)
    try:
        st = status.catalog_status(cfg, conn)["models"]
        assert st["installed"] is True and st["note"] is None
    finally:
        conn.close()


def test_search_is_keyword_only_with_embeddings_but_no_extra(cfg, fake_models, monkeypatch):
    sync.run_index(cfg, workers=1)  # fake embeddings under the real CLAP model keys
    monkeypatch.setattr(models, "missing_packages", lambda: ["torch"])
    models.set_factory(None)
    vectors._reset_encoders()
    conn = db.open_catalog(cfg)
    try:
        assert vectors.embedding_models(conn)  # the catalog has embeddings
        assert vectors.clap_api() is None
        out = search.search(conn, "metal shelf hit")
        assert out["mode"] == "keyword" and "models` extra" in out["note"]
        assert out["results"]
    finally:
        conn.close()
