from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WORK = ROOT.parent / "work"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(WORK / "fixtures"))

from build_fixtures import build  # noqa: E402
from ctxfilter.core import (  # noqa: E402
    MAX_SCAN_BYTES,
    Limits,
    dumps_result,
    fetch_excerpt,
    open_regular,
    preview_file,
    run_action,
    search_file,
)


class ReviewFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fx = build()

    def _assert_found(self, path: str, query: str, chunk_size: int) -> dict:
        result = search_file(path, query, Limits(chunk_size=chunk_size, max_matches=10))
        self.assertTrue(result["ok"], result)
        self.assertGreaterEqual(result["matches_returned"], 1, result)
        hit = result["matches"][0]
        self.assertIn(query, hit["excerpt"])
        return result

    def test_search_boundaries_1024_and_eof(self) -> None:
        cases = [
            ("b1024_end", "ENDCHUNK", 1024),
            ("b1024_span", "SPANMARK", 1024),
            ("b1024_at", "ATCHUNK!", 1024),
            ("b1024_after", "AFTER___", 1024),
            ("b1024_jp_end", "日本語B", 1024),
            ("b1024_jp_span", "日本語A", 1024),
            ("b1024_jp_at", "日本語C", 1024),
            ("eof_1024", "EOFMARK!", 1024),
        ]
        for key, query, chunk in cases:
            with self.subTest(key=key):
                self._assert_found(self.fx[key], query, chunk)

    def test_search_boundaries_65536(self) -> None:
        cases = [
            ("b65536_end", "END65536", 65536),
            ("b65536_span", "SPN65536", 65536),
            ("b65536_jp_span", "日本語S", 65536),
            ("eof_65536", "EOF65536", 65536),
        ]
        for key, query, chunk in cases:
            with self.subTest(key=key):
                self._assert_found(self.fx[key], query, chunk)

    def test_second_chunk_line_and_char_match_byte_fetch(self) -> None:
        path = Path(self.fx["long_log"])
        found = search_file(path.as_posix(), "MARKER-MID unique-bbb-4000", Limits(chunk_size=1024))
        self.assertEqual(found["matches_returned"], 1)
        hit = found["matches"][0]
        by_byte = fetch_excerpt(
            path.as_posix(),
            found["identity"],
            byte_offset=hit["byte_offset"],
            limits=Limits(max_chars=28, max_lines=2),
        )
        by_char = fetch_excerpt(
            path.as_posix(),
            found["identity"],
            char_offset=hit["char_offset"],
            limits=Limits(max_chars=28, max_lines=2),
        )
        by_line = fetch_excerpt(
            path.as_posix(),
            found["identity"],
            line=hit["line"],
            limits=Limits(max_chars=80, max_lines=1),
        )
        self.assertTrue(by_byte["excerpt"].startswith("MARKER-MID unique-bbb-4000"))
        self.assertTrue(by_char["excerpt"].startswith("MARKER-MID unique-bbb-4000"))
        self.assertTrue(by_line["excerpt"].startswith("MARKER-MID unique-bbb-4000"))
        raw = path.read_bytes()
        self.assertEqual(by_byte["excerpt"], raw[by_byte["start_byte"] : by_byte["end_byte"]].decode("utf-8"))

    def test_output_shrink_cursors_allow_full_reconstruct(self) -> None:
        path = Path(self.fx["quotes"])
        original = path.read_text(encoding="utf-8")
        budget = 2048
        preview = preview_file(path.as_posix(), Limits(max_chars=8000, max_lines=200))
        payload = dumps_result(preview, budget)
        self.assertLessEqual(len(payload.encode("utf-8")), budget)
        page = json.loads(payload)
        self.assertTrue(page["ok"], page)
        if page.get("output_truncated"):
            self.assertFalse(page.get("scan_complete"))
        parts = [page["excerpt"]]
        identity = page["identity"]
        guard = 0
        while page.get("truncated") or page.get("output_truncated"):
            guard += 1
            self.assertLess(guard, 400)
            nxt = page["next_byte"]
            fetched = fetch_excerpt(
                path.as_posix(),
                identity,
                byte_offset=nxt,
                limits=Limits(max_chars=8000, max_lines=200),
            )
            payload = dumps_result(fetched, budget)
            self.assertLessEqual(len(payload.encode("utf-8")), budget)
            page = json.loads(payload)
            self.assertTrue(page["ok"], page)
            self.assertEqual(page["start_byte"], nxt)
            self.assertEqual(page["next_byte"], page["start_byte"] + len(page["excerpt"].encode("utf-8")))
            self.assertTrue(
                page["next_byte"] > nxt or page.get("stop_reason") == "eof",
                "shrunk cursor must not skip or stall before eof",
            )
            if page.get("output_truncated"):
                self.assertFalse(page.get("scan_complete"))
            parts.append(page["excerpt"])
            if page.get("stop_reason") == "eof" and not page.get("excerpt"):
                break
            if page.get("stop_reason") == "eof" and not (page.get("truncated") and page.get("excerpt")):
                if not page.get("truncated"):
                    break
        reconstructed = "".join(parts)
        self.assertEqual(reconstructed, original)

    def test_too_small_output_budget_is_error(self) -> None:
        preview = preview_file(self.fx["quotes"], Limits(max_chars=8000, max_lines=200))
        payload = dumps_result(preview, 256)
        parsed = json.loads(payload)
        self.assertFalse(parsed.get("ok"))
        self.assertEqual(parsed["error"]["code"], "output_budget")
        self.assertIn("max_output_bytes", parsed["error"]["message"])

    def test_search_continuation_past_scan_and_match_caps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "late.txt"
            path.write_bytes(b"n" * 5000 + b"LATEHIT" + b"n" * 500)
            first = search_file(
                str(path),
                "LATEHIT",
                Limits(max_scan_bytes=1024, chunk_size=256, max_matches=20),
            )
            self.assertTrue(first["ok"])
            self.assertFalse(first["scan_complete"])
            self.assertEqual(first["matches_returned"], 0)
            self.assertLess(first["next_search_byte"], path.stat().st_size)
            self.assertNotEqual(first["bytes_scanned"], first["next_search_byte"])
            cont = search_file(
                str(path),
                "LATEHIT",
                Limits(max_scan_bytes=8192, chunk_size=256, max_matches=20),
                from_byte=first["next_search_byte"],
                identity=first["identity"],
            )
            self.assertGreaterEqual(cont["matches_returned"], 1)
            self.assertIn("LATEHIT", cont["matches"][0]["excerpt"])

        capped = search_file(self.fx["long_log"], "INFO", Limits(max_matches=2, chunk_size=1024))
        self.assertTrue(capped["truncated_matches"])
        self.assertFalse(capped["scan_complete"])
        more = search_file(
            self.fx["long_log"],
            "INFO",
            Limits(max_matches=2, chunk_size=1024),
            from_byte=capped["next_search_byte"],
            identity=capped["identity"],
        )
        self.assertGreaterEqual(more["matches_returned"], 1)
        self.assertGreater(more["matches"][0]["byte_offset"], capped["matches"][-1]["byte_offset"])

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cont.txt"
            target.write_text("version-one LATEHIT\n", encoding="utf-8")
            initial = search_file(str(target), "LATEHIT")
            target.write_text("version-two LATEHIT\n", encoding="utf-8")
            stale = run_action(
                "search",
                path=str(target),
                query="LATEHIT",
                from_byte=initial["next_search_byte"],
                identity=initial["identity"],
            )
            self.assertFalse(stale["ok"])
            self.assertEqual(stale["error"]["code"], "stale_identity")

    def test_search_continuation_past_default_8mib(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "after8mib.bin"
            marker = b"AFTER8MIB"
            with path.open("wb") as handle:
                handle.write(b"a" * MAX_SCAN_BYTES)
                handle.write(marker)
            first = search_file(str(path), "AFTER8MIB")
            self.assertTrue(first["ok"])
            self.assertFalse(first["scan_complete"])
            self.assertEqual(first["matches_returned"], 0)
            self.assertLessEqual(first["bytes_scanned"], MAX_SCAN_BYTES)
            self.assertLess(first["next_search_byte"], path.stat().st_size)
            cont = search_file(
                str(path),
                "AFTER8MIB",
                from_byte=first["next_search_byte"],
                identity=first["identity"],
            )
            self.assertGreaterEqual(cont["matches_returned"], 1)
            self.assertEqual(cont["matches"][0]["byte_offset"], MAX_SCAN_BYTES)
            self.assertIn("AFTER8MIB", cont["matches"][0]["excerpt"])

    def test_match_excerpt_contains_query_at_edges(self) -> None:
        mixed = search_file(self.fx["mixed"], "日本語TAIL")
        self.assertIn("日本語TAIL", mixed["matches"][0]["excerpt"])
        start = search_file(self.fx["mixed"], "HEAD")
        self.assertIn("HEAD", start["matches"][0]["excerpt"])
        end = search_file(self.fx["mixed"], "end")
        self.assertIn("end", end["matches"][0]["excerpt"])
        jp = search_file(self.fx["japanese"], "検索対象。")
        self.assertIn("検索対象。", jp["matches"][0]["excerpt"])

    def test_invalid_utf8_is_rejected(self) -> None:
        result = run_action("preview", path=self.fx["invalid_utf8"], limits=Limits(max_chars=80, max_lines=10))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_utf8")
        found = search_file(self.fx["invalid_utf8"], "TAIL-ASCII")
        self.assertGreaterEqual(found["matches_returned"], 1)
        from_start = run_action(
            "fetch",
            path=self.fx["invalid_utf8"],
            identity=found["identity"],
            byte_offset=0,
            limits=Limits(max_chars=20, max_lines=2),
        )
        self.assertEqual(from_start["error"]["code"], "invalid_utf8")

    def test_fetch_to_eof_and_empty_eof_stop(self) -> None:
        path = Path(self.fx["japanese"])
        preview = preview_file(path.as_posix(), Limits(max_chars=30, max_lines=2))
        self.assertTrue(preview["truncated"])
        self.assertFalse(preview["scan_complete"])
        tail = fetch_excerpt(
            path.as_posix(),
            preview["identity"],
            byte_offset=preview["next_byte"],
            limits=Limits(max_chars=8000, max_lines=200),
        )
        self.assertTrue(tail["ok"])
        self.assertEqual(tail["stop_reason"], "eof")
        self.assertFalse(tail["truncated"])
        self.assertTrue(tail["scan_complete"])
        self.assertEqual(tail["next_byte"], path.stat().st_size)
        empty = fetch_excerpt(
            path.as_posix(),
            preview["identity"],
            byte_offset=tail["next_byte"],
            limits=Limits(max_chars=40, max_lines=5),
        )
        self.assertTrue(empty["ok"])
        self.assertEqual(empty["excerpt"], "")
        self.assertEqual(empty["stop_reason"], "eof")
        self.assertFalse(empty["truncated"])
        self.assertTrue(empty["scan_complete"])
        self.assertEqual(empty["next_byte"], tail["next_byte"])
        pages = 0
        cursor = preview["next_byte"]
        complete = False
        while pages < 20 and not complete:
            page = fetch_excerpt(
                path.as_posix(),
                preview["identity"],
                byte_offset=cursor,
                limits=Limits(max_chars=8000, max_lines=200),
            )
            pages += 1
            complete = bool(page["scan_complete"] or (not page["truncated"] and page["stop_reason"] == "eof"))
            if page["next_byte"] == cursor and complete:
                break
            cursor = page["next_byte"]
        self.assertTrue(complete)
        self.assertLess(pages, 20)

    def test_fifo_open_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / "pipe"
            os.mkfifo(fifo)
            started = time.monotonic()
            try:
                open_regular(fifo)
                self.fail("fifo should not open as a regular file")
            except Exception as exc:
                self.assertEqual(getattr(exc, "code", None), "not_a_regular_file")
            self.assertLess(time.monotonic() - started, 1.0)
            preview = run_action("preview", path=str(fifo))
            self.assertEqual(preview["error"]["code"], "not_a_regular_file")
            self.assertLess(time.monotonic() - started, 1.0)

    def test_missing_after_path_does_not_crash(self) -> None:
        missing = run_action("preview", path=str(WORK / "fixtures" / "missing-now.txt"))
        self.assertEqual(missing["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
