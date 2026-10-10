"""Text handling across languages: tokenising, joining, spacing rules, script detection. No network."""
import unittest

from studio import pipeline as pl


class Tokenize(unittest.TestCase):
    def roundtrip(self, s):
        return pl.join_tokens(pl.tokenize(s))

    def test_english_keeps_words_and_punctuation(self):
        self.assertEqual(pl.tokenize("I'm a moron."), ["I'm", "a", "moron."])
        self.assertEqual(self.roundtrip("Hello, world — again"), "Hello, world — again")

    def test_chinese_is_per_character(self):
        self.assertEqual(pl.tokenize("你好，世界"), ["你", "好，", "世", "界"])
        self.assertEqual(self.roundtrip("你好，世界"), "你好，世界")

    def test_mixed_chinese_english_gets_spaces(self):
        self.assertEqual(self.roundtrip("我覺得AI很厲害，對吧？"), "我覺得 AI 很厲害，對吧？")

    def test_japanese_is_per_character_without_spaces(self):
        self.assertEqual(self.roundtrip("今日はいい天気ですね。"), "今日はいい天気ですね。")

    def test_japanese_no_space_around_digits_latin_or_wave_dash(self):
        self.assertEqual(self.roundtrip("ホロライブ3期生の、兎田ぺこ〜らぺこ〜！"), "ホロライブ3期生の、兎田ぺこ〜らぺこ〜！")
        self.assertEqual(self.roundtrip("VTuberのディアナちゃんで〜す"), "VTuberのディアナちゃんで〜す")
        self.assertEqual(self.roundtrip("Usada Pekoraです"), "Usada Pekoraです")
        self.assertEqual(pl.ja_unspace("38 万！？"), "38万！？")

    def test_karaoke_line_follows_cue_spacing(self):
        ws = [{"t": t, "s": i, "e": i + 1} for i, t in enumerate(["38", "万", "！？"])]
        self.assertEqual(pl.marked_source({"text": "38万！？", "words": ws}, -1), "38万！？")
        self.assertEqual(pl.marked_source({"text": "38 万！？", "words": ws}, -1), "38 万！？")
        ws = [{"t": t, "s": i, "e": i + 1} for i, t in enumerate(["で", "〜", "す", "！"])]
        self.assertEqual(pl.marked_source({"text": "で〜す！", "words": ws}, 0), pl.HL_ON + "で" + pl.HL_OFF + "〜す！")
        self.assertEqual(pl.ja_unspace("- AI すごい\n- Usada Pekora"), "- AIすごい\n- Usada Pekora")

    def test_korean_keeps_spaces_between_words(self):
        self.assertEqual(self.roundtrip("안녕하세요 저는 개발자입니다."), "안녕하세요 저는 개발자입니다.")
        self.assertEqual(pl.tokenize("저는 개발자"), ["저는", "개발자"])

    def test_norm_keeps_indic_vowel_signs(self):
        self.assertNotEqual(pl.norm("नमस्ते"), pl.norm("नमस्त"))
        self.assertEqual(pl.norm("Hello,"), "hello")


class Scripts(unittest.TestCase):
    def test_script_of(self):
        self.assertEqual(pl.script_of("這是中文"), "cjk")
        self.assertEqual(pl.script_of("これは日本語"), "cjk")
        self.assertEqual(pl.script_of("한국어 문장"), "hangul")
        self.assertEqual(pl.script_of("plain English"), "latin")

    def test_line_length_defaults(self):
        words = lambda s: [{"t": t, "s": 0, "e": 0} for t in pl.tokenize(s)]
        self.assertEqual(pl.cue_limits(words("這是中文句子"), {"max_chars": 0})[1], 16)
        self.assertEqual(pl.cue_limits(words("한국어 문장입니다"), {"max_chars": 0})[1], 20)
        self.assertEqual(pl.cue_limits(words("an English line"), {"max_chars": 0})[1], 42)
        self.assertEqual(pl.cue_limits(words("an English line"), {"max_chars": 30})[1], 30)

    def test_translation_line_length_follows_target_language(self):
        self.assertEqual(pl.target_line_limit({"target_lang": "繁體中文（台灣）"}), 16)
        self.assertEqual(pl.target_line_limit({"target_lang": "日本語"}), 16)
        self.assertEqual(pl.target_line_limit({"target_lang": "Korean"}), 20)
        self.assertEqual(pl.target_line_limit({"target_lang": "English"}), 42)


