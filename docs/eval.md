# Measuring search: `foley eval`

Hybrid search blends keyword matching with CLAP audio-text similarity. Whether that blend helps depends on your library: a library with descriptive file names is already well served by keywords, while one full of `SFX_0412.wav` needs CLAP. `foley eval` measures it on a test set you write, and `foley eval --adopt` makes search use whatever won. CLAP has to earn its place: if no hybrid configuration beats keyword-only, search stays keyword-only.

## 1. Write a test set

The test set is a TOML file outside the repo (it names files in your library). Point `foley.toml` at it:

```toml
[eval]
testset = "C:/Users/me/AppData/Local/Foley/testset.toml"
```

Start from [`eval/testset.example.toml`](../eval/testset.example.toml). Each query is one `[[query]]` table:

```toml
# Header comments (everything before the first [[query]]) are kept on every rewrite.

[[query]]
text = "short bright UI confirm beep"      # required: the query as you would type it
cue = "ui:confirm"                         # optional note, e.g. the game slot; ignored
good = [                                   # 1-3 files you would accept, library-relative
    "packs/UI Pack/confirm_soft_01.wav",
    "packs/Interface Sounds/Positive Select Click 008.wav",
]
filters = { max_duration_s = 1.0 }         # optional: search_audio filter names

[[query]]
text = "sci-fi shotgun blast, wide spread shot"
good = []                                  # still looking: eval skips it
```

Filter names are those of the `search_audio` tool: `category`, `pack`, `is_music`, `min_duration_s`, `max_duration_s`, `min_lufs`, `max_lufs`, `min_attack_s`, `max_attack_s`.

Guidelines:

- **Write about 25 queries** typical of what your game needs, in the words you or Claude would actually use. Describe the sound ("heavy metal impact with a long ringing tail"), not the file.
- **List 1 to 3 good answers** per query: files you would really accept. A query passes if any of them, or an exact copy of one, is in the top results.
- **Find answers by listening and browsing**, not only by searching. If every answer came from keyword search, the test set rewards keyword search and CLAP can never win. `foley browse` has a test-set mode for exactly this: pick a query, browse folders and similar sounds, and press `T` on each file you would accept; see [browse.md](browse.md#test-set-mode).
- Include some queries where file names do not help: music cues, designed sounds named after their pack, sounds you know by ear.
- Keep it as the library grows. A path that is no longer in the catalog is reported as a warning (and still passes if an exact copy is present).

## 2. Run it

```
foley eval                                   # every checkpoint with embeddings, weights 0..1
foley eval --clap htsat-unfused --weights 0,0.3,0.5,0.7 --k 10
foley eval --testset other.toml --json       # another file; machine-readable output
foley eval --adopt                           # also record the winner in the catalog
```

For every answered query and every configuration, eval runs the same ranking as `search_audio` (exact duplicates collapsed) and finds the rank of the first good answer. Configurations are keyword-only (always) and each CLAP checkpoint that has embeddings at blend weights 0.1 to 1. It is cheap: each query is embedded and keyword-searched once per checkpoint, then re-blended for every weight.

## 3. Read the output

This is the run on the small demo library used for the screenshots (306 files, 10 answered queries):

```
foley eval: 10 of 11 queries have answers (1 skipped), k=10, testset ...\testset.toml
checkpoints: larger-general (306 embeddings), htsat-unfused (306 embeddings), htsat-fused (306 embeddings)

  config                 recall@10     MRR     hits
  keyword only               1.000   0.861   10/10
  larger-general w=0.1       1.000   0.850   10/10
  ...
  htsat-fused w=0.1          1.000   0.858   10/10
* htsat-fused w=0.2          1.000   0.867   10/10
  htsat-fused w=0.3          1.000   0.853   10/10
  ...
  htsat-fused w=0.9          0.800   0.510    8/10
  htsat-fused w=1            0.800   0.450    8/10

best hybrid: htsat-fused w=0.2: recall@10 1.000, MRR 0.867; beats keyword-only (1.000, 0.861)
recommendation: clap_model=htsat-fused, blend_weight=0.2 (hybrid htsat-fused w=0.2 beats keyword-only)
misses for htsat-fused w=0.2 (0 of 10):
  none
report: ...\eval\20261016T132529Z.json
```

- **recall@10** is the share of queries with a good answer in the top 10 (`--k` changes 10). It answers "does Claude find a usable file in one search?"
- **MRR** (mean reciprocal rank) rewards putting the good answer high: 1 for rank 1, 0.5 for rank 2, 0.33 for rank 3, and 0 if it is not in the top 100. It separates configurations that all find the answer somewhere in the top 10.
- **hits** is the raw count behind recall.
- The `*` marks the best configuration: highest recall, then highest MRR. Ties go to keyword-only, then to the middle of a plateau of equally good weights.
- **misses** lists the queries the best configuration fails, with the rank of the first good answer (if it is in the top 100) and what came first instead. These are the queries to look at: either search is wrong, or the answers in your test set are incomplete.
- Warnings name good paths that are not active in the catalog.

What to expect: pure CLAP (weight 1) is usually worst on sound-effects libraries, since file names carry a lot of information; a small weight often helps by re-ordering keyword matches by how they actually sound. On a library with descriptive names and a small test set the difference is within noise: when the demo test set grew to 11 queries, keyword-only edged ahead (MRR 0.874 against 0.864) and `--adopt` switched search to keyword-only. Write more queries before trusting a small margin.

## 4. Adopt the result

`foley eval --adopt` records the best checkpoint and weight in the catalog's `meta` table (`search_clap_model`, `search_blend_weight`, plus a summary in `search_eval`). Search, the MCP server and `foley browse` pick them up without a restart. If no hybrid configuration beats keyword-only, the adopted weight is 0: keyword-only search, with the best checkpoint still recorded.

A value set in `[search]` in `foley.toml` overrides the adopted one, and eval tells you so. Leave `[search]` unset to follow the evaluation.

Every run, adopted or not, saves the full JSON report (per-query ranks and top results for every configuration) under `<data_dir>/eval/<UTC timestamp>.json`.
