from __future__ import annotations

import io
import json
import os
import random
import struct
import subprocess
import sys

import audiogen as ag
import pytest
import soundfile as sf
from conftest import LIBRARY_FILES, OLD_MTIME

from foley import scan
from foley.config import Config

# --------------------------------------------------------------------------- scan_library


def test_scan_fixture_library(cfg, library):
    result = scan.scan_library(cfg)
    assert [f.rel_path for f in result.audio] == sorted(LIBRARY_FILES)
    assert [f.rel_path for f in result.archives] == ["packs/Test Bundle 2026/Extras.zip"]
    assert result.errors == []
    laser = next(f for f in result.audio if f.rel_path.endswith("lasrhit3.WAV"))
    st = (library / laser.rel_path).stat()
    assert laser.ext == ".wav"
    assert (laser.size_bytes, laser.mtime_ns) == (st.st_size, st.st_mtime_ns)
    assert laser.mtime_ns == OLD_MTIME * 10**9


def test_scan_skips_hidden_system_and_quarantine(cfg, library):
    ag.write(library / ".hidden/a.wav", ag.click())
    ag.write(library / "sfx/._resource.wav", ag.click())
    ag.write(library / "@eaDir/thumb.wav", ag.click())
    ag.write(library / "#recycle/old.wav", ag.click())
    ag.write(library / "Quarantine/sfx/dup.wav", ag.click())
    ag.write(library / "sfx/UPPER.FLAC", ag.click())
    (library / "sfx/notes.pdf").write_bytes(b"%PDF")
    if sys.platform == "win32":
        ag.write(library / "sfx/winhidden.wav", ag.click())
        ag.write(library / "syshidden/x.wav", ag.click())
        subprocess.run(["attrib", "+h", str(library / "sfx/winhidden.wav")], check=True)
        subprocess.run(["attrib", "+s", str(library / "syshidden")], check=True)
    cfg.quarantine_root = library / "Quarantine"
    paths = [f.rel_path for f in scan.scan_library(cfg).audio]
    assert "sfx/UPPER.FLAC" in paths
    assert len(paths) == len(LIBRARY_FILES) + 1


def test_scan_reports_unreadable_directories(cfg, library, monkeypatch):
    real = os.scandir
    blocked = os.path.normcase(str(library / "voice"))

    def scandir(path="."):
        if os.path.normcase(os.fspath(path)) == blocked:
            raise PermissionError(13, "Access is denied")
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    result = scan.scan_library(cfg)
    assert [e[0] for e in result.errors] == ["voice"]
    assert not any(f.rel_path.startswith("voice/") for f in result.audio)


def test_scan_missing_root_is_an_error(tmp_path):
    cfg = Config(library_root=tmp_path / "nope", data_dir=tmp_path / "data")
    result = scan.scan_library(cfg)
    assert result.audio == [] and result.errors[0][0] == "."


# --------------------------------------------------------------------------- classify_path


@pytest.mark.parametrize(
    "rel, expected",
    [
        ("packs/Pack A/x/y/f.wav", ("Pack A", "x/y")),
        ("packs/Pack A/f.wav", ("Pack A", None)),
        ("Packs/Pack A/sub/f.wav", ("Pack A", "sub")),
        ("packs/f.wav", (None, "packs")),
        ("sfx/lightsabers/f.wav", (None, "sfx/lightsabers")),
        ("f.wav", (None, None)),
        ("vendor/bundles/Big/a/f.wav", ("Big", "a")),
    ],
)
def test_classify_path(tmp_path, rel, expected):
    cfg = Config(library_root=tmp_path, pack_roots=["packs", "vendor/bundles"])
    assert scan.classify_path(cfg, rel) == expected


# --------------------------------------------------------------------------- UCS


def test_parse_ucs_contract_example():
    got = scan.parse_ucs("GOREMisc_Cladding_NailScratch19_InMotionAudio_SinisterTextures5")
    assert got == {
        "cat_id": "GOREMisc",
        "category": "GORE",
        "subcategory": "MISC",
        "fx_name": "Cladding NailScratch19",
        "creator_id": "InMotionAudio",
        "source_id": "SinisterTextures5",
    }


