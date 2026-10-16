"""CLAP audio and text embeddings via Hugging Face transformers (pipeline stage 7).

CONTRACT (the search agent's code depends on these names):

    CLAP_CHECKPOINTS: dict[str, str]   short name -> Hugging Face repo id
        "larger-general": "laion/larger_clap_general"     HTSAT-base, unfused; trained
                                                          on general audio, music, speech
        "htsat-unfused":  "laion/clap-htsat-unfused"      HTSAT-tiny, unfused (LAION-630k)
        "htsat-fused":    "laion/clap-htsat-fused"        HTSAT-tiny, feature fusion
        All three are Apache-2.0 (LAION, Wu et al., "Large-scale Contrastive
        Language-Audio Pretraining with Feature Fusion and Keyword-to-Caption
        Augmentation", ICASSP 2023). Each is pinned to a commit in REVISIONS.
    REVISIONS: dict[str, str]          short name -> pinned HF commit
    DEFAULT_CLAP = "larger-general"    used when config does not say

    model_key(short_name) -> str
        The exact embeddings.model string: "clap:<repo id>@<first 12 of the commit>",
        e.g. "clap:laion/larger_clap_general@ada0c23a36c4". Stable; a new revision or
        preprocessing changes it, so the stage re-runs. KeyError for unknown names.
    short_name_for_key(key) -> str | None

    class ClapTextEncoder(short_name, device="cpu")
        Text tower only (ClapTextModelWithProjection + the repo's tokenizer), eval mode.
        key == model_key(short_name), dim (512)
        embed_text(texts: list[str]) -> np.ndarray (n, dim) float32, L2-normalized
            Tokenized like LAION's training code: truncation to 77 tokens.
    text_encoder(short_name=None) -> ClapTextEncoder
        Process-wide cached instance per short name, loaded lazily and thread-safely on
        the CPU (servers must not hold VRAM). None -> the first [models] clap entry of
        the config passed to configure(), else DEFAULT_CLAP. Built through
        foley.models.factory(), so tests can inject fakes.
    configure(clap_names: list[str] | None) -> None    the default for text_encoder(None)

    extractor_id(extractor) -> str; extractor_config(extractor) -> dict;
    make_extractor(config) -> ClapFeatureExtractor; extract(extractor, windows) -> dict
        Feature extraction away from the model (the indexer runs it in its workers).
    clear_text_encoders() -> None

    class ClapAudioEmbedder(short_name, device)   (used by the indexer)
        Audio tower only (ClapAudioModelWithProjection) + the repo's ClapFeatureExtractor.
        key, dim, fusion, extractor, config_id (= extractor_id(extractor))
        features(windows) -> dict          extract(self.extractor, windows)
        embed_features(feats: list[dict]) -> np.ndarray (n_clips, dim)   from extract()
            outputs, one per clip (what the analysis workers send)
        embed(clips: list[list[np.ndarray]]) -> np.ndarray (n_clips, dim) float32
            Each clip is a file's 48 kHz windows (foley.models.prep.clap_windows: at most
            12 evenly spaced windows of at most 10 s). All windows of all clips are
            batched together; each window embedding is L2-normalized, a file's vector is
            the mean of its windows, L2-normalized again.

Loading downloads the checkpoint into Hugging Face's cache (HF_HOME) on first use and
raises foley.models.ModelUnavailable if that or the load fails.

PREPROCESSING (as in the Hugging Face ClapModel/ClapProcessor examples): mono float
audio at 48 kHz passed to the checkpoint's own feature extractor with its own settings
(64-bin log-mel, 10 s max_length, "repeatpad" for shorter clips). Because Foley never
passes more than 10 s, the extractor's random cropping never runs. For fused checkpoints
the extractor (like LAION's own HTSAT code) marks one random clip of a batch as "longer"
when none is, so a single clip, as in the reference example, always takes the fusion
branch. Foley sets the flag on every window of a fused checkpoint, so each window gets
exactly what the reference gives that window on its own, independent of the batch.
"""

from __future__ import annotations

import threading

import numpy as np

from foley.models import ModelUnavailable

