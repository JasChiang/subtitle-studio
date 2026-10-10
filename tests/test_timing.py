"""Timing: alignment onto the ASR clock, display timing, karaoke markup, reel subtitles, cut points. No network."""
import array
import unittest

from studio import highlights as hl
from studio import pipeline as pl


def seg(words, start=None, end=None):
    ws = [{"text": t, "start": s, "end": e} for t, s, e in words]
    return {"start": start if start is not None else ws[0]["start"], "end": end if end is not None else ws[-1]["end"],
            "words": ws}


class Align(unittest.TestCase):
    def test_replaced_word_keeps_asr_time_and_dropped_filler_disappears(self):
        s = seg([("you", 5.1, 5.3), ("saw", 5.3, 5.6), ("uh,", 5.7, 5.9), ("Netflix.", 6.0, 6.5)])
        out = pl.align_segment(s, "you sold Netflix.")
        self.assertEqual([u["t"] for u in out], ["you", "sold", "Netflix."])
        # "saw uh," → "sold": the replacement keeps the ASR start and takes over the dropped filler's time
        self.assertEqual(out[1]["s"], 5.3)
        self.assertLessEqual(out[1]["e"], 6.0)
        self.assertEqual((out[2]["s"], out[2]["e"]), (6.0, 6.5))

    def test_words_moved_to_another_segment_still_land_on_their_time(self):
        a = seg([("type", 1.0, 1.2), ("your", 1.2, 1.4), ("movie", 1.4, 1.8)])
        b = seg([("That's", 2.0, 2.3), ("never", 2.3, 2.6)])
        # the corrector moved "movie" into the next segment
        out = pl.align_window([a, b], ["type your", "movie That's never"])
        movie = next(u for u in out if u["t"] == "movie")
        self.assertEqual((movie["s"], movie["e"]), (1.4, 1.8))

    def test_speaker_change_marker(self):
        out = pl.align_segment(seg([("Netflix.", 1.0, 1.5), ("Right.", 1.6, 2.0)]), "Netflix. || Right.")
        self.assertTrue(out[1]["spk"])

    def test_unsqueeze_spreads_crammed_words_and_marks_them(self):
        words = [{"t": "a", "s": 0.0, "e": 0.5}] + \
            [{"t": f"w{i}", "s": 0.5 + i * 0.01, "e": 0.51 + i * 0.01} for i in range(5)] + \
            [{"t": "z", "s": 2.0, "e": 2.4}]
        pl.unsqueeze(words, 0.0, 3.0)
        mid = words[1:6]
        self.assertTrue(all(w.get("est") for w in mid))
        self.assertGreater(mid[-1]["e"] - mid[0]["s"], 0.5)
        self.assertEqual(words[-1]["s"], 2.0)  # an ASR-timed start is never moved


    def test_normal_japanese_characters_are_not_flagged(self):
        words = [{"t": ch, "s": 1.0 + k * 0.08, "e": 1.08 + k * 0.08} for k, ch in enumerate("ちょっと分かりづらい")]
        pl.unsqueeze(words, 0.0, 3.0)
        self.assertFalse(any(w.get("est") for w in words))


class Energy(unittest.TestCase):
    def test_pause_absorbed_into_a_word_is_moved_out(self):
        # 10 ms frames: speech 0–1.0 s, silence 1.0–1.4 s, speech 1.4–2.0 s
        db = array.array("f", [-15.0] * 100 + [-40.0] * 40 + [-15.0] * 60)
        a = {"t": "was", "s": 0.6, "e": 1.5}     # ASR stretched "was" over the pause
        b = {"t": "important", "s": 1.5, "e": 1.6}  # and started the next word late
        self.assertEqual(pl.snap_to_energy([a, b], db), 1)
        self.assertAlmostEqual(a["e"], 1.02, places=2)
        self.assertAlmostEqual(b["s"], 1.40, places=2)


