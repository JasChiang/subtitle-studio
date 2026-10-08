"""End-to-end on a Mac, no network: macOS text-to-speech reads a known script → Apple ASR → alignment →
rule-based cues → SRT. Skipped where `say` / bin/apple-asr / ffmpeg are unavailable."""
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from studio import pipeline as pl

SCRIPT = ("Subtitle Studio turns a long interview into accurate subtitles. "
          "Every word gets its own timestamp. Then the subtitles are translated.")


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("say") and shutil.which("ffmpeg")
                     and pl.ASR_BIN.exists(), "needs macOS, `say`, ffmpeg and ./setup.sh")
class MacPipeline(unittest.TestCase):
    def test_speech_to_subtitles(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            aiff = tmp / "speech.aiff"
            subprocess.run(["say", "-v", "Samantha", "-o", str(aiff), SCRIPT], check=True)
            media = tmp / "clip.m4a"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(aiff), "-c:a", "aac", str(media)], check=True)
            proj = tmp / "proj"
            proj.mkdir()
            (proj / "project.json").write_text(json.dumps({
                "id": "test", "name": "clip", "media": str(media), "created": time.time(),
                "options": {**pl.DEFAULT_OPTIONS, "locale": "en-US", "semantic_breaks": False, "translate": False},
                "done": [], "status": {}}))
            p = pl.Project(proj)
            pl.stage_asr(p)
            pl.stage_cues(p)  # no corrected.json → uses the ASR text; rule-based breaks, no Gemini
            cues = p.read("cues.json")["cues"]
            text = " ".join(c["text"] for c in cues).lower()
            for word in ("subtitle", "interview", "timestamp", "translated"):
                self.assertIn(word, text)
            for a, b in zip(cues, cues[1:]):
                self.assertLessEqual(a["end"], b["start"] + 1e-6)
                self.assertLessEqual(a["start"], a["end"])
            words = [w for c in cues for w in c["words"]]
            self.assertTrue(all(w2["s"] >= w1["s"] for w1, w2 in zip(words, words[1:])))
            self.assertTrue(pl.export(cues, "src").startswith("1\n00:00:0"))


if __name__ == "__main__":
    unittest.main()
