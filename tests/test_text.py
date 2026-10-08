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
        self.assertEqual(pl.cue_limits(words("這是中文句子"), {"max_chars": 0})[1], 22)
        self.assertEqual(pl.cue_limits(words("한국어 문장입니다"), {"max_chars": 0})[1], 30)
        self.assertEqual(pl.cue_limits(words("an English line"), {"max_chars": 0})[1], 70)


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
