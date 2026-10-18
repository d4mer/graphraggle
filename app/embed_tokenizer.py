"""app/embed_tokenizer.py — count tokens the way the SERVED embedder does.

Why this exists
---------------
cl100k is the right ruler for CHUNK SIZES and the wrong one for the embedder's window.
mxbai-embed-large-v1 is a BERT/XLM-R style tokenizer, and names, numbers, dates and STT
garbles cost it MORE tokens than cl100k counts. A chunk that fits a 512-token cl100k
budget can still be truncated silently, and truncation is invisible: the embedder returns a
vector for the prefix and nobody is told. Any claim of the form "no piece exceeds the
embedder window" has to be measured with the embedder's own vocabulary.

What is supported
-----------------
Read from LOCAL files only - nothing here ever downloads, because a component that fetches
a model at run time fails at 3 a.m. for a reason unrelated to the experiment:

* HF ``tokenizer.json`` with a ``WordPiece`` or ``Unigram`` model (the two shapes this
  deployment's embedders use), including ``added_tokens`` specials and the ``do_lower_case``
  / ``Lowercase`` normalizer flags;
* a plain ``vocab.txt`` (one token per line, ``##`` continuations).

Normalisation is deliberately narrow: case folding when the files ask for it, and the
``\u2581`` word-boundary marker for sentencepiece-style (Unigram) vocabularies. Nothing else
is claimed - the number is a WINDOW test, not a reproduction of the serving stack's
pre-tokenizer. Anything else (BPE, a real sentencepiece model file) raises rather than being
approximated by cl100k, because a silent approximation is how a window claim becomes a lie.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Sequence

EMBED_WINDOW_DEFAULT = 512        # mxbai-embed-large-v1: 512 including special tokens
_UNIGRAM_UNK_SCORE = -12.0        # same order as sentencepiece's unk_penalty


class EmbedTokenizerError(Exception):
    """The tokenizer was not usable; the caller decides whether that is fatal."""

class LocalTokenizer:
    """Counts tokens the way the served embedder does, from a local vocabulary.

    ``count`` includes the special tokens the sequence is wrapped in, because that is what
    the 512-token truncation is measured against. Normalisation is deliberately narrow:
    case folding when the files ask for it, and the ``\u2581`` word-boundary marker for
    Unigram (sentencepiece-style) vocabularies. Anything beyond that is not claimed: the
    number is a window test, not a reproduction of the serving stack's pre-tokenizer.
    """

    def __init__(self, *, kind: str, entries: Sequence[tuple[str, float | None]],
                 unk_token: str, continuing_prefix: str = "##",
                 special_tokens: Sequence[str] = (), lowercase: bool = False,
                 label: str = "tokenizer") -> None:
        self.kind = kind
        self.unk_token = unk_token or "[UNK]"
        self.continuing_prefix = continuing_prefix
        self.special_tokens = [token for token in special_tokens if token]
        self.lowercase = bool(lowercase)
        self.body: dict[str, float | None] = {
            token: score for token, score in entries
            if token and token != self.unk_token and token not in self.special_tokens}
        self.max_piece = max((len(token) for token in self.body), default=1)
        # [CLS]/[SEP] or <s>/</s>: the pair that wraps every input.
        self.special_overhead = min(2, len(self.special_tokens))
        self.label = label
        self.name = (f"{kind.lower()}-{len(self.body)}-entry-local"
                     f"+{self.special_overhead}special")

    def prepare(self, text: str) -> str:
        prepared = str(text)
        if self.lowercase:
            prepared = prepared.lower()
        if self.kind == "unigram":
            prepared = "\u2581" + re.sub(r"\s+", "\u2581", prepared.strip())
        return prepared

    def segments(self, text: str) -> list[str]:
        prepared = self.prepare(text)
        if not prepared.strip("\u2581"):
            return []
        return (self._unigram_segments(prepared) if self.kind == "unigram"
                else self._wordpiece_segments(prepared))

    def count(self, text: str) -> int:
        return len(self.segments(text)) + self.special_overhead

    def _wordpiece_segments(self, text: str) -> list[str]:
        pieces: list[str] = []
        for word in text.split():
            found = self._wordpiece_word(word)
            pieces.extend(found if found is not None else [self.unk_token])
        return pieces

    def _wordpiece_word(self, word: str) -> list[str] | None:
        """Greedy longest-match, the way BERT tokenizers cut. None means whole-word [UNK]."""
        out: list[str] = []
        start = 0
        while start < len(word):
            end = min(len(word), start + self.max_piece)
            hit = None
            while end > start:
                piece = word[start:end] if start == 0 else self.continuing_prefix + word[start:end]
                if piece in self.body:
                    hit = piece
                    break
                end -= 1
            if hit is None:
                return None
            out.append(hit)
            start = end
        return out

    def _unigram_segments(self, text: str) -> list[str]:
        """Max-probability segmentation over the unigram scores (Viterbi over positions)."""
        total = len(text)
        best: list[float | None] = [None] * (total + 1)
        back: list[tuple[int, str] | None] = [None] * (total + 1)
        best[0] = 0.0
        for start in range(total):
            if best[start] is None:
                continue
            for length in range(min(self.max_piece, total - start), 0, -1):
                token = text[start:start + length]
                score = self.body.get(token)
                if score is None:
                    if length != 1:
                        continue
                    score = _UNIGRAM_UNK_SCORE
                candidate = float(best[start]) + float(score)
                if best[start + length] is None or candidate > best[start + length]:
                    best[start + length] = candidate
                    back[start + length] = (start, token)
        if best[total] is None:
            return [self.unk_token]
        out: list[str] = []
        cursor = total
        while cursor > 0 and back[cursor] is not None:
            start, token = back[cursor]
            out.append(token)
            cursor = start
        return list(reversed(out))


def _not_found_hint() -> str:
    """What to do about a tokenizer path that is not there. No fetch is attempted."""
    return ("this script never downloads a tokenizer; fetch it once "
            "(`huggingface-cli download mixedai/mxbai-embed-large-v1`) and pass "
            "the resulting directory, or point at tokenizer.json / vocab.txt")


def load_embed_tokenizer(spec: str | None) -> LocalTokenizer | None:
    """Build a LocalTokenizer from a local directory or tokenizer.json/vocab.txt file.

    None means "not requested". A path that does not exist is a hard error naming what was
    asked for: an hf hub id is NOT fetched, because a benchmark that downloads at run time
    silently changes what it measured.
    """
    if spec is None or not str(spec).strip():
        return None
    requested = str(spec).strip()
    candidate = Path(requested).expanduser()
    if not candidate.exists():
        raise EmbedTokenizerError(f"--embed-tokenizer {requested!r} is not a readable "
                                  f"local path: {_not_found_hint()}.")
    token_file = candidate if candidate.is_file() else None
    vocab_file: Path | None = None
    if candidate.is_dir():
        for name in ("tokenizer.json", "tokenizer.json.gz"):
            if (candidate / name).is_file():
                token_file = candidate / name
                break
        if (candidate / "vocab.txt").is_file():
            vocab_file = candidate / "vocab.txt"
        if token_file is None and vocab_file is None:
            raise EmbedTokenizerError("--embed-tokenizer directory holds neither tokenizer.json nor "
                             "vocab.txt (looked in the directory you passed)")
    if token_file is None:
        token_file = vocab_file
    assert token_file is not None
    return _read_tokenizer_files(token_file, candidate if candidate.is_dir() else token_file.parent)


def _read_tokenizer_files(token_file: Path, directory: Path) -> LocalTokenizer:
    """Parse HF tokenizer.json or a plain vocab.txt. Unsupported model types say so."""
    if token_file.suffix == ".json":
        try:
            data = json.loads(token_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EmbedTokenizerError(f"tokenizer.json could not be read as JSON: {exc}") from exc
        model = data.get("model") if isinstance(data.get("model"), dict) else {}
        kind = str(model.get("type") or "").strip().lower()
        added = [str(entry.get("content") or "") for entry in (data.get("added_tokens") or [])
                 if isinstance(entry, dict) and entry.get("special")]
        specials = [str(item) for item in (model.get("special_tokens") or []) if item] or added
        normalizers = data.get("normalizer")
        flattened: list[Any] = []
        if isinstance(normalizers, dict):
            flattened = [normalizers, *(normalizers.get("normalizers") or [])]
        lowercase = any(str(item.get("type") or "").lower() == "lowercase"
                        for item in flattened if isinstance(item, dict))
        config = directory / "tokenizer_config.json"
        if config.is_file():
            try:
                lowercase = lowercase or bool(json.loads(config.read_text(encoding="utf-8"))
                                              .get("do_lower_case"))
            except (OSError, json.JSONDecodeError):
                pass
        if kind == "wordpiece":
            vocab = model.get("vocab") if isinstance(model.get("vocab"), dict) else {}
            if not vocab:
                raise EmbedTokenizerError("tokenizer.json declares WordPiece but carries no vocab.")
            return LocalTokenizer(kind="wordpiece", entries=[(token, None) for token in vocab],
                                  unk_token=str(model.get("unk_token") or "[UNK]"),
                                  continuing_prefix=str(model.get("continuing_subword_prefix")
                                                        or "##"),
                                  special_tokens=specials or ["[CLS]", "[SEP]"],
                                  lowercase=lowercase)
        if kind == "unigram":
            vocab = model.get("vocab") if isinstance(model.get("vocab"), list) else []
            entries = [(str(pair[0]), float(pair[1]) if len(pair) > 1 else 0.0)
                       for pair in vocab if isinstance(pair, (list, tuple)) and pair]
            if not entries:
                raise EmbedTokenizerError("tokenizer.json declares Unigram but carries no vocab.")
            unk = str(model.get("unk_token") or "[UNK]")
            if unk == "[UNK]":
                unk_id = model.get("unk_id")
                if isinstance(unk_id, int) and 0 <= int(unk_id) < len(vocab):
                    unk = str(vocab[unk_id][0])
            return LocalTokenizer(kind="unigram", entries=entries, unk_token=unk,
                                  special_tokens=specials or ["<s>", "</s>"],
                                  lowercase=lowercase or True)
        raise EmbedTokenizerError(
            f"tokenizer model type {model.get('type')!r} cannot be counted locally. Supported: "
            "WordPiece and Unigram vocabularies. Install the `tokenizers` library (absent from "
            "this venv) or point --embed-tokenizer at a WordPiece/Unigram tokenizer.")
    try:
        lines = token_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EmbedTokenizerError(f"vocab.txt could not be read: {exc}") from exc
    vocab = [line for line in lines if line.strip()]
    if not vocab:
        raise EmbedTokenizerError("vocab.txt is empty")
    specials = [token for token in ("[CLS]", "[SEP]", "[UNK]", "[PAD]", "[MASK]") if token in vocab]
    return LocalTokenizer(kind="wordpiece", entries=[(token, None) for token in vocab],
                          unk_token="[UNK]", special_tokens=specials or ["[CLS]", "[SEP]"])


