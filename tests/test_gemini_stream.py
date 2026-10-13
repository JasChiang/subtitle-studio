"""Gemini client against a fake local server: streamed answers, dropped streams, network errors. No network."""
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from studio import gemini

ANSWER = '{"items":[{"id":1,"tr":"一"},{"id":2,"tr":"二"},{"id":3,"tr":"三"}]}'


class Fake(BaseHTTPRequestHandler):
    plan: list[str] = []  # what each successive request does: ok | break | drop
    calls = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        mode = Fake.plan[min(Fake.calls, len(Fake.plan) - 1)]
        Fake.calls += 1
        if mode == "drop":  # connection dies before anything is sent
            self.connection.close()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        pieces = [ANSWER[:30], ANSWER[30:55], ANSWER[55:]]
        for k, piece in enumerate(pieces):
            if mode == "break" and k == 2:
                self.wfile.flush()
                self.connection.close()  # cut off mid-answer, no final chunk
                return
            ev = {"candidates": [{"content": {"parts": [{"text": piece}]}}]}
            if k == len(pieces) - 1:
                ev["candidates"][0]["finishReason"] = "STOP"
                ev["usageMetadata"] = {"promptTokenCount": 10, "candidatesTokenCount": 20}
            self.wfile.write(f"data: {json.dumps(ev)}\r\n\r\n".encode())
            self.wfile.flush()


class Stream(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.saved = (gemini.BASE, gemini.time.sleep, os.environ.get("GEMINI_API_KEY"))
        gemini.BASE = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        gemini.time.sleep = lambda s: None
        os.environ["GEMINI_API_KEY"] = "test"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        gemini.BASE, gemini.time.sleep, key = cls.saved
        if key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = key

    def run_plan(self, plan, **kw):
        Fake.plan, Fake.calls = plan, 0
        seen = []
        text, model = gemini.generate("m", [{"text": "hi"}], on_text=seen.append, log=lambda *_: None, **kw)
        return text, seen

    def test_streamed_answer_reports_progress_and_usage(self):
        usage = gemini.Usage()
        text, seen = self.run_plan(["ok"], usage=usage)
        self.assertEqual(text, ANSWER)
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(ANSWER.startswith(s) for s in seen))
        self.assertEqual(usage.summary()["models"]["m"]["in"], 10)

    def test_broken_stream_is_retried_from_scratch(self):
        text, _ = self.run_plan(["break", "ok"])
        self.assertEqual(text, ANSWER)
        self.assertEqual(Fake.calls, 2)

    def test_dropped_connection_is_retried(self):
        text, _ = self.run_plan(["drop", "drop", "ok"])
        self.assertEqual(text, ANSWER)

    def test_resume_hands_back_the_partial_answer(self):
        with self.assertRaises(gemini.StreamBroken) as cm:
            self.run_plan(["break"], resume=True)
        self.assertEqual(Fake.calls, 1)  # no blind retry: the caller asks only for what is missing
        self.assertEqual(cm.exception.partial, ANSWER[:55])  # what arrived before the cut
        self.assertEqual([x["id"] for x in gemini.partial_items(cm.exception.partial, "items")], [1, 2])

    def test_network_error_is_a_transient_status(self):
        status, _, body = gemini._request("http://127.0.0.1:9/", data=b"x", method="POST", timeout=2)
        self.assertEqual(status, 0)
        self.assertIn(0, gemini.TRANSIENT)


class PartialItems(unittest.TestCase):
    def test_keeps_only_complete_objects(self):
        t = '{"segments":[{"id":"s1","text":"a \\"quoted\\" {brace}"},{"id":"s2","text":"b"},{"id":"s3","te'
        self.assertEqual([x["id"] for x in gemini.partial_items(t, "segments")], ["s1", "s2"])
        self.assertEqual(gemini.partial_items(t, "segments")[0]["text"], 'a "quoted" {brace}')

    def test_nothing_yet(self):
        self.assertEqual(gemini.partial_items('{"items":[', "items"), [])
        self.assertEqual(gemini.partial_items("", "items"), [])


if __name__ == "__main__":
    unittest.main()
