"""AudioSet tags from PANNs CNN14 (pipeline stage 6, "tags").

CONTRACT:

    MODEL_KEY = "panns:cnn14@zenodo3987831-541141fa"   stored in tags.model; names the
        checkpoint (Zenodo record 3987831, Cnn14_mAP=0.431.pth, md5 prefix). A new
        checkpoint or preprocessing gets a new key, which re-runs the stage.
    TAG_MIN_SCORE = 0.1      every AudioSet label scoring at least this is stored
    MUSIC_LABEL = "Music", MUSIC_THRESHOLD = 0.5
        features.is_music = 1 when the "Music" score is >= MUSIC_THRESHOLD, else 0.

    class PannsTagger(cache: Path, device: str = "cpu")
        Downloads the checkpoint and the AudioSet label list into `cache` on first use
        (urllib; the checkpoint's md5 is verified; partial downloads never look complete)
        and loads CNN14 in eval mode on `device`. Raises models.ModelUnavailable when that
        fails.
        key, labels (527 AudioSet display names, in model output order)
        tag(clips: list[list[np.ndarray]]) -> np.ndarray (n_clips, 527) float32
            Each clip is a file's 32 kHz windows (foley.models.prep.panns_windows). Windows
            of equal length are batched; a file's scores are the mean of its windows'
            clipwise (sigmoid) outputs.

    scores_to_tags(scores: np.ndarray, labels) -> list[tuple[str, float]]
        Labels >= TAG_MIN_SCORE, best first.
    is_music(tags: list[tuple[str, float]]) -> int

    class Cnn14(torch.nn.Module)   the published architecture (see below), for tests and
                                   the verification script.

PREPROCESSING (as in the reference inference code, audioset_tagging_cnn
pytorch/inference.py and panns_inference): mono, 32 kHz, float waveform in [-1, 1],
log-mel front end inside the model (n_fft 1024, hop 320, 64 mel bins, 50 Hz to 14 kHz,
power_to_db with ref 1.0, amin 1e-10, no top_db). The reference feeds a whole clip at
once; Foley feeds windows of at most 10 s (the AudioSet clip length), at most 12 of them
(prep.window_starts), so memory stays bounded on long files, and zero-pads only clips
shorter than CNN14 can take at all (0.31 s, where the reference raises). Files of at most
10 s therefore get exactly the reference output (verified: max |diff| 1e-5).

The architecture is vendored from https://github.com/qiuqiangkong/audioset_tagging_cnn
(pytorch/models.py, Cnn14), Copyright (c) 2020 Qiuqiang Kong, MIT License. The log-mel
front end reproduces torchlibrosa's Spectrogram/LogmelFilterBank (MIT) with the
convolution kernels and mel matrix stored in the checkpoint, so it is the reference front
end exactly. The pretrained weights (Kong et al., "PANNs: Large-Scale Pretrained Audio
Neural Networks for Audio Pattern Recognition", 2020; https://zenodo.org/records/3987831)
are licensed CC BY 4.0. The AudioSet ontology label list (Google, CC BY 4.0) is
downloaded from the AudioSet site.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import urllib.request
from collections import defaultdict
from pathlib import Path

import numpy as np

from foley.models import ModelUnavailable

ZENODO_RECORD = "3987831"
CHECKPOINT_NAME = "Cnn14_mAP=0.431.pth"
CHECKPOINT_URL = (
    "https://zenodo.org/records/3987831/files/Cnn14_mAP%3D0.431.pth?download=1"
)
CHECKPOINT_MD5 = "541141fa2ee191a88f24a3219fff024e"
CHECKPOINT_BYTES = 327_428_481
LABELS_URL = (
    "https://storage.googleapis.com/us_audioset/youtube_corpus/v1/csv/class_labels_indices.csv"
)
LABELS_NAME = "class_labels_indices.csv"
N_LABELS = 527

MODEL_KEY = f"panns:cnn14@zenodo{ZENODO_RECORD}-{CHECKPOINT_MD5[:8]}"
TAG_MIN_SCORE = 0.1
MUSIC_LABEL = "Music"
MUSIC_THRESHOLD = 0.5

SAMPLE_RATE = 32000
WINDOW_SIZE = 1024
HOP_SIZE = 320
MEL_BINS = 64
FMIN = 50
FMAX = 14000
BATCH_WINDOWS = 16
_DOWNLOAD_TIMEOUT = 60


def scores_to_tags(scores, labels) -> list[tuple[str, float]]:
    order = np.argsort(-np.asarray(scores))
    return [
        (labels[i], float(scores[i])) for i in order if float(scores[i]) >= TAG_MIN_SCORE
    ]


def is_music(tags) -> int:
    score = next((s for label, s in tags if label == MUSIC_LABEL), 0.0)
    return int(score >= MUSIC_THRESHOLD)


# --------------------------------------------------------------------------- downloads


def _download(url: str, dest: Path, md5: str | None = None, say=None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    if say:
        say(f"downloading {url} -> {dest}")
    h = hashlib.md5()
    try:
        with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as r, open(part, "wb") as f:
            while block := r.read(1 << 20):
                h.update(block)
                f.write(block)
    except OSError as e:
        part.unlink(missing_ok=True)
        raise ModelUnavailable(f"download of {url} failed: {type(e).__name__}: {e}") from e
    if md5 and h.hexdigest() != md5:
        part.unlink(missing_ok=True)
        raise ModelUnavailable(f"{url}: md5 {h.hexdigest()} != expected {md5}")
    os.replace(part, dest)


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while block := f.read(1 << 22):
            h.update(block)
    return h.hexdigest()


def ensure_checkpoint(cache: Path, say=None) -> Path:
    """The CNN14 checkpoint in `cache`, downloaded and md5-verified if needed."""
    path = cache / CHECKPOINT_NAME
    ok = cache / (CHECKPOINT_NAME + ".md5ok")  # verified once; size re-checked each load
    if path.is_file() and path.stat().st_size == CHECKPOINT_BYTES and ok.is_file():
        return path
    if path.is_file() and _md5(path) == CHECKPOINT_MD5:
        ok.write_text(CHECKPOINT_MD5)
        return path
    _download(CHECKPOINT_URL, path, CHECKPOINT_MD5, say)
    ok.write_text(CHECKPOINT_MD5)
    return path


def parse_labels(text: str) -> list[str]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or rows[0][:3] != ["index", "mid", "display_name"]:
        raise ModelUnavailable("AudioSet label list has an unexpected header")
    labels = [r[2] for r in rows[1:] if len(r) >= 3]
    if len(labels) != N_LABELS or [int(r[0]) for r in rows[1:]] != list(range(N_LABELS)):
        raise ModelUnavailable(f"AudioSet label list has {len(labels)} rows, not {N_LABELS}")
    return labels


def ensure_labels(cache: Path, say=None) -> list[str]:
    path = cache / LABELS_NAME
    if not path.is_file():
        _download(LABELS_URL, path, None, say)
    try:
        return parse_labels(path.read_text(encoding="utf-8"))
    except ModelUnavailable:
        path.unlink(missing_ok=True)  # let the next load fetch it again
        raise


# --------------------------------------------------------------------------- model


def _torch():
    try:
        import torch
        from torch import nn
        from torch.nn import functional as F  # noqa: N812
    except Exception as e:  # noqa: BLE001
        raise ModelUnavailable(f"torch is not importable: {type(e).__name__}: {e}") from e
    return torch, nn, F


def _build_classes():
    torch, nn, F = _torch()  # noqa: N806

    class _STFT(nn.Module):
        """torchlibrosa.stft.STFT (center, reflect pad, periodic Hann) as two conv1d."""

        def __init__(self, n_fft: int, hop: int):
            super().__init__()
            self.n_fft = n_fft
            out = n_fft // 2 + 1
            self.conv_real = nn.Conv1d(1, out, n_fft, stride=hop, bias=False)
            self.conv_imag = nn.Conv1d(1, out, n_fft, stride=hop, bias=False)
            n = np.arange(n_fft)
            window = 0.5 - 0.5 * np.cos(2 * np.pi * n / n_fft)  # periodic Hann
            w = np.exp(-2j * np.pi * np.outer(n, n) / n_fft)[:, :out] * window[:, None]
            self.conv_real.weight.data = torch.tensor(np.real(w).T, dtype=torch.float32)[:, None]
            self.conv_imag.weight.data = torch.tensor(np.imag(w).T, dtype=torch.float32)[:, None]

        def forward(self, x):
            x = F.pad(x[:, None, :], (self.n_fft // 2, self.n_fft // 2), mode="reflect")
            real = self.conv_real(x)[:, None].transpose(2, 3)
            imag = self.conv_imag(x)[:, None].transpose(2, 3)
            return real, imag

    class _Spectrogram(nn.Module):
        def __init__(self, n_fft: int, hop: int):
            super().__init__()
            self.stft = _STFT(n_fft, hop)

        def forward(self, x):
            real, imag = self.stft(x)
            return real**2 + imag**2

    class _LogmelFilterBank(nn.Module):
        def __init__(self, sr: int, n_fft: int, n_mels: int, fmin: float, fmax: float):
            super().__init__()
            import librosa

            mel = librosa.filters.mel(sr=sr, n_fft=n_fft, n_mels=n_mels, fmin=fmin, fmax=fmax)
            self.melW = nn.Parameter(torch.tensor(mel.T, dtype=torch.float32))
            self.amin = 1e-10
            self.ref = 1.0

        def forward(self, x):
            mel = torch.matmul(x, self.melW)
            db = 10.0 * torch.log10(torch.clamp(mel, min=self.amin))
            return db - 10.0 * np.log10(max(self.amin, self.ref))

    class ConvBlock(nn.Module):
        def __init__(self, cin: int, cout: int):
            super().__init__()
            self.conv1 = nn.Conv2d(cin, cout, (3, 3), (1, 1), (1, 1), bias=False)
            self.conv2 = nn.Conv2d(cout, cout, (3, 3), (1, 1), (1, 1), bias=False)
            self.bn1 = nn.BatchNorm2d(cout)
            self.bn2 = nn.BatchNorm2d(cout)

        def forward(self, x, pool_size=(2, 2)):
            x = F.relu_(self.bn1(self.conv1(x)))
            x = F.relu_(self.bn2(self.conv2(x)))
            return F.avg_pool2d(x, kernel_size=pool_size)

    class Cnn14(nn.Module):
        def __init__(self, sample_rate=SAMPLE_RATE, window_size=WINDOW_SIZE,
                     hop_size=HOP_SIZE, mel_bins=MEL_BINS, fmin=FMIN, fmax=FMAX,
                     classes_num=N_LABELS):
            super().__init__()
            self.spectrogram_extractor = _Spectrogram(window_size, hop_size)
            self.logmel_extractor = _LogmelFilterBank(
                sample_rate, window_size, mel_bins, fmin, fmax
            )
            self.bn0 = nn.BatchNorm2d(mel_bins)
            self.conv_block1 = ConvBlock(1, 64)
            self.conv_block2 = ConvBlock(64, 128)
            self.conv_block3 = ConvBlock(128, 256)
            self.conv_block4 = ConvBlock(256, 512)
            self.conv_block5 = ConvBlock(512, 1024)
            self.conv_block6 = ConvBlock(1024, 2048)
            self.fc1 = nn.Linear(2048, 2048, bias=True)
            self.fc_audioset = nn.Linear(2048, classes_num, bias=True)

        def forward(self, waveform):
            """(batch, samples) at 32 kHz -> {'clipwise_output', 'embedding'} (eval)."""
            x = self.logmel_extractor(self.spectrogram_extractor(waveform))
            x = self.bn0(x.transpose(1, 3)).transpose(1, 3)
            for block in (self.conv_block1, self.conv_block2, self.conv_block3,
                          self.conv_block4, self.conv_block5):
                x = F.dropout(block(x, (2, 2)), p=0.2, training=self.training)
            x = F.dropout(self.conv_block6(x, (1, 1)), p=0.2, training=self.training)
            x = torch.mean(x, dim=3)
            x = torch.max(x, dim=2)[0] + torch.mean(x, dim=2)
            x = F.dropout(x, p=0.5, training=self.training)
            x = F.relu_(self.fc1(x))
            embedding = F.dropout(x, p=0.5, training=self.training)
            return {"clipwise_output": torch.sigmoid(self.fc_audioset(x)),
                    "embedding": embedding}

    return Cnn14


_CLASSES: dict = {}


def __getattr__(name: str):
    if name == "Cnn14":  # built lazily so importing this module never imports torch
        if "Cnn14" not in _CLASSES:
            _CLASSES["Cnn14"] = _build_classes()
        return _CLASSES["Cnn14"]
    raise AttributeError(name)


class PannsTagger:
    key = MODEL_KEY

    def __init__(self, cache: Path, device: str = "cpu", say=None):
        torch, _, _ = _torch()
        self.device = device
        self.labels = ensure_labels(Path(cache), say)
        checkpoint = ensure_checkpoint(Path(cache), say)
        try:
            state = torch.load(checkpoint, map_location="cpu", weights_only=True)
            model = __getattr__("Cnn14")()
            model.load_state_dict(state["model"] if "model" in state else state)
        except Exception as e:  # noqa: BLE001
            raise ModelUnavailable(
                f"cannot load {checkpoint}: {type(e).__name__}: {e}"
            ) from e
        self.model = model.to(device).eval()

    def tag(self, clips: list[list[np.ndarray]]) -> np.ndarray:
        torch, _, _ = _torch()
        flat = [(ci, w) for ci, ws in enumerate(clips) for w in ws]
        by_len: dict[int, list[int]] = defaultdict(list)
        for i, (_, w) in enumerate(flat):
            by_len[len(w)].append(i)
        probs = np.zeros((len(flat), len(self.labels)), dtype=np.float32)
        with torch.inference_mode():
            for idxs in by_len.values():
                for start in range(0, len(idxs), BATCH_WINDOWS):
                    chunk = idxs[start : start + BATCH_WINDOWS]
                    x = torch.from_numpy(np.stack([flat[i][1] for i in chunk])).to(self.device)
                    out = self.model(x)["clipwise_output"].float().cpu().numpy()
                    probs[chunk] = out
        result = np.zeros((len(clips), len(self.labels)), dtype=np.float32)
        counts = np.zeros(len(clips))
        for i, (ci, _) in enumerate(flat):
            result[ci] += probs[i]
            counts[ci] += 1
        return result / np.maximum(counts, 1)[:, None]
