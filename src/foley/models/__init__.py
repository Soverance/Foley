"""Model plumbing shared by the tagging (PANNs) and embedding (CLAP) stages.

CONTRACT:

    This package and foley.models.prep import neither torch nor transformers, so the
    analysis workers, the CLI and the servers can import them cheaply. The model
    modules (foley.models.panns, foley.models.clap) import torch only inside the
    functions that load or run a model.

    class ModelUnavailable(RuntimeError)
        A model cannot be loaded (no torch/CUDA problem, download failed, weights
        corrupt, loading disabled in tests). Callers treat the model stages as skipped.

    resolve_device(device: str = "auto") -> str
        "auto" -> "cuda" when torch sees a CUDA device, else "cpu". "cuda" without a
        device raises ModelUnavailable. Anything else is passed to torch as given.

    class ModelFactory
        tagger(cfg, device) -> panns.PannsTagger-like
            .key: str (tags.model), .labels: list[str],
            .tag(clips: list[list[np.ndarray]]) -> np.ndarray (n_clips, n_labels) float32
              one entry per file: its 32 kHz windows (foley.models.prep.panns_windows);
              the per-file score is the mean of the per-window clipwise probabilities.
        clap_audio(short_name, device) -> clap.ClapAudioEmbedder-like
            .key: str (embeddings.model), .dim: int,
            .embed(clips: list[list[np.ndarray]]) -> np.ndarray (n_clips, dim) float32
              one entry per file: its 48 kHz windows (prep.clap_windows); each row is the
              mean of the L2-normalized window embeddings, L2-normalized again.
        clap_text(short_name, device) -> clap.ClapTextEncoder-like (see foley.models.clap)

    factory() -> ModelFactory          the one in use (the real one unless replaced)
    set_factory(f: ModelFactory | None) -> None
        Replace the factory (tests inject fakes; None restores the real one). Also clears
        cached text encoders (foley.models.clap.text_encoder).

    cache_dir(cfg) -> Path            data_dir/models: where the PANNs checkpoint and the
                                      AudioSet label list are downloaded. CLAP checkpoints
                                      use Hugging Face's own cache (HF_HOME).
"""

from __future__ import annotations

from pathlib import Path


class ModelUnavailable(RuntimeError):
    pass


def resolve_device(device: str = "auto") -> str:
    device = (device or "auto").strip().lower()
    if device not in ("auto", "cuda"):
        return device
    try:
        import torch
    except Exception as e:  # noqa: BLE001 - a broken torch install is "unavailable"
        raise ModelUnavailable(f"torch is not importable: {type(e).__name__}: {e}") from e
    if torch.cuda.is_available():
        return "cuda"
    if device == "cuda":
        raise ModelUnavailable("[models] device = 'cuda' but torch sees no CUDA device")
    return "cpu"


def cache_dir(cfg) -> Path:
    return Path(cfg.data_dir) / "models"


class ModelFactory:
    """Loads the real models (downloads on first use)."""

    def tagger(self, cfg, device: str):
        from foley.models import panns

        return panns.PannsTagger(cache_dir(cfg) / "panns", device)

    def clap_audio(self, short_name: str, device: str):
        from foley.models import clap

        return clap.ClapAudioEmbedder(short_name, device)

    def clap_text(self, short_name: str, device: str):
        from foley.models import clap

        return clap.ClapTextEncoder(short_name, device)


_REAL = ModelFactory()
_FACTORY: ModelFactory = _REAL


def factory() -> ModelFactory:
    return _FACTORY


def set_factory(f: ModelFactory | None) -> None:
    global _FACTORY
    _FACTORY = f if f is not None else _REAL
    from foley.models import clap

    clap.clear_text_encoders()
