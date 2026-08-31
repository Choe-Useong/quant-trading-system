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

from kr_stock_live.kis.client import KRKISClient, load_kr_kis_config
from live_common.kis_base import redacted_account


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check Korean-stock KIS balance/price/orderable endpoints.")
    parser.add_argument("--account-profile", default="default", help="Account profile suffix: default or isa.")
    parser.add_argument("--symbol", default="005930", help="Domestic stock code for price/orderable checks.")
    parser.add_argument("--skip-balance", action="store_true")
    parser.add_argument("--skip-price", action="store_true")
    parser.add_argument("--skip-orderable", action="store_true")
    parser.add_argument("--format", choices=["summary", "json"], default="summary")
    return parser


def _to_float(value: Any) -> float:
    try:
        text = str(value).replace(",", "").strip()
        return float(text) if text else 0.0
    except (TypeError, ValueError):
        return 0.0


def _summary(payload: dict[str, Any]) -> dict[str, Any]:
    balance = payload.get("balance") or {}
    output1 = balance.get("output1") or []
    output2 = balance.get("output2") or []
    holdings = [
        {
            "symbol": str(item.get("pdno") or ""),
            "name": str(item.get("prdt_name") or ""),
            "qty": _to_float(item.get("hldg_qty")),
            "orderable_qty": _to_float(item.get("ord_psbl_qty")),
            "price": _to_float(item.get("prpr")),
            "value": _to_float(item.get("evlu_amt")),
        }
        for item in output1
        if isinstance(item, dict)
    ]
    account = output2[0] if isinstance(output2, list) and output2 else {}
    price_output = (payload.get("price") or {}).get("output") or {}
    orderable_output = (payload.get("orderable") or {}).get("output") or {}
    return {
        "account": payload["account"],
        "holdings_count": len(holdings),
        "holdings": holdings,
        "cash_krw": _to_float(account.get("dnca_tot_amt")),
        "total_eval_krw": _to_float(account.get("tot_evlu_amt")),
        "symbol": payload["symbol"],
        "price_krw": _to_float(price_output.get("stck_prpr")),
        "orderable_cash_krw": _to_float(orderable_output.get("nrcvb_buy_amt")),
        "orderable_qty": _to_float(orderable_output.get("nrcvb_buy_qty")),
    }


def main() -> int:
    args = build_parser().parse_args()
    config = load_kr_kis_config(account_profile=args.account_profile)
    client = KRKISClient(config)
    client.issue_access_token()

    payload: dict[str, Any] = {
        "account": redacted_account(config),
        "account_profile": args.account_profile,
        "symbol": args.symbol,
    }
    if not args.skip_balance:
        payload["balance"] = client.domestic_balance()
    if not args.skip_price:
        payload["price"] = client.domestic_price(args.symbol)
    if not args.skip_orderable:
        price = ((payload.get("price") or {}).get("output") or {}).get("stck_prpr") or "1"
        payload["orderable"] = client.domestic_orderable_amount(symbol=args.symbol, price=price)

    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(_summary(payload), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
