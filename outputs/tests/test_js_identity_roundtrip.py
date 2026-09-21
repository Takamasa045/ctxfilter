from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin" / "ctxfilter"
WORK = ROOT.parent / "work"
PYTHON = sys.executable
NODE = shutil.which("node")


def run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [PYTHON, str(LAUNCHER), *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def encode_mcp(payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return body.encode("utf-8") + b"\n"


def read_mcp(proc: subprocess.Popen[bytes]) -> dict:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if line == b"":
        raise EOFError("MCP server closed stdout")
    return json.loads(line.decode("utf-8"))


def js_roundtrip(identity: dict) -> dict:
    if not NODE:
        raise RuntimeError("node is required for JS identity roundtrip")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "identity.json"
        src.write_text(json.dumps(identity, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        proc = subprocess.run(
            [
                NODE,
                "-e",
                "const fs=require('fs'); const v=JSON.parse(fs.readFileSync(process.argv[1],'utf8')); process.stdout.write(JSON.stringify(v));",
                str(src),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


class JsIdentityRoundtripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(WORK / "fixtures"))
        from build_fixtures import build

        cls.fx = build()

    def test_identity_fields_are_decimal_strings(self) -> None:
        preview = json.loads(run_cli(["preview", "--path", self.fx["japanese"], "--max-chars", "20"]).stdout)
        ident = preview["identity"]
        for key in ("size", "mtime_ns", "inode", "dev"):
            self.assertIsInstance(ident[key], str, key)
            self.assertTrue(ident[key].isdigit(), ident[key])
        self.assertGreater(int(ident["mtime_ns"]), 2**53 - 1)

    def test_node_parse_stringify_then_cli_fetch(self) -> None:
        search = json.loads(
            run_cli(
                ["search", "--path", self.fx["long_log"], "--query", "MARKER-MID unique-bbb-4000"]
            ).stdout
        )
        self.assertEqual(search["matches_returned"], 1)
        original = search["identity"]["mtime_ns"]
        rounded = js_roundtrip({"mtime_ns": int(original)})["mtime_ns"]
        self.assertNotEqual(int(original), rounded)
        tripped = js_roundtrip(search["identity"])
        self.assertEqual(tripped["mtime_ns"], original)
        fetch = run_cli(
            [
                "fetch",
                "--path",
                self.fx["long_log"],
                "--identity-json",
                json.dumps(tripped, separators=(",", ":")),
                "--byte-offset",
                str(search["matches"][0]["byte_offset"]),
                "--max-chars",
                "32",
            ]
        )
        self.assertEqual(fetch.returncode, 0, fetch.stdout)
        data = json.loads(fetch.stdout)
        self.assertTrue(data["ok"], data)
        self.assertTrue(data["excerpt"].startswith("MARKER-MID unique-bbb-4000"))

    def test_node_parse_stringify_then_mcp_fetch(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        proc = subprocess.Popen(
            [PYTHON, "-u", str(LAUNCHER), "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            assert proc.stdin is not None
            proc.stdin.write(
                encode_mcp(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "js-roundtrip", "version": "0"},
                        },
                    }
                )
            )
            proc.stdin.flush()
            self.assertEqual(read_mcp(proc)["id"], 1)
            proc.stdin.write(
                encode_mcp(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "search_file",
                            "arguments": {
                                "path": self.fx["japanese"],
                                "query": "MARKER-JP-MID",
                            },
                        },
                    }
                )
            )
            proc.stdin.flush()
            search = json.loads(read_mcp(proc)["result"]["content"][0]["text"])
            self.assertEqual(search["matches_returned"], 1)
            tripped = js_roundtrip(search["identity"])
            self.assertEqual(tripped["mtime_ns"], search["identity"]["mtime_ns"])
            proc.stdin.write(
                encode_mcp(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "fetch_excerpt",
                            "arguments": {
                                "path": self.fx["japanese"],
                                "identity": tripped,
                                "byte_offset": search["matches"][0]["byte_offset"],
                                "max_chars": 24,
                            },
                        },
                    }
                )
            )
            proc.stdin.flush()
            fetched_msg = read_mcp(proc)
            self.assertFalse(fetched_msg["result"]["isError"], fetched_msg)
            fetched = json.loads(fetched_msg["result"]["content"][0]["text"])
            self.assertTrue(fetched["ok"], fetched)
            self.assertTrue(fetched["excerpt"].startswith("MARKER-JP-MID"))
        finally:
            if proc.stdin:
                proc.stdin.close()
            proc.kill()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
