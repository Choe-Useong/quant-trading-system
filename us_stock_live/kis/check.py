#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from us_stock_live.kis.client import KISClient, KISError, load_kis_config, redacted_account


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check Korea Investment Open API connectivity without placing orders.")
    parser.add_argument("--symbol", default="QQQ", help="US ticker to query for a price check. Default: QQQ")
    parser.add_argument("--price-exchange", default="NAS", help="Quote exchange code. NAS, NYS, AMS, etc.")
    parser.add_argument("--balance-exchange", default="NASD", help="Balance exchange code. NASD, NYSE, AMEX, etc.")
    parser.add_argument("--currency", default="USD", help="Balance currency. Default: USD")
    parser.add_argument("--skip-balance", action="store_true", help="Only check token and price; do not query account balance.")
    parser.add_argument("--refresh-token", action="store_true", help="Force a new access token instead of using the local cache.")
    parser.add_argument("--raw", action="store_true", help="Print raw API payloads.")
    return parser


def _compact_price(payload: dict[str, Any]) -> dict[str, Any]:
    output = payload.get("output") or {}
    if not isinstance(output, dict):
        return {"output": output}
    return {
        "last": output.get("last") or output.get("last_price") or output.get("ovrs_nmix_prpr"),
        "base": output.get("base") or output.get("base_price"),
        "currency": output.get("curr") or output.get("tr_crcy_cd"),
        "raw_keys": sorted(output.keys())[:20],
    }


def _compact_balance(payload: dict[str, Any]) -> dict[str, Any]:
    output1 = payload.get("output1") or []
    output2 = payload.get("output2") or {}
    if not isinstance(output1, list):
        output1 = []
    if not isinstance(output2, dict):
        output2 = {}
    holdings = []
    for item in output1:
        if not isinstance(item, dict):
            continue
        holdings.append(
            {
                "symbol": item.get("ovrs_pdno"),
                "name": item.get("ovrs_item_name"),
                "qty": item.get("ovrs_cblc_qty") or item.get("ord_psbl_qty"),
                "eval_amt": item.get("ovrs_stck_evlu_amt"),
                "pnl": item.get("frcr_evlu_pfls_amt"),
            }
        )
    return {
        "holding_count": len(holdings),
        "holdings": holdings[:20],
        "summary": {
            "tot_evlu_pfls_amt": output2.get("tot_evlu_pfls_amt"),
            "ovrs_tot_pfls": output2.get("ovrs_tot_pfls"),
            "tot_pftrt": output2.get("tot_pftrt"),
            "frcr_buy_amt_smtl1": output2.get("frcr_buy_amt_smtl1"),
            "ovrs_stck_evlu_amt": output2.get("ovrs_stck_evlu_amt"),
        },
    }


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = load_kis_config()
        client = KISClient(config)
        token = client.issue_access_token(force_refresh=args.refresh_token)
        result: dict[str, Any] = {
            "env": config.env,
            "account": redacted_account(config),
            "token_ok": bool(token),
        }
        price_payload = client.overseas_price(args.symbol, exchange_code=args.price_exchange)
        result["price"] = price_payload if args.raw else _compact_price(price_payload)
        if not args.skip_balance:
            balance_payload = client.overseas_balance(
                exchange_code=args.balance_exchange,
                currency=args.currency,
            )
            result["balance"] = balance_payload if args.raw else _compact_balance(balance_payload)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KISError as exc:
        print(f"KIS check failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Unexpected KIS check failure: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
