from __future__ import annotations

import unittest

from src.transcribe.hallucination_filter import HallucinationFilter


class HallucinationFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.filter = HallucinationFilter(enabled=True)

    def test_blocks_known_phrase(self) -> None:
        self.assertTrue(self.filter.is_hallucination("ご視聴ありがとうございました"))

    def test_blocks_repeated_syllable_noise(self) -> None:
        text = "皆さん、すみません、がんがんがんがんがんがんがんがんがんがん"
        self.assertTrue(self.filter.is_hallucination(text))

    def test_blocks_repeated_char_noise(self) -> None:
        self.assertTrue(self.filter.is_hallucination("ああああああああああああああああ"))

    def test_blocks_repeated_clause_noise(self) -> None:
        text = (
            "私は、私のお気に入りのお気に入りのお気に入りのお気に入りの"
            "お気に入りのお気に入りのお気に入りのお気に入りのお気に入り"
        )
        self.assertTrue(self.filter.is_hallucination(text))

    def test_allows_normal_sentence(self) -> None:
        text = "こちら6番目ですが、アカウントロックの期間は30分以上でお願いします。"
        self.assertFalse(self.filter.is_hallucination(text))

    def test_allows_real_text_with_repeated_tail(self) -> None:
        text = (
            "大丈夫ですかね。いつもギリギリまですみません。"
            "本日の朝会これで終わりたいと思います。"
            "よろしくお願いします。よろしくお願いします。"
            "よろしくお願いします。よろしくお願いします。"
        )
        self.assertFalse(self.filter.is_hallucination(text))


if __name__ == "__main__":
    unittest.main()