CLAP_CHECKPOINTS: dict[str, str] = {
    "larger-general": "laion/larger_clap_general",
    "htsat-unfused": "laion/clap-htsat-unfused",
    "htsat-fused": "laion/clap-htsat-fused",
}
REVISIONS: dict[str, str] = {
    "larger-general": "ada0c23a36c4e8582805bb38fec3905903f18b41",
    "htsat-unfused": "8fa0f1c6d0433df6e97c127f64b2a1d6c0dcda8a",
    "htsat-fused": "365dea6ef167def6676140ed93bbc43f84dabb28",
}
LICENSES: dict[str, str] = {name: "Apache-2.0" for name in CLAP_CHECKPOINTS}
DEFAULT_CLAP = "larger-general"
SAMPLE_RATE = 48000
TEXT_MAX_TOKENS = 77
BATCH_WINDOWS = 32


def model_key(short_name: str) -> str:
    repo = CLAP_CHECKPOINTS[short_name]
    return f"clap:{repo}@{REVISIONS[short_name][:12]}"


def short_name_for_key(key: str) -> str | None:
    return next((n for n in CLAP_CHECKPOINTS if model_key(n) == key), None)


def _check(short_name: str) -> str:
    if short_name not in CLAP_CHECKPOINTS:
        raise KeyError(
            f"unknown CLAP checkpoint {short_name!r}; known: {', '.join(CLAP_CHECKPOINTS)}"
        )
    return short_name


def _imports():
    try:
        import torch
        import transformers
    except Exception as e:  # noqa: BLE001
        raise ModelUnavailable(
            f"torch/transformers are not importable: {type(e).__name__}: {e}"
        ) from e
    return torch, transformers


def _load(fn, short_name: str):
    repo = CLAP_CHECKPOINTS[short_name]
    _, tf = _imports()
    log = tf.utils.logging
    verbosity = log.get_verbosity()
    # A tower-only class loads part of a checkpoint by design: keep its load report (every
    # skipped key of the other tower) and progress bars out of the indexer's log.
    log.set_verbosity_error()
    log.disable_progress_bar()
    try:
        return fn(repo, revision=REVISIONS[short_name])
    except Exception as e:  # noqa: BLE001 - network, cache, format: all "unavailable"
        raise ModelUnavailable(
            f"cannot load {repo}@{REVISIONS[short_name][:12]}: {type(e).__name__}: {e}"
        ) from e
    finally:
        log.set_verbosity(verbosity)
        log.enable_progress_bar()


