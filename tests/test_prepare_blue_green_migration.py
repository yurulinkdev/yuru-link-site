"""Offline regression tests for the non-activating Green preparation."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from prepare_blue_green_migration import index_rows, append_chunks, prepare_plan

class GreenMigrationTests(unittest.TestCase):
    def test_duplicate_keys_abort(self):
        with self.assertRaisesRegex(ValueError, "duplicate key"):
            index_rows([["id", "value"], ["a", "one"], ["a", "two"]],
                       ["id", "value"], (0,), "tab")

    def test_headers_must_match(self):
        with self.assertRaisesRegex(ValueError, "unexpected headers"):
            index_rows([["wrong"], ["x"]], ["id"], (0,), "tab")

    def test_existing_records_are_not_reappended(self):
        fake = MagicMock()
        existing = {"book0": {"collection_requests": [
            ["CHANNEL", "a"], ["CHANNEL", "b"]
        ]}}
        with patch("prepare_blue_green_migration.read_index",
                   return_value={("CHANNEL", "a"): ["CHANNEL", "a"]}):
            with patch.dict("prepare_blue_green_migration.TABS", {
                "book0": [("collection_requests", ["target_type", "target_id"], (0, 1))]
            }, clear=True):
                result = prepare_plan(fake, {"book0": "id"}, existing)
        self.assertEqual(result["book0"]["collection_requests"]["add"], [["CHANNEL", "b"]])
        self.assertEqual(result["book0"]["collection_requests"]["expected"], 2)

    def test_append_single_chunk(self):
        fake = MagicMock()
        append_chunks(fake, "id", "tab", [["a", "value"]], 2, (0,), ["id", "value"])
        self.assertEqual(fake.values.return_value.append.call_count, 1)

    def test_ambiguous_error_after_commit_does_not_append_again(self):
        from googleapiclient.errors import HttpError
        from httplib2 import Response
        fake = MagicMock()
        fake.values.return_value.append.return_value.execute.side_effect = HttpError(
            Response({"status": 429}), b"quota exceeded"
        )
        with patch("prepare_blue_green_migration.read_index",
                   return_value={("a",): ["a", "value"]}):
            append_chunks(fake, "id", "tab", [["a", "value"]], 2, (0,), ["id", "value"])
        self.assertEqual(fake.values.return_value.append.call_count, 1)

if __name__ == "__main__":
    unittest.main()
