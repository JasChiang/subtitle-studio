"""Generate short multilingual test videos with macOS text-to-speech (original scripts, no copyright issues).

    python3 tests/make_multilingual_clips.py [out_dir]     # default: uploads/testclips

Each clip: a title card (drawn by bin/text-render) + the spoken script. The scripts are the ground truth used
to measure recognition accuracy."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIPS = {
    "zh-TW_two-speakers": {"locale": "zh-TW", "target": "English", "title": ["多語言測試：繁體中文", "Two speakers"],
        "turns": [("Flo (Chinese (Taiwan))", "大家好，歡迎收看今天的節目。我們要介紹一個字幕工具，它會先用蘋果的語音辨識，替每一個字加上時間碼。"),
                  ("Eddy (Chinese (Taiwan))", "聽起來很方便。那翻譯的部分呢？"),
                  ("Flo (Chinese (Taiwan))", "翻譯交給另一個模型，它會看完整段影片，再把字幕翻成英文。")]},
    "ja-JP": {"locale": "ja-JP", "target": "繁體中文（台灣）", "title": ["多言語テスト：日本語", "Japanese"],
        "turns": [("Flo (Japanese (Japan))", "こんにちは。今日は字幕ツールについて紹介します。まず音声を文字に起こして、それから翻訳します。")]},
    "ko-KR": {"locale": "ko-KR", "target": "繁體中文（台灣）", "title": ["다국어 테스트: 한국어", "Korean"],
        "turns": [("Flo (Korean (South Korea))", "안녕하세요. 오늘은 자막 도구를 소개하겠습니다. 먼저 음성을 텍스트로 바꾸고, 그 다음에 번역합니다.")]},
    "es-ES": {"locale": "es-ES", "target": "繁體中文（台灣）", "title": ["Prueba multilingüe: español", "Spanish"],
        "turns": [("Flo (Spanish (Spain))", "Hola a todos. Hoy vamos a probar una herramienta de subtítulos. Primero reconoce la voz y después traduce el texto al chino.")]},
    "zh-TW_code-switching": {"locale": "zh-TW", "target": "English", "title": ["中英夾雜測試", "Code-switching"],
        "turns": [("Eddy (Chinese (Taiwan))", "這次我們用 Gemini 做 video understanding，再把 subtitle 翻成英文，最後用 FFmpeg 輸出影片。")]},
}


def make(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, c in CLIPS.items():
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            parts = []
            for k, (voice, text) in enumerate(c["turns"]):
                a = tmp / f"t{k}.aiff"
                subprocess.run(["say", "-v", voice, "-o", str(a), text], check=True)
                parts.append(a)
            lst = tmp / "list.txt"
            lst.write_text("".join(f"file '{a}'\n" for a in parts))
            audio = tmp / "speech.m4a"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                            "-af", "apad=pad_dur=1", "-c:a", "aac", "-b:a", "128k", str(audio)], check=True)
            card = tmp / "card.png"
            subprocess.run([str(ROOT / "bin" / "text-render")], input=json.dumps({"width": 1920, "height": 1080, "items": [
                {"out": str(card), "kind": "card", "lines": c["title"]}]}).encode(), check=True, capture_output=True)
            video = out / f"{name}.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x2a2f36:s=1920x1080:r=30",
                            "-i", str(card), "-i", str(audio), "-filter_complex", "[0][1]overlay,format=yuv420p[v]",
                            "-map", "[v]", "-map", "2:a", "-shortest", "-c:v", "libx264", "-preset", "veryfast",
                            "-c:a", "copy", str(video)], check=True)
        manifest[name] = {"file": str(video), "locale": c["locale"], "target": c["target"],
                          "script": "".join(t for _, t in c["turns"]) if c["locale"][:2] in ("zh", "ja")
                          else " ".join(t for _, t in c["turns"])}
        print("made", video)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    make(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "uploads" / "testclips")