def test_parse_ucs_variants():
    amb = scan.parse_ucs("AMBUrbn_Street Traffic Busy_JD_CityPack")
    assert amb["category"] == "AMBIENCE" and amb["fx_name"] == "Street Traffic Busy"
    assert (amb["creator_id"], amb["source_id"]) == ("JD", "CityPack")
    user = scan.parse_ucs("DOORWood-Creaky_Old Door Open_ME_Lib")
    assert user["cat_id"] == "DOORWood" and user["user_category"] == "Creaky"
    short = scan.parse_ucs("UIClick_Soft")
    assert short["fx_name"] == "Soft" and short["creator_id"] is None
    assert scan.parse_ucs("WOODHndl_Grab_ME_Lib")["subcategory"]  # the v8.2.1 fix
    assert scan.parse_ucs("GOREMISC_Splat_ME_Lib")["cat_id"] == "GOREMisc"


@pytest.mark.parametrize(
    "stem",
    ["ui_scifi_traditional_confirm", "lasrhit3", "Metal Shelf Hit 10", "Water_Fountain_Fienup_002",
     "GOREMisc", "XYZAbc_thing_me_lib", "", "_"],
)
def test_parse_ucs_rejects_non_ucs(stem):
    assert scan.parse_ucs(stem) is None


def test_ucs_list_is_bundled():
    table = scan._ucs_table()
    assert len(table) > 700
    assert table["AMBUrbn"][0] == "AMBIENCE"


# --------------------------------------------------------------------------- kind_hint


@pytest.mark.parametrize(
    "rel, meta, expected",
    [
        ("music/Arteria3D/Battle Loop.mp3", None, "music"),
        ("ambiance/forest/birds.wav", None, "ambience"),
        ("voice/male-npc/hello.wav", None, "voice"),
        ("sfx/click.wav", None, "sfx"),
        ("packs/P/03_weather/ambient_wind_through_trees_01_lp.ogg", None, "ambience"),
        ("packs/P/x/AMBUrbn_Traffic_JD_Lib.wav", None, "ambience"),
        ("packs/P/x/VOXScrm_Yell_JD_Lib.wav", None, "voice"),
        ("packs/P/x/MUSCBell_Ding_JD_Lib.wav", None, "sfx"),
        ("packs/P/x/MUSCLoop_Drums Groove_JD_Lib.wav", None, "music"),
        ("packs/P/x/GOREMisc_Splat_JD_Lib.wav", None, "sfx"),
        ("packs/P/x/whatever.wav", {"ucs": {"cat_id": "AMBForst"}}, "ambience"),
        ("packs/P/Metal Shelf/Metal Shelf Hit 10.wav", None, None),
        ("packs/P/x/HeroThemeLoop.wav", None, "music"),
    ],
)
def test_kind_hint(rel, meta, expected):
    assert scan.kind_hint(rel, meta) == expected


# --------------------------------------------------------------------------- licenses


def test_find_license(cfg, library):
    scan.clear_license_cache()
    rel = "packs/Test Bundle 2015/Metal Shelf/Metal Shelf Hit 10.wav"
    assert scan.find_license(cfg, rel) == "packs/Test Bundle 2015/LICENSE.txt"
    assert scan.find_license(cfg, "sfx/click.wav") is None
    assert scan.find_license(cfg, "packs/Test Bundle 2026/Sinister Textures/x.wav") is None


def test_find_license_patterns_and_nearest(cfg, library):
    (library / "packs/Oculus Like Pack/Please Read.pdf").write_bytes(b"%PDF")
    (library / "packs/Oculus Like Pack/05_user_interface/ui_license_terms.md").write_text("x")
    (library / "packs/Oculus Like Pack/03_weather/License Plate Rattle.wav").write_bytes(b"x")
    scan.clear_license_cache()
    assert scan.find_license(cfg, "packs/Oculus Like Pack/05_user_interface/a.wav") == (
        "packs/Oculus Like Pack/05_user_interface/ui_license_terms.md"
    )
    assert scan.find_license(cfg, "packs/Oculus Like Pack/03_weather/a.wav") == (
        "packs/Oculus Like Pack/Please Read.pdf"
    )


def test_license_cache_filled_by_scan(cfg, library):
    scan.scan_library(cfg)
    (library / "packs/Test Bundle 2015/LICENSE.txt").unlink()
    rel = "packs/Test Bundle 2015/Water 01/x.flac"
    assert scan.find_license(cfg, rel) == "packs/Test Bundle 2015/LICENSE.txt"  # cached
    scan.clear_license_cache()
    assert scan.find_license(cfg, rel) is None


