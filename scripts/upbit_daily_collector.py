#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lib.storage import write_candles_csv, write_market_manifest
from lib.upbit_collector import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_PAUSE_SECONDS,
    EXCHANGE_BASE_URLS,
    collect_daily_candles,
    list_markets,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download Upbit (or Bithumb) daily candles and save them as per-market CSV files."
    )
    parser.add_argument(
        "--exchange",
        choices=sorted(EXCHANGE_BASE_URLS),
        default="upbit",
        help="Exchange with the Upbit-style /v1 API. Bithumb daily candles start at 00:00 KST (15:00 UTC)",
    )
    parser.add_argument(
        "--quote",
        default="KRW",
        help="Quote currency prefix used to filter markets",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Maximum number of daily candles per market; omit to collect as far back as possible",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Maximum candles requested per API call",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=DEFAULT_PAUSE_SECONDS,
        help="Delay between API calls for one market",
    )
    parser.add_argument(
        "--exclude-warnings",
        action="store_true",
        help="Exclude markets currently marked with investment warnings",
    )
    parser.add_argument(
        "--markets",
        default="",
        help="Comma-separated subset of markets to download, for example KRW-BTC,KRW-ETH",
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=None,
        help="Limit the number of markets after filtering",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Base output directory (default data/<exchange>)",
    )
    parser.add_argument(
        "--new-markets-only",
        action="store_true",
        help=(
            "Download only markets without a saved daily file holding at least one candle, with their full "
            "history; saved files are left as they are. A market with no candle yet gets no file and is "
            "retried on the next run. Files are always rewritten whole, so --days with a saved directory "
            "would replace each market's history and its first candle (the listing day)."
        ),
    )
    return parser


def has_candles(path: Path) -> bool:
    """True when the daily CSV exists and holds at least one data row."""
    if not path.exists():
        return False
    with path.open("r", encoding="utf-8-sig") as handle:
        return sum(1 for _ in zip(range(2), handle)) >= 2


def collect(
    exchange: str,
    out_dir: Path,
    *,
    quote: str = "KRW",
    days: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    pause_seconds: float = DEFAULT_PAUSE_SECONDS,
    include_warnings: bool = True,
    markets_filter: set[str] | None = None,
    max_markets: int | None = None,
    new_markets_only: bool = False,
    log=print,
) -> dict[str, list[str]]:
    """Save per-market daily candles under out_dir/daily and the market list as out_dir/markets.csv.

    Returns the markets listed, downloaded (file written) and waiting (new market without a candle yet).
    """
    if new_markets_only and days is not None:
        raise ValueError("--new-markets-only downloads full histories; do not combine it with --days")
    base_url = EXCHANGE_BASE_URLS[exchange]
    daily_dir = out_dir / "daily"
    markets = list_markets(quote=quote, include_warnings=include_warnings, base_url=base_url)
    if markets_filter:
        markets = [market for market in markets if market.market in markets_filter]
    if max_markets is not None:
        markets = markets[:max_markets]

    write_market_manifest(out_dir / "markets.csv", markets)
    log(f"Found {len(markets)} {quote.upper()} markets")
    if new_markets_only:
        markets = [market for market in markets if not has_candles(daily_dir / f"{market.market}.csv")]
        log(f"New markets without saved candles: {len(markets)}")

    downloaded, waiting = [], []
    for idx, market in enumerate(markets, start=1):
        candles = collect_daily_candles(
            market=market,
            days=days,
            batch_size=batch_size,
            pause_seconds=pause_seconds,
            base_url=base_url,
        )
        if new_markets_only and not candles:
            waiting.append(market.market)
            log(f"[{idx}/{len(markets)}] {market.market}: no candle yet")
            continue
        row_count = write_candles_csv(daily_dir / f"{market.market}.csv", candles)
        downloaded.append(market.market)
        log(f"[{idx}/{len(markets)}] {market.market}: saved {row_count} rows")
    return {"downloaded": downloaded, "waiting": waiting}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    allowed = {market.strip().upper() for market in args.markets.split(",") if market.strip()} or None
    collect(
        args.exchange,
        Path(args.out_dir or f"data/{args.exchange}"),
        quote=args.quote,
        days=args.days,
        batch_size=args.batch_size,
        pause_seconds=args.pause_seconds,
        include_warnings=not args.exclude_warnings,
        markets_filter=allowed,
        max_markets=args.max_markets,
        new_markets_only=args.new_markets_only,
    )


if __name__ == "__main__":
    main()