class Display(unittest.TestCase):
    def words(self, spans):
        return [[{"t": f"w{i}{j}", "s": s, "e": e} for j, (s, e) in enumerate(group)] for i, group in enumerate(spans)]

    def test_no_overlap_lead_in_hold_and_minimum(self):
        groups = self.words([[(1.0, 1.3)], [(1.35, 3.0)], [(5.0, 6.0)]])
        cues = pl.finalize_cues(groups)
        for a, b in zip(cues, cues[1:]):
            self.assertLessEqual(a["end"], b["start"])
        self.assertAlmostEqual(cues[2]["start"], 4.9, places=2)   # 0.1 s lead-in into silence
        self.assertAlmostEqual(cues[2]["end"], 6.6, places=2)     # 0.6 s hold
        self.assertGreaterEqual(cues[1]["end"], 3.0)              # never before its last word

    def test_omitted_backchannel_and_two_speaker_cue(self):
        a = [{"t": "So", "s": 1.0, "e": 1.2}, {"t": "anyway.", "s": 1.2, "e": 1.6}]
        yeah = [{"t": "Yeah.", "s": 1.7, "e": 1.9, "line": {"omit": True}}]
        b1 = [{"t": "Not", "s": 3.0, "e": 3.2, "line": {"speaker": "A"}}, {"t": "yet.", "s": 3.2, "e": 3.5}]
        b2 = [{"t": "Not", "s": 3.1, "e": 3.3, "line": {"speaker": "B", "with_prev": True}},
              {"t": "yet.", "s": 3.3, "e": 3.6}]
        cues = pl.finalize_cues([a, yeah, b1, b2])
        self.assertEqual([c["text"] for c in cues], ["So anyway.", "- Not yet.\n- Not yet."])
        self.assertTrue(cues[1]["dialog"])
        self.assertEqual(cues[1]["split"], 2)


class Karaoke(unittest.TestCase):
    cue = {"text": "AI is here", "words": [{"t": "AI", "s": 1.0, "e": 1.2}, {"t": "is", "s": 1.3, "e": 1.4},
                                          {"t": "here", "s": 1.5, "e": 1.9}]}

    def test_marks_spoken_words(self):
        m = pl.marked_source(self.cue, 1.35)
        self.assertEqual(m, "\x01AI is\x02 here")
        self.assertEqual(m.replace("\x01", "").replace("\x02", ""), self.cue["text"])

    def test_two_speaker_lines_highlight_independently(self):
        c = {"text": "- a b\n- c d", "dialog": True, "split": 2,
             "words": [{"t": "a", "s": 1, "e": 1.1}, {"t": "b", "s": 2, "e": 2.1},
                       {"t": "c", "s": 1.05, "e": 1.2}, {"t": "d", "s": 3, "e": 3.1}]}
        self.assertEqual(pl.marked_source(c, 1.5), "- \x01a\x02 b\n- \x01c\x02 d")
        self.assertEqual(pl.dialog_split({**c, "split": None}), 2)


class FrameRate(unittest.TestCase):
    def test_odd_phone_rates_snap_to_standard(self):
        self.assertEqual(hl.standard_fps("290395/2421"), "60")  # iPhone 4K ~120 fps → 59.97 → 60
        self.assertEqual(hl.standard_fps("120/1"), "60")
        self.assertEqual(hl.standard_fps("30000/1001"), "30000/1001")
        self.assertEqual(hl.standard_fps("2997/100"), "30000/1001")
        self.assertEqual(hl.standard_fps("25/1"), "25")
        self.assertEqual(hl.standard_fps("24000/1001"), "24000/1001")
        self.assertEqual(hl.standard_fps("0/0"), "30000/1001")
        self.assertEqual(hl.standard_fps(""), "30000/1001")


class Reel(unittest.TestCase):
    cues = [
        {"text": "first line", "tr": "第一句", "start": 0.9, "end": 2.6,
         "words": [{"t": "first", "s": 1.0, "e": 1.5}, {"t": "line", "s": 1.5, "e": 2.0}]},
        {"text": "next one", "tr": "下一句", "start": 2.05, "end": 3.5,
         "words": [{"t": "next", "s": 2.15, "e": 2.6}, {"t": "one", "s": 2.6, "e": 3.0}]},
    ]

    def test_lead_in_of_the_next_line_does_not_flash_at_a_cut(self):
        subs = hl.reel_subs([(0.9, 2.1, 0, False, 1.0)], self.cues, True, False)
        self.assertEqual([s[2] for s in subs], [["第一句", "first line"]])

    def test_line_belongs_to_clip_only_if_half_its_words_are_inside(self):
        self.assertTrue(hl.spoken_in(self.cues[0], 0.9, 2.1))
        self.assertFalse(hl.spoken_in(self.cues[1], 0.9, 2.3))

    def test_export_srt(self):
        srt = pl.export([{"start": 1.0, "end": 2.5, "text": "Hi", "tr": "嗨"}], "bi")
        self.assertEqual(srt.strip(), "1\n00:00:01,000 --> 00:00:02,500\nHi\n嗨")


if __name__ == "__main__":
    unittest.main()