def _normalize(x: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(x, axis=-1, keepdims=True)
    return (x / np.maximum(norm, 1e-12)).astype(np.float32)


class ClapTextEncoder:
    def __init__(self, short_name: str, device: str = "cpu"):
        _check(short_name)
        torch, tf = _imports()
        self.short_name = short_name
        self.key = model_key(short_name)
        self.device = device
        self.tokenizer = _load(tf.AutoTokenizer.from_pretrained, short_name)
        model = _load(tf.ClapTextModelWithProjection.from_pretrained, short_name)
        self.model = model.to(device).eval()
        self.dim = int(self.model.config.projection_dim)
        self._lock = threading.Lock()

    def embed_text(self, texts: list[str]) -> np.ndarray:
        torch, _ = _imports()
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        inputs = self.tokenizer(
            list(texts), padding=True, truncation=True, max_length=TEXT_MAX_TOKENS,
            return_tensors="pt",
        )
        with self._lock, torch.inference_mode():
            out = self.model(**{k: v.to(self.device) for k, v in inputs.items()})
            emb = out.text_embeds.float().cpu().numpy()
        return _normalize(emb)


class ClapAudioEmbedder:
    def __init__(self, short_name: str, device: str = "cpu"):
        _check(short_name)
        torch, tf = _imports()
        self.short_name = short_name
        self.key = model_key(short_name)
        self.device = device
        self.extractor = _load(tf.ClapFeatureExtractor.from_pretrained, short_name)
        model = _load(tf.ClapAudioModelWithProjection.from_pretrained, short_name)
        self.model = model.to(device).eval()
        self.dim = int(self.model.config.projection_dim)
        self.fusion = bool(getattr(self.model.config, "enable_fusion", False))

        self.config_id = extractor_id(self.extractor)

    def features(self, windows: list[np.ndarray]) -> dict:
        """The extractor's model inputs for windows of <= 10 s (see extract())."""
        return extract(self.extractor, windows)

    def embed_features(self, feats: list[dict]) -> np.ndarray:
        """One vector per clip from extract() outputs (one dict per clip): window
        embeddings in batches, each L2-normalized, mean-pooled, L2-normalized."""
        torch, _ = _imports()
        counts = [len(f["input_features"]) for f in feats]
        if not sum(counts):
            return np.zeros((len(feats), self.dim), dtype=np.float32)
        x = np.concatenate([f["input_features"] for f in feats])
        per_window = np.zeros((len(x), self.dim), dtype=np.float32)
        with torch.inference_mode():
            for start in range(0, len(x), BATCH_WINDOWS):
                chunk = torch.from_numpy(np.ascontiguousarray(x[start : start + BATCH_WINDOWS]))
                inputs = {"input_features": chunk.to(self.device)}
                if self.fusion:  # every window of a fused checkpoint: see PREPROCESSING
                    inputs["is_longer"] = torch.ones((len(chunk), 1), dtype=torch.bool,
                                                     device=self.device)
                res = self.model(**inputs)
                per_window[start : start + len(chunk)] = res.audio_embeds.float().cpu().numpy()
        per_window = _normalize(per_window)
        out = np.zeros((len(feats), self.dim), dtype=np.float32)
        i = 0
        for ci, n in enumerate(counts):
            if n:
                out[ci] = per_window[i : i + n].mean(axis=0)
            i += n
        return _normalize(out)

    def embed(self, clips: list[list[np.ndarray]]) -> np.ndarray:
        return self.embed_features([self.features(ws) for ws in clips])


# --------------------------------------------------------------------------- features
#
# Feature extraction (numpy log-mel) is most of CLAP's cost on the CPU, so the indexer
# runs it in its analysis workers: it sends each worker the extractors' configs, and
# checkpoints with identical configs share one extraction.


def extractor_id(extractor) -> str:
    """Identifies an extractor configuration (equal ids give identical features)."""
    import hashlib
    import json

    cfg = json.dumps(extractor.to_dict(), sort_keys=True, default=str)
    return hashlib.sha1(cfg.encode()).hexdigest()[:12]


def extractor_config(extractor) -> dict:
    return extractor.to_dict()


def make_extractor(config: dict):
    _, tf = _imports()
    return tf.ClapFeatureExtractor.from_dict(dict(config))


def extract(extractor, windows: list[np.ndarray]) -> dict:
    """{"input_features": np.ndarray (n, C, frames, mels)} for windows of <= 10 s, exactly
    as the checkpoint's extractor computes them (its own truncation/padding settings)."""
    if not windows:
        return {"input_features": np.zeros((0, 1, 1, 1), dtype=np.float32)}
    inputs = extractor(
        [np.asarray(w, dtype=np.float32) for w in windows], sampling_rate=SAMPLE_RATE,
        return_tensors="np",
    )
    return {"input_features": np.asarray(inputs["input_features"])}


# --------------------------------------------------------------------------- text cache

_TEXT: dict[str, object] = {}
_TEXT_LOCK = threading.Lock()
_CONFIGURED: list[str] = []


def configure(clap_names: list[str] | None) -> None:
    _CONFIGURED[:] = [_check(n) for n in (clap_names or [])]


def clear_text_encoders() -> None:
    with _TEXT_LOCK:
        _TEXT.clear()


def text_encoder(short_name: str | None = None):
    name = _check(short_name or (_CONFIGURED[0] if _CONFIGURED else DEFAULT_CLAP))
    enc = _TEXT.get(name)
    if enc is not None:
        return enc
    with _TEXT_LOCK:
        enc = _TEXT.get(name)
        if enc is None:
            from foley import models

            enc = models.factory().clap_text(name, "cpu")
            _TEXT[name] = enc
        return enc
