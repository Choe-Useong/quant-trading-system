#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from kr_stock_live.trading.state import load_state, mark_plan_executed, state_path_for_profile, write_json
from live_common.target_weights import _resolve_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mark a saved KR live rebalance plan as executed after fill confirmation.")
    parser.add_argument("--plan-json", required=True)
    parser.add_argument("--state-json", default="")
    parser.add_argument("--note", default="")
    parser.add_argument("--confirm", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.confirm:
        print("Refusing to mark executed without --confirm", file=sys.stderr)
        return 2
    plan_path = _resolve_path(args.plan_json)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    profile_json = _resolve_path(payload["profile_json"])
    state_path = _resolve_path(args.state_json) if args.state_json else state_path_for_profile(profile_json)
    state = load_state(state_path)
    state = mark_plan_executed(state, payload, execution_note=args.note)
    write_json(state_path, state)
    print(
        json.dumps(
            {
                "state_json": str(state_path.relative_to(ROOT_DIR) if state_path.is_relative_to(ROOT_DIR) else state_path),
                "plan_id": payload.get("plan_id"),
                "last_executed_change_timestamp": state.get("last_executed_change_timestamp"),
                "last_executed_plan_id": state.get("last_executed_plan_id"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
