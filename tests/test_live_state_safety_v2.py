from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kr_stock_live.trading import run_auto_rebalance as kr_auto
from live_common.state import load_state, read_json, write_json
from us_stock_live.trading import run_auto_rebalance as us_auto


class LiveStateSafetyV2Test(unittest.TestCase):
    def test_atomic_write_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            payload = {"version": 1, "events": {"event": {"status": "submitted"}}, "plans": {}}

            write_json(path, payload)

            self.assertEqual(read_json(path), payload)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_replace_failure_preserves_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            original = {"version": 1, "events": {"original": {}}, "plans": {}}
            path.write_text(json.dumps(original), encoding="utf-8")

            with patch("live_common.state.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    write_json(path, {"version": 2})

            self.assertEqual(read_json(path), original)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_missing_state_initializes_but_corrupt_state_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.assertEqual(load_state(path), {"version": 1, "events": {}, "plans": {}})

            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                load_state(path)

            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_state(path)

    def test_corrupt_auto_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auto_state.json"
            path.write_text("{broken", encoding="utf-8")

            for loader in (kr_auto._load_json, us_auto._load_json):
                with self.subTest(loader=loader.__module__):
                    with self.assertRaises(json.JSONDecodeError):
                        loader(path)


if __name__ == "__main__":
    unittest.main()
