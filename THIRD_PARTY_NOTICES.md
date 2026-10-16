# Third-party notices

Foley itself is MIT licensed (see `LICENSE`). This file lists third-party material that
is included in this repository and its wheel, and third-party material that Foley
downloads at run time but does not redistribute. Python dependencies installed from
PyPI or the PyTorch index (numpy, librosa, torch, transformers, ...) carry their own
licences and are not listed here.

## Included in this repository

### PANNs CNN14 architecture (audioset_tagging_cnn)

`src/foley/models/panns.py` contains the CNN14 model architecture adapted from
`pytorch/models.py` in https://github.com/qiuqiangkong/audioset_tagging_cnn
(licence file `LICENSE.MIT`, checked at commit d2f4b8c18eab).

```
The MIT License

Copyright (c) 2018-2020 Qiuqiang Kong

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

### torchlibrosa (log-mel front end)

The STFT and log-mel filter bank modules in `src/foley/models/panns.py` reproduce
`torchlibrosa.stft.Spectrogram` and `LogmelFilterBank` from
https://github.com/qiuqiangkong/torchlibrosa (licence file `LICENSE.md`, checked at
commit d4e8add6c9ee), so that the pretrained CNN14 checkpoint loads unchanged.

```
The MIT License

Copyright (c) 2018-2021 Qiuqiang Kong

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

### Fonts (`foley browse`)

The web UI ships Latin WOFF2 subsets of three fonts under the SIL Open Font License,
Version 1.1. The full licence text, with each font's copyright notice, is next to the
font files and is also included in the wheel's licence files:

| Font | Files | Copyright | Licence text |
|---|---|---|---|
| Barlow | `Barlow-400/500/600.woff2` | 2017 The Barlow Project Authors (https://github.com/jpt/barlow) | `src/foley/browse_ui/fonts/Barlow-OFL.txt` |
| JetBrains Mono | `JetBrainsMono.woff2` | 2020 The JetBrains Mono Project Authors (https://github.com/JetBrains/JetBrainsMono) | `src/foley/browse_ui/fonts/JetBrainsMono-OFL.txt` |
| Michroma | `Michroma-400.woff2` | 2011 The Michroma Project Authors (https://github.com/googlefonts/Michroma-font) | `src/foley/browse_ui/fonts/Michroma-OFL.txt` |

### Universal Category System (UCS) list

`src/foley/data/ucs_catids.csv`

The CatID list of the Universal Category System, v8.2.1, from
https://universalcategorysystem.com ("UCS v8.2.1 Full List.xlsx"), reduced to its CatID, Category,
SubCategory and CatShort columns. UCS, created by Tim Nielsen, Kai Paquin and Justin
Drury, describes itself as "a public domain
initiative" for naming and categorizing sound effects; it is included here with
attribution to the UCS project and its contributors.

## Downloaded at run time, not redistributed

With the optional `models` extra, `foley index` downloads the following on first use.
None of it is in this repository or the wheel; each is used under its own terms, and
anything you redistribute (for example a catalog containing tags or embeddings) is your
responsibility.

### PANNs CNN14 pretrained weights

`Cnn14_mAP=0.431.pth` from Zenodo record 3987831
(https://zenodo.org/records/3987831), downloaded into `<data_dir>/models/panns/` and
md5-verified. Licence: Creative Commons Attribution 4.0 International (CC BY 4.0).

> Qiuqiang Kong, Yin Cao, Turab Iqbal, Yuxuan Wang, Wenwu Wang, Mark D. Plumbley.
> "PANNs: Large-Scale Pretrained Audio Neural Networks for Audio Pattern Recognition."
> IEEE/ACM Transactions on Audio, Speech, and Language Processing, 2020.
> Pretrained models: https://doi.org/10.5281/zenodo.3987831

### LAION CLAP checkpoints

Loaded through Hugging Face `transformers` into the Hugging Face cache (`HF_HOME`),
each pinned to a commit in `src/foley/models/clap.py`. Each model card states the
Apache License 2.0 (https://www.apache.org/licenses/LICENSE-2.0):

| Checkpoint | Pinned revision | Licence |
|---|---|---|
| https://huggingface.co/laion/larger_clap_general | `ada0c23a36c4` | Apache-2.0 |
| https://huggingface.co/laion/clap-htsat-unfused | `8fa0f1c6d043` | Apache-2.0 |
| https://huggingface.co/laion/clap-htsat-fused | `365dea6ef167` | Apache-2.0 |

> Yusong Wu, Ke Chen, Tianyu Zhang, Yuchen Hui, Taylor Berg-Kirkpatrick, Shlomo Dubnov.
> "Large-scale Contrastive Language-Audio Pretraining with Feature Fusion and
> Keyword-to-Caption Augmentation." ICASSP 2023.

### AudioSet label list

`class_labels_indices.csv` (the 527 AudioSet class names, in CNN14's output order),
downloaded from Google's AudioSet storage
(https://storage.googleapis.com/us_audioset/youtube_corpus/v1/csv/class_labels_indices.csv)
into `<data_dir>/models/panns/`. The label names become the `tags` rows of your
catalog. Per https://research.google.com/audioset/download.html, the AudioSet dataset
is made available by Google Inc. under CC BY 4.0, and the AudioSet ontology, from
which the class names are taken, under Creative Commons Attribution-ShareAlike 4.0
International (CC BY-SA 4.0).

> Jort F. Gemmeke, Daniel P. W. Ellis, Dylan Freedman, Aren Jansen, Wade Lawrence,
> R. Channing Moore, Manoj Plakal, Marvin Ritter. "Audio Set: An ontology and
> human-labeled dataset for audio events." ICASSP 2017.