# --------------------------------------------------------------------------- RIFF metadata


def _chunk(cid: bytes, body: bytes, pad: bool = True) -> bytes:
    out = cid + struct.pack("<I", len(body)) + body
    return out + (b"\x00" if pad and len(body) % 2 else b"")


def _pcm_wav() -> bytes:
    bio = io.BytesIO()
    sf.write(bio, ag.tone(440, 0.05), ag.SR, format="WAV", subtype="PCM_16")
    return bio.getvalue()


def _with_chunks(wav: bytes, *chunks: bytes) -> bytes:
    body = wav[12:] + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body


def _bext(description: str, version: int = 2) -> bytes:
    b = bytearray(602)
    b[0:len(description)] = description.encode()
    b[256:262] = b"Scott\x00"
    b[320:330] = b"2026-10-14"
    b[330:338] = b"12:00:00"
    struct.pack_into("<IIH", b, 338, 48000, 0, version)
    struct.pack_into("<h", b, 412, -2300)  # loudness value -23.00
    return bytes(b) + b"A=PCM,F=48000\r\n"


IXML = b"""<?xml version="1.0" encoding="UTF-8"?>
<BWFXML><IXML_VERSION>2.10</IXML_VERSION><PROJECT>Endless</PROJECT><NOTE>metal hit</NOTE>
<USER><CATEGORY>METAL</CATEGORY><SUBCATEGORY>IMPACT</SUBCATEGORY>
<KEYWORDS>shelf, clang</KEYWORDS><FXNAME>Shelf Hit</FXNAME></USER></BWFXML>\x00\x00"""


def _info(**fields: str) -> bytes:
    ids = {"title": b"INAM", "artist": b"IART", "comment": b"ICMT"}
    body = b"INFO"
    for k, v in fields.items():
        raw = v.encode() + b"\x00"
        body += _chunk(ids[k], raw)
    return body


def test_read_metadata_bext_ixml_info():
    data = _with_chunks(
        _pcm_wav(),
        _chunk(b"bext", _bext("Metal shelf hit, close")),
        _chunk(b"iXML", IXML),
        _chunk(b"LIST", _info(title="Shelf Hit", artist="Soverance", comment="odd")),
    )
    meta = scan.read_metadata(data, ".wav", "packs/P/GOREMisc_Splat_ME_Lib.wav")
    assert meta["bext"]["description"] == "Metal shelf hit, close"
    assert meta["bext"]["originator"] == "Scott"
    assert meta["bext"]["origination_date"] == "2026-10-14"
    assert meta["bext"]["loudness_value"] == -23.0
    assert meta["bext"]["coding_history"].startswith("A=PCM")
    assert meta["ixml"] == {
        "project": "Endless", "note": "metal hit", "category": "METAL",
        "subcategory": "IMPACT", "keywords": "shelf, clang", "fxname": "Shelf Hit",
    }
    assert meta["info"] == {"title": "Shelf Hit", "artist": "Soverance", "comment": "odd"}
    assert meta["ucs"]["cat_id"] == "GOREMisc"
    assert "warnings" not in meta
    json.dumps(meta)


def test_read_metadata_odd_chunks_with_and_without_padding():
    odd = _chunk(b"junk", b"abc", pad=True) + _chunk(b"iXML", b"<X><NOTE>a</NOTE></X>")
    meta = scan.read_metadata(_with_chunks(_pcm_wav(), odd), ".wav", "a.wav")
    assert meta["ixml"] == {"note": "a"}
    unpadded = _chunk(b"junk", b"abc", pad=False) + _chunk(b"iXML", b"<X><NOTE>b</NOTE></X>")
    meta = scan.read_metadata(_with_chunks(_pcm_wav(), unpadded), ".wav", "a.wav")
    assert meta["ixml"] == {"note": "b"}


def test_read_metadata_rf64():
    wav = _pcm_wav()
    fmt = wav[12:12 + 8 + 16]
    pcm = b"\x01\x00" * 100
    ds64 = _chunk(b"ds64", struct.pack("<QQQI", 0, len(pcm), 100, 0))
    data_chunk = b"data" + struct.pack("<I", 0xFFFFFFFF) + pcm
    body = b"WAVE" + ds64 + fmt + data_chunk + _chunk(b"iXML", b"<X><NOTE>rf</NOTE></X>")
    rf64 = b"RF64" + struct.pack("<I", 0xFFFFFFFF) + body
    meta = scan.read_metadata(rf64, ".wav", "a.wav")
    assert meta["ixml"] == {"note": "rf"}


