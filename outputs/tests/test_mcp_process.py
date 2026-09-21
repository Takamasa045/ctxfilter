from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin" / "ctxfilter"
WORK = ROOT.parent / "work"
PYTHON = sys.executable


def encode_message(payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return body.encode("utf-8") + b"\n"


def read_message(proc: subprocess.Popen[bytes]) -> dict:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if line == b"":
        raise EOFError("MCP server closed stdout")
    if len(line) > 1_000_000:
        raise RuntimeError("MCP line too large")
    return json.loads(line.decode("utf-8"))


class McpProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(WORK / "fixtures"))
        from build_fixtures import build

        cls.fx = build()

    def _start(self) -> subprocess.Popen[bytes]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.Popen(
            [PYTHON, "-u", str(LAUNCHER), "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def test_initialize_list_call_and_ping_after_parse_error(self) -> None:
        proc = self._start()
        try:
            assert proc.stdin is not None
            proc.stdin.write(
                encode_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "ctxfilter-test", "version": "0"},
                        },
                    }
                )
            )
            proc.stdin.flush()
            init = read_message(proc)
            self.assertEqual(init["id"], 1)
            self.assertEqual(init["result"]["serverInfo"]["name"], "ctxfilter")

            proc.stdin.write(encode_message({"jsonrpc": "2.0", "method": "notifications/initialized"}))
            proc.stdin.flush()

            proc.stdin.write(encode_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
            proc.stdin.flush()
            listed = read_message(proc)
            names = [tool["name"] for tool in listed["result"]["tools"]]
            self.assertEqual(names, ["preview_file", "search_file", "fetch_excerpt", "read_file"])

            proc.stdin.write(
                encode_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "preview_file",
                            "arguments": {"path": self.fx["japanese"], "max_chars": 80, "max_lines": 6},
                        },
                    }
                )
            )
            proc.stdin.flush()
            preview_msg = read_message(proc)
            self.assertFalse(preview_msg["result"]["isError"])
            preview = json.loads(preview_msg["result"]["content"][0]["text"])
            self.assertIn("日本語", preview["excerpt"])

            proc.stdin.write(
                encode_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "search_file",
                            "arguments": {"path": self.fx["japanese"], "query": "MARKER-JP-MID"},
                        },
                    }
                )
            )
            proc.stdin.flush()
            search = json.loads(read_message(proc)["result"]["content"][0]["text"])
            self.assertEqual(search["matches_returned"], 1)
            hit = search["matches"][0]

            proc.stdin.write(
                encode_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {
                            "name": "fetch_excerpt",
                            "arguments": {
                                "path": self.fx["japanese"],
                                "identity": search["identity"],
                                "byte_offset": hit["byte_offset"],
                                "max_chars": 24,
                            },
                        },
                    }
                )
            )
            proc.stdin.flush()
            fetched = json.loads(read_message(proc)["result"]["content"][0]["text"])
            self.assertTrue(fetched["excerpt"].startswith("MARKER-JP-MID"))

            proc.stdin.write(b"this is not json\n")
            proc.stdin.flush()
            err = read_message(proc)
            self.assertEqual(err["error"]["code"], -32700)

            proc.stdin.write(encode_message({"jsonrpc": "2.0", "id": 9, "method": "ping"}))
            proc.stdin.flush()
            ping = read_message(proc)
            self.assertEqual(ping["id"], 9)
            self.assertEqual(ping.get("result"), {})
        finally:
            if proc.stdin:
                proc.stdin.close()
            proc.kill()
            proc.wait(timeout=5)

    def test_bad_argument_types_then_ping_and_preview(self) -> None:
        proc = self._start()
        try:
            assert proc.stdin is not None
            proc.stdin.write(
                encode_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "ctxfilter-test", "version": "0"},
                        },
                    }
                )
            )
            proc.stdin.flush()
            self.assertEqual(read_message(proc)["id"], 1)

            proc.stdin.write(
                encode_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "preview_file", "arguments": {"path": [1, 2, 3]}},
                    }
                )
            )
            proc.stdin.flush()
            bad_path = read_message(proc)
            body = json.loads(bad_path["result"]["content"][0]["text"])
            self.assertTrue(bad_path["result"]["isError"])
            self.assertFalse(body.get("ok"))
            self.assertLess(len(json.dumps(bad_path)), 2000)

            surrogate_line = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "search_file",
                        "arguments": {"path": self.fx["no_match"], "query": "PLACEHOLDER"},
                    },
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ).replace("PLACEHOLDER", "\\ud800")
            proc.stdin.write(surrogate_line.encode("ascii") + b"\n")
            proc.stdin.flush()
            bad_query = read_message(proc)
            self.assertTrue(bad_query["result"]["isError"])

            proc.stdin.write(encode_message({"jsonrpc": "2.0", "id": 4, "method": "ping"}))
            proc.stdin.flush()
            ping = read_message(proc)
            self.assertEqual(ping["id"], 4)
            self.assertEqual(ping.get("result"), {})

            proc.stdin.write(
                encode_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {
                            "name": "preview_file",
                            "arguments": {"path": self.fx["japanese"], "max_chars": 40},
                        },
                    }
                )
            )
            proc.stdin.flush()
            preview = read_message(proc)
            self.assertFalse(preview["result"]["isError"])
            data = json.loads(preview["result"]["content"][0]["text"])
            self.assertIn("日本語", data["excerpt"])

            proc.stdin.write(encode_message({"jsonrpc": "2.0", "id": 6, "method": "tools/list"}))
            proc.stdin.flush()
            listed = read_message(proc)
            for tool in listed["result"]["tools"]:
                self.assertTrue(tool["annotations"]["readOnlyHint"])
                self.assertFalse(tool["annotations"]["destructiveHint"])
        finally:
            if proc.stdin:
                proc.stdin.close()
            proc.kill()
            proc.wait(timeout=5)

    def test_mcp_stdin_eof_exits_zero(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        proc = subprocess.run(
            [PYTHON, "-u", str(LAUNCHER), "mcp"],
            input=b"",
            capture_output=True,
            env=env,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(proc.stdout, b"")


if __name__ == "__main__":
    unittest.main()