class Spacing(unittest.TestCase):
    def test_cjk_spacing(self):
        self.assertEqual(pl.cjk_spacing("我54歲，在AI公司"), "我 54 歲，在 AI 公司")
        self.assertEqual(pl.cjk_spacing("「OpenAI」的"), "「OpenAI」的")

    def test_rule_only_for_chinese(self):
        zh = [{"text": "x", "tr": "AI公司"}]
        ja = [{"text": "x", "tr": "AIは便利"}]
        self.assertEqual(pl.apply_cjk_spacing(zh, {"target_lang": "繁體中文（台灣）", "locale": "en-US"}), 1)
        self.assertEqual(zh[0]["tr"], "AI 公司")
        self.assertEqual(pl.apply_cjk_spacing(ja, {"target_lang": "日本語", "locale": "en-US"}), 0)
        self.assertEqual(ja[0]["tr"], "AIは便利")

    def test_can_be_turned_off(self):
        c = [{"text": "x", "tr": "AI公司"}]
        self.assertEqual(pl.apply_cjk_spacing(c, {"target_lang": "繁體中文", "cjk_spacing": False}), 0)


class Languages(unittest.TestCase):
    def test_han_variant(self):
        self.assertEqual(pl.han_variant({"target_lang": "繁體中文（台灣）"}), "TC")
        self.assertEqual(pl.han_variant({"target_lang": "简体中文"}), "SC")
        self.assertEqual(pl.han_variant({"target_lang": "粵語（香港）"}), "HK")

    def test_style_follows_target_language(self):
        self.assertIn("台灣", pl.style_for({"target_lang": "繁體中文（台灣）", "translate_style": ""}))
        self.assertIn("大陸", pl.style_for({"target_lang": "简体中文", "translate_style": ""}))
        self.assertIn("native speaker", pl.style_for({"target_lang": "日本語", "translate_style": ""}))
        self.assertEqual(pl.style_for({"target_lang": "日本語", "translate_style": "敬體"}), "敬體")


class TranslationChecks(unittest.TestCase):
    def test_echo_detects_shifted_answers(self):
        self.assertTrue(pl.echo_ok("Not yet.", "Not yet."))
        self.assertFalse(pl.echo_ok("Yeah, not yet", "Not yet."))
        self.assertTrue(pl.echo_ok("- Not yet", "- Not yet.\n- Not yet."))
        self.assertTrue(pl.echo_ok("因為那是", "因為那是品味"))

    def test_sentence_groups(self):
        cues = [{"text": t, "start": i, "end": i + 0.9} for i, t in enumerate(
            ["This is gonna be the last thing", "that gets replaced.", "Yes.", "And then"])]
        self.assertEqual(pl.sentence_groups(cues, [0, 1, 2, 3]), [[0, 1], [2], [3]])


if __name__ == "__main__":
    unittest.main()


class Proxy(unittest.TestCase):
    base = {"has_video": True, "codec": "h264", "pix_fmt": "yuv420p", "container": "mov,mp4,m4a,3gp,3g2,mj2",
            "width": 1920, "height": 1080, "bit_rate": 1_300_000}

    def test_browser_friendly_1080p_h264_needs_none(self):
        self.assertIsNone(pl.needs_proxy(self.base))

    def test_reasons(self):
        self.assertIsNotNone(pl.needs_proxy({**self.base, "codec": "prores"}))
        self.assertIsNotNone(pl.needs_proxy({**self.base, "width": 3840, "height": 2160}))
        self.assertIsNotNone(pl.needs_proxy({**self.base, "bit_rate": 60_000_000}))
        self.assertIsNotNone(pl.needs_proxy({**self.base, "pix_fmt": "yuv420p10le"}))
        self.assertIsNotNone(pl.needs_proxy({**self.base, "container": "matroska,webm", "ext": ".mkv"}))
        self.assertIsNone(pl.needs_proxy({**self.base, "container": "matroska,webm", "ext": ".webm", "codec": "vp9"}))
        self.assertIsNotNone(pl.needs_proxy({**self.base, "container": "avi", "ext": ".avi"}))

    def test_modes(self):
        self.assertIsNotNone(pl.needs_proxy(self.base, "always"))
        self.assertIsNone(pl.needs_proxy({**self.base, "codec": "prores"}, "never"))
        self.assertIsNone(pl.needs_proxy({**self.base, "has_video": False}, "always"))
