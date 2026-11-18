from __future__ import annotations

from collections import Counter
import re
from typing import Iterable

_PUNCT_SPACE_RE = re.compile(r"[ \t\r\n\u3000。、，,.．・:：;；!！?？~～…'\"“”‘’`´\-_()\[\]{}<>「」『』【】]+")
_REPEATED_CHAR_RE = re.compile(r"(.)\1{7,}")

# 単独で出ると意味を持たないノイズトークン (whisper が無音や息で生成しがち)
# "うん" "はい" "そう" "ええ" など意味のある短い相づちはあえて除外している
NOISE_TOKENS_EXACT: frozenset = frozenset({
    "ん", "んん", "んんん", "んー", "んーん", "んっ",
    "あ", "ああ", "あー", "あぁ", "あっ",
    "う", "うー", "うぅ",
    "ぁ", "ぃ", "ぅ", "ぇ", "ぉ",
    "っ", "ー",
})
# 4 文字以下で、これらの文字のみで構成されていればノイズ扱い
# (普通の母音 あ い う え お は含めない → "うん" "ええ" 等は残る)
_NOISE_CHARS: frozenset = frozenset("んぁぃぅぇぉっー")


DEFAULT_BLOCKED_PHRASES = [
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございます",
    "最後までご視聴ありがとうございました",
    "チャンネル登録お願いします",
    "高評価とチャンネル登録お願いします",
    "ご清聴ありがとうございました",
    "本日はご覧いただきありがとうございました",
    "また次回お会いしましょう",
    "字幕はアマラコミュニティによって作成されました",
    "sous titres realises par la communaute damara org",
    "thanks for watching",
    "thank you for watching",
    "subscribe to my channel",
]


def normalize_text(text: str) -> str:
    if not text:
        return ""
    lowered = text.strip().lower()
    return _PUNCT_SPACE_RE.sub("", lowered)


class HallucinationFilter:
    def __init__(
        self,
        enabled: bool = True,
        blocked_phrases: Iterable[str] | None = None,
        max_direct_match_len: int = 64,
    ) -> None:
        self.enabled = enabled
        self.max_direct_match_len = max_direct_match_len
        phrases = list(DEFAULT_BLOCKED_PHRASES)
        if blocked_phrases:
            phrases.extend(str(p) for p in blocked_phrases if str(p).strip())
        self._blocked = {normalize_text(p) for p in phrases if normalize_text(p)}

    @classmethod
    def from_config(cls, cfg: dict | None) -> HallucinationFilter:
        cfg = cfg or {}
        return cls(
            enabled=bool(cfg.get("enabled", True)),
            blocked_phrases=cfg.get("blocked_phrases"),
            max_direct_match_len=int(cfg.get("max_direct_match_len", 64)),
        )

    def is_hallucination(self, text: str) -> bool:
        if not self.enabled:
            return False
        normalized = normalize_text(text)
        if not normalized:
            return False

        # 単独ノイズトークン (例: "ん", "んん", "ぁ") を除外
        if normalized in NOISE_TOKENS_EXACT:
            return True
        if len(normalized) <= 4 and all(c in _NOISE_CHARS for c in normalized):
            return True

        # 典型パターンは「短い定型句」なので、短文一致を最優先で除外。
        if len(normalized) <= self.max_direct_match_len and normalized in self._blocked:
            return True

        # 同じ定型句の反復 (例: ご視聴ありがとうございました×N) を除外。
        for blocked in self._blocked:
            if len(blocked) < 6:
                continue
            repetitions = normalized.count(blocked)
            if (
                repetitions >= 2
                and repetitions * len(blocked) / len(normalized) >= 0.6
            ):
                return True

        # 文字/短い語幹の異常反復 (例: がんがんがん..., ああああああ...)
        if self._is_repetition_noise(normalized):
            return True
        return False

    def _is_repetition_noise(self, normalized: str) -> bool:
        if len(normalized) < 12:
            return False

        # 同一文字の長大連続
        if _REPEATED_CHAR_RE.search(normalized):
            return True

        # 短い単位 (2-4文字) の繰り返し
        for unit_len in (2, 3, 4):
            min_repeat = 5 if unit_len == 2 else 4
            if self._has_repeated_unit(normalized, unit_len=unit_len, min_repeat=min_repeat):
                return True

        # 中〜長い単位 (5-12文字, フレーズ単位) が3回以上繰り返されるケース
        # 例: 「おやすみなさいおやすみなさいおやすみなさい」, 「これこれ目指すわこれこれ目指すわ...」
        for unit_len in range(5, 13):
            if self._has_repeated_unit(normalized, unit_len=unit_len, min_repeat=3):
                return True

        # 長文なのに文字種類が極端に少ない場合はノイズ扱い
        if len(normalized) >= 30:
            unique_ratio = len(set(normalized)) / len(normalized)
            if unique_ratio <= 0.30:
                return True

        # 句単位の反復 (緩めの閾値)
        if len(normalized) >= 60 and self._has_dominant_ngram(
            normalized, ngram_len=6, min_count=6, min_coverage=0.40
        ):
            return True
        return False

    @staticmethod
    def _has_repeated_unit(text: str, unit_len: int, min_repeat: int) -> bool:
        if unit_len <= 0 or min_repeat <= 1:
            return False
        n = len(text)
        span = unit_len * min_repeat
        if n < span:
            return False
        for i in range(0, n - span + 1):
            unit = text[i:i + unit_len]
            if not unit:
                continue
            repeats = 1
            j = i + unit_len
            while j + unit_len <= n and text[j:j + unit_len] == unit:
                repeats += 1
                if repeats >= min_repeat and unit_len * repeats / n >= 0.6:
                    return True
                j += unit_len
        return False

    @staticmethod
    def _has_dominant_ngram(
        text: str,
        ngram_len: int,
        min_count: int,
        min_coverage: float,
    ) -> bool:
        if ngram_len <= 1 or len(text) < ngram_len * 4:
            return False
        grams = [text[i:i + ngram_len] for i in range(0, len(text) - ngram_len + 1)]
        if not grams:
            return False
        gram, cnt = Counter(grams).most_common(1)[0]
        if cnt < min_count:
            return False
        if len(set(gram)) <= 1:
            return False
        coverage = (cnt * ngram_len) / max(1, len(text))
        return coverage >= min_coverage