def test_read_metadata_ixml_with_dtd_is_ignored():
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><X><NOTE>&a;</NOTE></X>'
    meta = scan.read_metadata(_with_chunks(_pcm_wav(), _chunk(b"iXML", evil)), ".wav", "a.wav")
    assert "ixml" not in meta and meta["warnings"]


def test_read_metadata_never_raises_on_garbage():
    rng = random.Random(7)
    wav = _with_chunks(_pcm_wav(), _chunk(b"bext", _bext("x")), _chunk(b"iXML", IXML))
    samples = [b"", b"RIFF", b"RIFF\xff\xff\xff\xffWAVE", b"\x00" * 64, wav[:60], wav[:-30]]
    for _ in range(200):
        buf = bytearray(wav)
        for _ in range(rng.randint(1, 20)):
            buf[rng.randrange(len(buf))] = rng.randrange(256)
        samples.append(bytes(buf[: rng.randint(0, len(buf))]))
    for ext in (".wav", ".mp3", ".flac", ".ogg", ".aif"):
        for data in samples:
            meta = scan.read_metadata(data, ext, "x/y.wav")
            json.dumps(meta)
    bad = scan.read_metadata(b"RIFF\x10\x00\x00\x00WAVEbe\x01\x02", ".wav", "a.wav")
    assert bad["warnings"]


def test_read_metadata_truncated_bext_warns():
    data = _with_chunks(_pcm_wav(), b"bext" + struct.pack("<I", 602) + b"short desc")
    meta = scan.read_metadata(data, ".wav", "a.wav")
    assert meta["bext"]["description"] == "short desc"
    assert any("truncated" in w for w in meta["warnings"])


# --------------------------------------------------------------------------- mutagen tags


def test_read_metadata_flac_ogg_mp3_tags(tmp_path):
    from mutagen.flac import FLAC
    from mutagen.id3 import COMM, ID3, TIT2, TPE1
    from mutagen.oggvorbis import OggVorbis

    flac = ag.write(tmp_path / "a.flac", ag.tone(440, 0.2))
    f = FLAC(flac)
    f["title"] = "Fountain"
    f["comment"] = "water splash"
    f.save()
    meta = scan.read_metadata(flac.read_bytes(), ".flac", "a.flac")
    assert meta["tags"]["title"] == "Fountain" and meta["tags"]["comment"] == "water splash"

    ogg = ag.write(tmp_path / "b.ogg", ag.tone(440, 0.2))
    o = OggVorbis(ogg)
    o["title"] = "Wind"
    o.save()
    assert scan.read_metadata(ogg.read_bytes(), ".ogg", "b.ogg")["tags"]["title"] == "Wind"

    mp3 = ag.write(tmp_path / "c.mp3", ag.tone(440, 0.5))
    tags = ID3()
    tags.add(TIT2(encoding=3, text="Battle"))
    tags.add(TPE1(encoding=3, text="Arteria"))
    tags.add(COMM(encoding=3, lang="eng", desc="", text="loopable"))
    tags.save(mp3)
    got = scan.read_metadata(mp3.read_bytes(), ".mp3", "c.mp3")["tags"]
    assert got["title"] == "Battle" and got["artist"] == "Arteria" and got["comment"] == "loopable"


def test_read_metadata_wav_id3_chunk():
    from mutagen.id3 import ID3, TIT2

    tags = ID3()
    tags.add(TIT2(encoding=3, text="Chunky"))
    bio = io.BytesIO()
    tags.save(bio)
    data = _with_chunks(_pcm_wav(), _chunk(b"id3 ", bio.getvalue()))
    assert scan.read_metadata(data, ".wav", "a.wav")["tags"]["title"] == "Chunky"


def test_read_metadata_on_fixture_library(library):
    for rel in LIBRARY_FILES:
        path = library / rel
        meta = scan.read_metadata(path.read_bytes(), path.suffix.lower(), rel)
        json.dumps(meta)
        assert "warnings" not in meta, (rel, meta)
