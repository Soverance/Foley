"""Check Foley's model preprocessing against each model's reference example code.

    uv run python eval/verify_models.py [--config foley.toml] [--file PATH ...]
                                        [--query "dog bark" ...] [--device auto|cpu|cuda]

For a few library files (by default: the best keyword hits for "dog bark", "gunshot",
"rain" and "music", one clip of at most 10 s and one longer clip each), it runs

  PANNs CNN14  reference: audioset_tagging_cnn pytorch/inference.py (audio_tagging):
               librosa.load(path, sr=32000, mono=True) -> model(waveform[None]) ->
               clipwise_output, on the whole file.
               Foley:     decode -> foley.models.prep.panns_windows -> PannsTagger.tag.
  CLAP         reference: the Hugging Face ClapModel example: librosa.load(sr=48000) ->
               ClapProcessor(text=labels, audio=..., padding=True) -> ClapModel ->
               logits_per_audio.softmax (zero-shot), audio_embeds.
               Foley:     decode -> prep.clap_windows -> ClapAudioEmbedder.embed, with
                          ClapTextEncoder for the labels.

and reports, per file: the reference and Foley top AudioSet labels and their max score
difference; for each CLAP checkpoint the cosine between the reference and Foley audio
embeddings and both zero-shot winners. Files of at most 10 s should agree to float
precision (PANNs: except clips under 0.31 s, which the reference cannot take). Longer
files differ by design (Foley's windows
vs the reference's whole file / random 10 s crop) but should agree on the content.

Reads library files read-only. Writes a JSON report to <data_dir>/eval/.
Downloads the checkpoints on first use (PANNs from Zenodo, CLAP from Hugging Face).
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from foley import config, decode, models
from foley.catalog import db
from foley.models import clap, panns, prep

LABELS = [
    "a dog barking", "a gunshot", "rain falling", "music", "footsteps on a hard floor",
    "a door closing", "wind blowing", "an explosion", "a car engine", "birds chirping",
    "a person speaking", "glass breaking",
]
QUERIES = {
    "dog bark": ("a dog barking", {"Dog", "Bark", "Bow-wow", "Animal", "Domestic animals, pets",
                                    "Yip", "Growling", "Howl"}),
    "gunshot": ("a gunshot", {"Gunshot, gunfire", "Explosion", "Machine gun", "Cap gun",
                              "Artillery fire", "Fusillade", "Burst, pop"}),
    "rain": ("rain falling", {"Rain", "Rain on surface", "Raindrop", "Water", "Drip"}),
    "music": ("music", {"Music"}),
}


def pick_files(cfg, queries: list[str]) -> list[tuple[str, str]]:
    conn = db.open_catalog(cfg)
    try:
        out = []
        for q in queries:
            terms = " ".join(f'"{w}"' for w in q.split())
            rows = conn.execute(
                "SELECT a.rel_path, f.duration_s FROM search_fts s "
                "JOIN assets a ON a.id = s.rowid JOIN features f ON f.asset_id = a.id "
                "WHERE search_fts MATCH ? AND a.status = 'active' "
                "AND a.ext IN ('.wav', '.flac') ORDER BY bm25(search_fts) LIMIT 200",
                (terms,),
            ).fetchall()
            short = next((r[0] for r in rows if r[1] and r[1] <= 10), None)
            longer = next((r[0] for r in rows if r[1] and 12 < r[1] <= 300), None)
            out += [(q, rel) for rel in (short, longer) if rel]
        return out
    finally:
        conn.close()


def top(scores, labels, k=5):
    order = np.argsort(-scores)[:k]
    return [(labels[i], round(float(scores[i]), 3)) for i in order]


def verify_panns(cfg, files, device) -> list[dict]:
    import librosa
    import torch

    tagger = panns.PannsTagger(models.cache_dir(cfg) / "panns", device, say=print)
    # The checkpoint's front-end kernels are the reference's (torchlibrosa); Foley's
    # freshly built ones must match them, which also checks the vendored definition.
    fresh = panns.Cnn14().state_dict()
    loaded = tagger.model.state_dict()
    frontend = {
        k: float((fresh[k] - loaded[k].cpu()).abs().max())
        for k in ("spectrogram_extractor.stft.conv_real.weight",
                  "spectrogram_extractor.stft.conv_imag.weight", "logmel_extractor.melW")
    }
    print(f"PANNs front end, max |built - checkpoint|: {frontend}")
    out = []
    for query, path in files:
        y, _ = librosa.load(path, sr=panns.SAMPLE_RATE, mono=True)
        with torch.inference_mode():
            x = torch.from_numpy(y[None].astype(np.float32)).to(device)
            ref = tagger.model(x)["clipwise_output"][0].float().cpu().numpy()
        audio = decode.decode_path(Path(path))
        ours = tagger.tag([prep.panns_windows(audio)])[0]
        expected = QUERIES.get(query, (None, set()))[1]
        ours_top = top(ours, tagger.labels)
        out.append({
            "query": query, "file": path, "duration_s": round(audio.duration_s, 2),
            "max_abs_diff": round(float(np.max(np.abs(ref - ours))), 5),
            "reference_top": top(ref, tagger.labels), "foley_top": ours_top,
            "expected_in_top5": bool(expected & {lab for lab, _ in ours_top}) if expected
            else None,
            "is_music": panns.is_music(panns.scores_to_tags(ours, tagger.labels)),
        })
        r = out[-1]
        print(f"  [{query}] {Path(path).name} ({r['duration_s']} s): diff {r['max_abs_diff']}"
              f"  ref {r['reference_top'][:3]}  foley {r['foley_top'][:3]}")
    del tagger
    return [{"frontend_max_abs_diff": frontend}] + out


def verify_clap(name, files, device) -> list[dict]:
    import librosa
    import torch
    import transformers as tf

    repo, rev = clap.CLAP_CHECKPOINTS[name], clap.REVISIONS[name]
    ref_model = tf.ClapModel.from_pretrained(repo, revision=rev).to(device).eval()
    processor = tf.ClapProcessor.from_pretrained(repo, revision=rev)
    emb = clap.ClapAudioEmbedder(name, device)
    txt = clap.ClapTextEncoder(name, device)
    text_vecs = txt.embed_text(LABELS)
    scale = float(ref_model.logit_scale_a.exp())
    out = []
    for query, path in files:
        y, _ = librosa.load(path, sr=clap.SAMPLE_RATE, mono=True)
        np.random.seed(0)  # the reference crops long clips at random
        # The processor's two halves, called as in the ClapModel docstring examples (a
        # combined call would route padding=True to the feature extractor as well).
        inputs = dict(processor.tokenizer(LABELS, padding=True, return_tensors="pt"))
        inputs.update(processor.feature_extractor(y, sampling_rate=clap.SAMPLE_RATE,
                                                  return_tensors="pt"))
        with torch.inference_mode():
            res = ref_model(**{k: v.to(device) for k, v in inputs.items()})
        ref_probs = res.logits_per_audio.softmax(-1)[0].float().cpu().numpy()
        ref_audio = res.audio_embeds[0].float().cpu().numpy()
        ref_text = res.text_embeds.float().cpu().numpy()
        audio = decode.decode_path(Path(path))
        ours = emb.embed([prep.clap_windows(audio)])[0]
        logits = scale * (text_vecs @ ours)
        probs = np.exp(logits - logits.max())
        probs /= probs.sum()
        expected = QUERIES.get(query, (None, set()))[0]
        out.append({
            "query": query, "file": path, "duration_s": round(audio.duration_s, 2),
            "audio_cosine_ref_vs_foley": round(float(ref_audio @ ours), 5),
            "text_max_abs_diff": round(float(np.max(np.abs(ref_text - text_vecs))), 6),
            "reference_zero_shot": (LABELS[int(ref_probs.argmax())],
                                    round(float(ref_probs.max()), 3)),
            "foley_zero_shot": (LABELS[int(probs.argmax())], round(float(probs.max()), 3)),
            "foley_correct": LABELS[int(probs.argmax())] == expected if expected else None,
        })
        r = out[-1]
        print(f"  [{name}] [{query}] {Path(path).name} ({r['duration_s']} s): cos "
              f"{r['audio_cosine_ref_vs_foley']}, text diff {r['text_max_abs_diff']}, ref "
              f"{r['reference_zero_shot']}, foley {r['foley_zero_shot']}")
    del ref_model, emb, txt
    gc.collect()
    if device.startswith("cuda"):
        torch.cuda.empty_cache()
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--config")
    p.add_argument("--file", action="append", default=[], help="library file (absolute)")
    p.add_argument("--query", action="append", help="pick files by keyword search")
    p.add_argument("--device", default=None)
    p.add_argument("--clap", action="append", help="CLAP checkpoint(s) (default: all)")
    args = p.parse_args(argv)
    cfg = config.load(args.config)
    device = models.resolve_device(args.device or cfg.models_device)
    files = [("file", f) for f in args.file]
    if not files:
        files = [(q, str(cfg.abs_path(rel)))
                 for q, rel in pick_files(cfg, args.query or list(QUERIES))]
    if not files:
        print("no files to check", file=sys.stderr)
        return 1
    print(f"device {device}; {len(files)} files")
    t0 = time.monotonic()
    report = {"at": datetime.now().isoformat(timespec="seconds"), "device": device,
              "panns": {"model": panns.MODEL_KEY, "results": verify_panns(cfg, files, device)},
              "clap": {}}
    for name in args.clap or list(clap.CLAP_CHECKPOINTS):
        report["clap"][clap.model_key(name)] = verify_clap(name, files, device)
    report["seconds"] = round(time.monotonic() - t0, 1)
    out_dir = Path(cfg.data_dir) / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"verify-models-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
