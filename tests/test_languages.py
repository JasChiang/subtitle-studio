"""Multilingual sources: language codes → Apple ASR locales, other-language stretches, merging re-recognised
segments. No network."""
import unittest

from studio import pipeline as pl

SUPPORTED = ["en-US", "en-GB", "es-ES", "ja-JP", "ko-KR", "yue-CN", "zh-CN", "zh-HK", "zh-TW"]


class Locales(unittest.TestCase):
    def test_codes_from_gemini_map_to_apple_locales(self):
        self.assertEqual(pl.to_locale("zh-TW", SUPPORTED), "zh-TW")
        self.assertEqual(pl.to_locale("zh-Hant", SUPPORTED), "zh-TW")
        self.assertEqual(pl.to_locale("zh-Hans", SUPPORTED), "zh-CN")
        self.assertEqual(pl.to_locale("cmn", SUPPORTED), "zh-TW")
        self.assertEqual(pl.to_locale("en", SUPPORTED), "en-US")
        self.assertEqual(pl.to_locale("en_GB", SUPPORTED), "en-GB")
        self.assertEqual(pl.to_locale("ja", SUPPORTED), "ja-JP")
        self.assertIsNone(pl.to_locale("ru", SUPPORTED))
        self.assertIsNone(pl.to_locale("", SUPPORTED))

    def test_lang_base(self):
        self.assertEqual(pl.lang_base("zh-TW"), "zh")
        self.assertEqual(pl.lang_base("en_US"), "en")
        self.assertEqual(pl.lang_base("yue-HK"), "yue")


class Targets(unittest.TestCase):
    def test_never_translate_into_the_spoken_language(self):
        self.assertEqual(pl.auto_target("zh-TW"), "English")
        self.assertEqual(pl.auto_target("yue-CN"), "English")
        self.assertEqual(pl.auto_target("en-US"), "繁體中文（台灣）")
        self.assertEqual(pl.auto_target("ja-JP"), "繁體中文（台灣）")
        self.assertEqual(pl.auto_target(""), "繁體中文（台灣）")


class OtherLanguageSpans(unittest.TestCase):
    # ASR words every 0.4 s (0.3 s long) with gaps at 5.1–5.3 and 10.0–10.4
    words = [{"s": t / 10, "e": t / 10 + 0.3} for t in list(range(0, 50, 4)) + list(range(53, 100, 4)) +
             list(range(104, 150, 4))]

    def test_whole_english_lines_inside_chinese_become_one_span_snapped_to_pauses(self):
        ref = [{"t": 0.0, "lang": "zh-TW", "text": "大家好"},
               {"t": 5.2, "lang": "en", "text": "Thanks for having me."},
               {"t": 7.6, "lang": "en", "text": "It's great to be here."},
               {"t": 10.3, "lang": "zh-TW", "text": "那我們開始吧"}]
        spans = pl.other_language_spans(ref, "zh-TW", self.words)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["lang"], "en")
        self.assertAlmostEqual(spans[0]["start"], 5.2, places=2)   # middle of the pause before the English
        self.assertAlmostEqual(spans[0]["end"], 10.2, places=2)    # middle of the pause after it

    def test_short_or_same_language_lines_are_ignored(self):
        ref = [{"t": 0.0, "lang": "zh-TW", "text": "大家好"}, {"t": 2.0, "lang": "en", "text": "OK."},
               {"t": 3.0, "lang": "zh", "text": "繼續"}]
        self.assertEqual(pl.other_language_spans(ref, "zh-TW", self.words), [])


class KoreanSpacing(unittest.TestCase):
    def test_correct_words_keep_the_asr_spacing(self):
        asr = "그 태 태웅님이라고 불러도 될까요"
        corrected = "그 태웅, 태웅 님이라고 불러도 될까요?"
        self.assertEqual(pl.keep_asr_spacing(asr, corrected), "그 태웅, 태웅님이라고 불러도 될까요?")

    def test_changed_words_keep_the_correctors_spacing(self):
        self.assertEqual(pl.keep_asr_spacing("자막 어루를 소개", "자막 도구를 소개"), "자막 도구를 소개")


class MergeReasr(unittest.TestCase):
    def test_words_inside_the_span_are_replaced(self):
        asr = {"duration": 12, "segments": [
            {"text": "大 家 好 三 克 油", "start": 0.0, "end": 6.0, "words": [
                {"text": "大家好", "start": 0.0, "end": 1.0}, {"text": "三克油", "start": 5.2, "end": 6.0}]},
            {"text": "那我們開始", "start": 10.4, "end": 11.5, "words": [{"text": "那我們開始", "start": 10.4, "end": 11.5}]}]}
        spans = [{"lang": "en", "start": 5.0, "end": 10.2}]
        redone = [[{"text": "Thank you.", "start": 5.2, "end": 6.0,
                    "words": [{"text": "Thank", "start": 5.2, "end": 5.5}, {"text": "you.", "start": 5.5, "end": 6.0}]}]]
        out = pl.merge_reasr(asr, spans, redone)
        self.assertEqual([s["text"] for s in out["segments"]], ["大家好", "Thank you.", "那我們開始"])
        self.assertEqual(out["segments"][1]["lang"], "en")
        self.assertEqual(out["reasr"], spans)


if __name__ == "__main__":
    unittest.main()
