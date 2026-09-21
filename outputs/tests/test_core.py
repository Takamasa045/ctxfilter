from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WORK = ROOT.parent / "work"
sys.path.insert(0, str(SRC))

from ctxfilter.core import (  # noqa: E402
    Limits,
    dumps_result,
    fetch_excerpt,
    preview_file,
    run_action,
    search_file,
)

FIXTURE_BUILDER = WORK / "fixtures" / "build_fixtures.py"


def fixtures() -> dict[str, str]:
    sys.path.insert(0, str(WORK / "fixtures"))
    from build_fixtures import build

    return build()


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fx = fixtures()

    def test_empty_file(self) -> None:
        result = preview_file(self.fx["empty"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "empty")
        self.assertEqual(result["excerpt"], "")
        self.assertFalse(result["truncated"])
        self.assertTrue(result["scan_complete"])

    def test_preview_is_bounded_and_keeps_start_marker(self) -> None:
        path = Path(self.fx["long_log"])
        text = path.read_text(encoding="utf-8")
        result = preview_file(path.as_posix(), Limits(max_chars=400, max_lines=12))
        self.assertTrue(result["ok"])
        self.assertLess(result["returned_chars"], 500)
        self.assertTrue(result["truncated"])
        self.assertIn("MARKER-START", result["excerpt"])
        self.assertNotIn("MARKER-MID", result["excerpt"])
        self.assertNotIn("MARKER-END", result["excerpt"])
        slice_text = path.read_bytes()[result["start_byte"] : result["end_byte"]].decode("utf-8")
        self.assertEqual(result["excerpt"], slice_text)
        self.assertEqual(result["excerpt"], text[result["start_char"] : result["end_char"]])

    def test_japanese_roundtrip(self) -> None:
        path = Path(self.fx["japanese"])
        result = preview_file(path.as_posix(), Limits(max_chars=80, max_lines=10))
        self.assertTrue(result["ok"])
        self.assertIn("日本語", result["excerpt"])
        self.assertIn("MARKER-JP-START", result["excerpt"])
        slice_text = path.read_bytes()[result["start_byte"] : result["end_byte"]].decode("utf-8")
        self.assertEqual(result["excerpt"], slice_text)

    def test_search_does_not_claim_total_when_capped(self) -> None:
        path = Path(self.fx["long_log"])
        result = search_file(path.as_posix(), "INFO", Limits(max_matches=3, chunk_size=1024))
        self.assertTrue(result["ok"])
        self.assertEqual(result["matches_returned"], 3)
        self.assertTrue(result["truncated_matches"])
        self.assertFalse(result["scan_complete"])
        self.assertNotIn("total_matches", result)
        first = result["matches"][0]
        self.assertEqual(first["line"], 2)

    def test_search_no_match_and_complete_scan(self) -> None:
        result = search_file(self.fx["no_match"], "needle-that-is-absent")
        self.assertTrue(result["ok"])
        self.assertEqual(result["matches_returned"], 0)
        self.assertTrue(result["scan_complete"])
        self.assertFalse(result["truncated_matches"])

    def test_search_then_fetch_middle_marker(self) -> None:
        path = Path(self.fx["long_log"])
        found = search_file(path.as_posix(), "MARKER-MID unique-bbb-4000")
        self.assertEqual(found["matches_returned"], 1)
        hit = found["matches"][0]
        fetched = fetch_excerpt(
            path.as_posix(),
            found["identity"],
            byte_offset=hit["byte_offset"],
            limits=Limits(max_chars=40, max_lines=5),
        )
        self.assertTrue(fetched["ok"])
        self.assertTrue(fetched["excerpt"].startswith("MARKER-MID unique-bbb-4000"))
        slice_text = path.read_bytes()[fetched["start_byte"] : fetched["end_byte"]].decode("utf-8")
        self.assertEqual(fetched["excerpt"], slice_text)

    def test_huge_line_middle_without_loading_prefix(self) -> None:
        path = Path(self.fx["long_line"])
        data = path.read_bytes()
        token = b"MIDTOKEN-XYZ"
        offset = data.index(token)
        preview = preview_file(path.as_posix(), Limits(max_chars=20, max_lines=1))
        self.assertNotIn("MIDTOKEN-XYZ", preview["excerpt"])
        fetched = fetch_excerpt(
            path.as_posix(),
            preview["identity"],
            byte_offset=offset,
            limits=Limits(max_chars=12, max_lines=1),
        )
        self.assertEqual(fetched["excerpt"], "MIDTOKEN-XYZ")
        self.assertEqual(data[fetched["start_byte"] : fetched["end_byte"]], token)
        self.assertLess(fetched["bytes_scanned"], 100)

    def test_stale_identity_after_rewrite(self) -> None:
        path = Path(self.fx["changed"])
        preview = preview_file(path.as_posix())
        path.write_text("version-two-contents\n", encoding="utf-8")
        stale = run_action(
            "fetch",
            path=path.as_posix(),
            identity=preview["identity"],
            byte_offset=0,
            limits=Limits(max_chars=20, max_lines=2),
        )
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["error"]["code"], "stale_identity")
        self.assertNotIn("version-two", json.dumps(stale))

    def test_directory_and_device_and_missing(self) -> None:
        directory = run_action("preview", path=str(WORK))
        self.assertFalse(directory["ok"])
        self.assertEqual(directory["error"]["code"], "is_directory")
        missing = run_action("preview", path=str(WORK / "fixtures" / "does-not-exist.txt"))
        self.assertEqual(missing["error"]["code"], "not_found")
        device = run_action("preview", path="/dev/null")
        self.assertEqual(device["error"]["code"], "not_a_regular_file")

    def test_binary_preview(self) -> None:
        result = preview_file(self.fx["binary"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "binary")
        self.assertIn("00", result["hex"])
        self.assertEqual(result["excerpt"], "")

    def test_invalid_path_is_not_echoed_at_length(self) -> None:
        huge = "/" + ("x" * 5000)
        result = run_action("preview", path=huge)
        self.assertFalse(result["ok"])
        encoded = json.dumps(result)
        self.assertLess(len(encoded), 500)
        self.assertNotIn("x" * 200, encoded)

    def test_output_budget(self) -> None:
        path = self.fx["long_log"]
        result = preview_file(path, Limits(max_chars=2000, max_lines=80))
        payload = dumps_result(result, 800)
        self.assertLessEqual(len(payload.encode("utf-8")), 800)
        parsed = json.loads(payload)
        self.assertTrue(parsed.get("truncated") or parsed.get("output_truncated") or parsed.get("ok") is False)

    def test_fetch_by_line(self) -> None:
        path = Path(self.fx["long_log"])
        preview = preview_file(path.as_posix(), Limits(max_chars=80, max_lines=3))
        fetched = fetch_excerpt(
            path.as_posix(),
            preview["identity"],
            line=4001,
            limits=Limits(max_chars=80, max_lines=1),
        )
        self.assertTrue(fetched["ok"])
        self.assertTrue(fetched["excerpt"].startswith("MARKER-MID unique-bbb-4000"))

    def test_nul_path_rejected(self) -> None:
        result = run_action("preview", path="foo\x00bar")
        self.assertEqual(result["error"]["code"], "invalid_path")


if __name__ == "__main__":
    unittest.main()
