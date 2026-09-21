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


class CliProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(WORK / "fixtures"))
        from build_fixtures import build

        cls.fx = build()

    def test_preview_search_fetch_pipeline(self) -> None:
        preview_proc = run_cli(
            ["preview", "--path", self.fx["long_log"], "--max-chars", "240", "--max-lines", "8"]
        )
        self.assertEqual(preview_proc.returncode, 0, preview_proc.stderr)
        preview = json.loads(preview_proc.stdout)
        self.assertTrue(preview["ok"])
        self.assertIn("MARKER-START", preview["excerpt"])
        self.assertNotIn("MARKER-MID", preview["excerpt"])

        search_proc = run_cli(
            ["search", "--path", self.fx["long_log"], "--query", "MARKER-END unique-ccc-7999"]
        )
        self.assertEqual(search_proc.returncode, 0, search_proc.stderr)
        search = json.loads(search_proc.stdout)
        self.assertEqual(search["matches_returned"], 1)
        hit = search["matches"][0]
        identity = json.dumps(search["identity"], separators=(",", ":"))

        fetch_proc = run_cli(
            [
                "fetch",
                "--path",
                self.fx["long_log"],
                "--identity-json",
                identity,
                "--byte-offset",
                str(hit["byte_offset"]),
                "--max-chars",
                "32",
            ]
        )
        self.assertEqual(fetch_proc.returncode, 0, fetch_proc.stderr)
        fetched = json.loads(fetch_proc.stdout)
        self.assertTrue(fetched["excerpt"].startswith("MARKER-END unique-ccc-7999"))
        raw = Path(self.fx["long_log"]).read_bytes()
        self.assertEqual(
            fetched["excerpt"],
            raw[fetched["start_byte"] : fetched["end_byte"]].decode("utf-8"),
        )

    def test_japanese_cli(self) -> None:
        proc = run_cli(["preview", "--path", self.fx["japanese"], "--max-chars", "60"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertIn("日本語", data["excerpt"])

    def test_error_exit_code(self) -> None:
        proc = run_cli(["preview", "--path", str(WORK / "fixtures" / "missing-file.txt")])
        self.assertNotEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertEqual(data["error"]["code"], "not_found")

    def test_search_continue_from_byte(self) -> None:
        first = run_cli(
            [
                "search",
                "--path",
                self.fx["long_log"],
                "--query",
                "INFO",
                "--max-matches",
                "2",
                "--chunk-size",
                "1024",
            ]
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        data = json.loads(first.stdout)
        self.assertTrue(data["truncated_matches"])
        cont = run_cli(
            [
                "search",
                "--path",
                self.fx["long_log"],
                "--query",
                "INFO",
                "--max-matches",
                "2",
                "--from-byte",
                str(data["next_search_byte"]),
                "--identity-json",
                json.dumps(data["identity"], separators=(",", ":")),
            ]
        )
        self.assertEqual(cont.returncode, 0, cont.stderr)
        more = json.loads(cont.stdout)
        self.assertGreaterEqual(more["matches_returned"], 1)
        self.assertGreater(more["matches"][0]["byte_offset"], data["matches"][-1]["byte_offset"])

    def test_tiny_output_budget_exits_nonzero(self) -> None:
        proc = run_cli(
            [
                "preview",
                "--path",
                self.fx["quotes"],
                "--max-chars",
                "2000",
                "--max-output-bytes",
                "256",
            ]
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        data = json.loads(proc.stdout)
        self.assertEqual(data["error"]["code"], "output_budget")


if __name__ == "__main__":
    unittest.main()
