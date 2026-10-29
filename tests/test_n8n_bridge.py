"""scripts/services/prqa_n8n_bridge.py: n8n runs pipeline stages through an authenticated Unix-socket bridge."""
import http.client
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "services"))
import prqa_n8n_bridge as B  # noqa: E402

TOKEN = "t" * 40
FAKE_STAGE = ("import json, sys; print('env up ok', file=sys.stderr); print('GROQ api key=abc', file=sys.stderr); "
              "print(json.dumps({'stage': sys.argv[1], 'argv': sys.argv[2:]}))")


class FakeStages(B.Stages):
    def cmd(self, argv):
        return [sys.executable, "-c", FAKE_STAGE, *argv]


class UnixConn(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=30)
        self.path_ = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.path_)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.sock = str(d / "b.sock")
        self.old_log, B.LOG_DIR = B.LOG_DIR, d / "logs"
        self.srv = B.UnixHTTPServer(self.sock, B.make_handler(FakeStages(), TOKEN))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        B.LOG_DIR = self.old_log
        self.tmp.cleanup()

    def call(self, method, path, body=None, token=TOKEN):
        c = UnixConn(self.sock)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        return r.status, json.loads(r.read())

    def test_rejects_missing_or_wrong_token(self):
        self.assertEqual(self.call("GET", "/status", token=None)[0], 401)
        self.assertEqual(self.call("POST", "/stage/discover", {}, token="x" * 40)[0], 401)

    def test_stage_runs_with_validated_arguments(self):
        st, r = self.call("POST", "/stage/claim", {"pr": 7})
        self.assertEqual((st, r["stage"], r["argv"], r["exit_code"]), (200, "claim", ["--pr", "7"], 0))
        self.assertIn("env up ok", r["log_tail"])
        self.assertFalse(any("key" in l.lower() for l in r["log_tail"]))    # credential-like lines never leave the host
        st, s = self.call("GET", "/status")
        self.assertEqual(s["recent_stages"][-1]["stage"], "claim")

    def test_invalid_input_is_refused_before_anything_runs(self):
        for path, body in (("/stage/shell", {}), ("/stage/claim", {"pr": "7; rm -rf /"}), ("/stage/claim", {"pr": True}),
                           ("/stage/prepare", {"key": "../../etc/passwd"}), ("/stage/discover", [1])):
            with self.subTest(path=path, body=body):
                self.assertEqual(self.call("POST", path, body)[0], 400)
        self.assertEqual(self.call("GET", "/stage/discover")[0], 404)
        self.assertEqual(self.call("GET", "/status")[1]["recent_stages"], [])

    def test_generation_goes_to_gate_on_stdin_only(self):
        key = "Kiramido1/Amenly-Product#2@" + "a" * 40 + ":auto-v1"
        _, r = self.call("POST", "/stage/gate", {"key": key, "generation": {"generated_text": "x" * 50_000, "finish_reason": "stop"}})
        self.assertEqual(r["argv"], ["--stdin", "--key", key])           # the prompt text is never an argument
        self.assertEqual(self.call("POST", "/stage/submit", {"key": key, "generation": None})[0], 400)
        self.assertEqual(self.call("POST", "/stage/gate", {"key": key, "generation": {"generated_text": 5}})[0], 400)

    def test_promptstep_takes_the_stage_result_on_stdin_with_numeric_usage_only(self):
        key = "Kiramido1/Amenly-Product#2@" + "a" * 40 + ":auto-v1"
        _, r = self.call("POST", "/stage/promptstep", {"key": key, "generation": {"generated_text": "{}", "finish_reason": "stop",
                                                                                 "usage": {"total_tokens": 1300, "prompt_tokens": "x", "note": "y"}}})
        self.assertEqual(r["argv"], ["--stdin", "--key", key])
        sent = json.loads(B.generation_input({"generation": {"usage": {"total_tokens": 1300, "prompt_tokens": "x", "note": "y"}}}))
        self.assertEqual(sent["generation"]["usage"], {"total_tokens": 1300})          # numbers only; no text rides along
        self.assertEqual(self.call("POST", "/stage/poll", {"key": key, "generation": None})[0], 400)

    def test_reason_is_sanitized(self):
        key = "Kiramido1/Amenly-Product#2@" + "a" * 40 + ":auto-v1"
        _, r = self.call("POST", "/stage/fail", {"key": key, "reason": "timed out `$(id)` <x>"})
        self.assertEqual(r["argv"], ["--key", key, "--reason", "timed out (id) x"])


if __name__ == "__main__":
    unittest.main()
