#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from live.execute_portfolio_v2 import main as execute_portfolio_v2_main


DEFAULT_EXECUTION_CONFIG = "configs/examples/live_portfolio_v2.example.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute preview/live orders from a v2 live execution config.")
    parser.add_argument("--mode", choices=["preview", "live"], default="preview")
    parser.add_argument("--execution-config-json", default=DEFAULT_EXECUTION_CONFIG)
    parser.add_argument("--min-order-krw", type=float, default=None)
    parser.add_argument("--ignore-unmanaged", action="store_true")
    parser.add_argument("--refresh-candles", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    execute_args = [
        "--mode",
        args.mode,
        "--execution-config-json",
        args.execution_config_json,
    ]
    if args.min_order_krw is not None:
        execute_args.extend(["--min-order-krw", str(args.min_order_krw)])
    if args.refresh_candles is not None:
        execute_args.extend(["--refresh-candles", str(args.refresh_candles)])
    if args.ignore_unmanaged:
        execute_args.append("--ignore-unmanaged")
    return int(execute_portfolio_v2_main(execute_args))


if __name__ == "__main__":
    raise SystemExit(main())
