from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from foley import testset
from foley.search import SearchFilters

EXAMPLE = Path(__file__).resolve().parents[1] / "eval" / "testset.example.toml"

SAMPLE = '''# Private test set.
# `cue` is informational.

[[query]]
text = "sci-fi shotgun blast, wide spread shot"
cue = "sfx:fire_scatter"
good = []

# a comment that is regenerated away
[[query]]
text = "bright crystalline pickup chime"
cue = "sfx:orb_collected"
good = ["packs/A/chime 01.wav", "packs/A/chime\\\\odd \\"quoted\\".wav"]
filters = { max_duration_s = 2.0, is_music = false }
rating = 4
'''


def test_parse_sample():
    ts = testset.parse(SAMPLE)
    assert ts.header == "# Private test set.\n# `cue` is informational.\n\n"
    q0, q1 = ts.queries
    assert (q0.text, q0.cue, q0.good, q0.filters) == (
        "sci-fi shotgun blast, wide spread shot", "sfx:fire_scatter", [], {})
    # backslashes in paths become forward slashes (library-relative paths)
    assert q1.good == ["packs/A/chime 01.wav", 'packs/A/chime/odd "quoted".wav']
    assert q1.filters == {"max_duration_s": 2.0, "is_music": False}
    assert q1.extra == {"rating": 4}


def test_round_trip_preserves_header_and_values(tmp_path):
    ts = testset.parse(SAMPLE)
    ts.queries.append(testset.Query("rüstung klirrt — ノイズ\ttab", ["sfx/ü/Ä.wav"], cue="ui:ö",
                                    filters={"min_lufs": -30, "category": "UI"}))
    path = testset.save(ts, tmp_path / "set.toml")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Private test set.\n# `cue` is informational.\n\n[[query]]\n")
    assert "rüstung klirrt — ノイズ\\ttab" in text  # unicode kept, control chars escaped
    assert 'filters = { max_duration_s = 2.0, is_music = false }' in text
    assert 'good = [\n    "packs/A/chime 01.wav",\n' in text and "good = []" in text
    back = testset.load(path)
    assert back.header == ts.header
    assert [(q.text, q.cue, q.good, q.filters, q.extra) for q in back.queries] == [
        (q.text, q.cue, q.good, testset.check_filters(q.filters), q.extra) for q in ts.queries]
    assert testset.dumps(back) == text  # stable
    assert list(tmp_path.iterdir()) == [path]  # no temp file left behind
    raw = tomllib.loads(text)
    assert raw["query"][2]["filters"] == {"min_lufs": -30.0, "category": "UI"}


def test_example_file_parses_and_round_trips(tmp_path):
    ts = testset.load(EXAMPLE)
    assert ts.queries and all(q.text for q in ts.queries)
    assert any(q.cue for q in ts.queries)
    again = testset.parse(testset.dumps(ts))
    assert [q.good for q in again.queries] == [q.good for q in ts.queries]


def test_missing_file_is_empty_with_default_header(tmp_path):
    ts = testset.load(tmp_path / "none.toml")
    assert ts.queries == [] and ts.header == testset.DEFAULT_HEADER
    ts.queries.append(testset.Query("a"))
    path = testset.save(ts)
    assert path == tmp_path / "none.toml" and testset.load(path).queries[0].text == "a"


@pytest.mark.parametrize("bad, msg", [
    ('[[query]]\ngood = []\n', "text"),
    ('[[query]]\ntext = "x"\ngood = "a.wav"\n', "good"),
    ('[[query]]\ntext = "x"\nfilters = { loudness = 3 }\n', "unknown filter"),
    ('[[query]]\ntext = "x"\nfilters = { max_duration_s = "long" }\n', "number"),
    ('[[query]]\ntext = "x"\nfilters = { is_music = 1 }\n', "bool"),
    ('[[query]\n', "not valid TOML"),
])
def test_bad_files(bad, msg):
    with pytest.raises(testset.TestsetError, match=msg):
        testset.parse(bad)


def test_refuses_to_drop_foreign_tables():
    ts = testset.parse('[[query]]\ntext = "x"\n\n[notes]\nwho = "someone"\n')
    with pytest.raises(testset.TestsetError, match="notes"):
        testset.dumps(ts)
    ok = testset.parse('# h\nversion = 2\n\n[[query]]\ntext = "x"\n')
    assert testset.dumps(ok).startswith("# h\nversion = 2\n\n[[query]]")


def test_search_filters():
    assert testset.search_filters({"max_duration_s": 1, "pack": "Oculus"}) == SearchFilters(
        max_duration_s=1.0, pack="Oculus")
