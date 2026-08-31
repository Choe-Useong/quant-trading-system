from __future__ import annotations

import http.client
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterator


BASE_URL = "https://api.upbit.com"
DEFAULT_PAUSE_SECONDS = 0.12
DEFAULT_BATCH_SIZE = 200
DEFAULT_REQUEST_RETRIES = 8
DEFAULT_RETRY_BACKOFF_SECONDS = 1.5


@dataclass(frozen=True)
class Market:
    market: str
    korean_name: str
    english_name: str
    market_warning: str


@dataclass(frozen=True)
class CandleRow:
    market: str
    korean_name: str
    english_name: str
    market_warning: str
    date_utc: str
    date_kst: str
    opening_price: float
    high_price: float
    low_price: float
    trade_price: float
    candle_acc_trade_volume: float
    candle_acc_trade_price: float
    timestamp: int | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def request_json(path: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
    query = ""
    if params:
        query = "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{BASE_URL}{path}{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": "coin-project-upbit-collector/1.0",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, DEFAULT_REQUEST_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {408, 429, 500, 502, 503, 504} or attempt >= DEFAULT_REQUEST_RETRIES:
                raise
        except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.RemoteDisconnected) as exc:
            last_error = exc
            if attempt >= DEFAULT_REQUEST_RETRIES:
                raise
        time.sleep(DEFAULT_RETRY_BACKOFF_SECONDS * attempt)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Request failed unexpectedly for {path}")


def list_markets(quote: str = "KRW", include_warnings: bool = True) -> list[Market]:
    payload = request_json("/v1/market/all", {"isDetails": "true"})
    prefix = f"{quote.upper()}-"
    markets: list[Market] = []
    for item in payload:
        market_code = str(item["market"])
        if not market_code.startswith(prefix):
            continue
        if not include_warnings and str(item.get("market_warning", "NONE")) != "NONE":
            continue
        markets.append(
            Market(
                market=market_code,
                korean_name=str(item.get("korean_name", "")),
                english_name=str(item.get("english_name", "")),
                market_warning=str(item.get("market_warning", "NONE")),
            )
        )
    return sorted(markets, key=lambda item: item.market)


def fetch_daily_candle_batch(
    market: Market,
    count: int,
    to: str | None = None,
) -> list[CandleRow]:
    params: dict[str, object] = {"market": market.market, "count": count}
    if to:
        params["to"] = to

    try:
        payload = request_json("/v1/candles/days", params)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{market.market}: HTTP {exc.code}") from exc

    rows = [
        CandleRow(
            market=market.market,
            korean_name=market.korean_name,
            english_name=market.english_name,
            market_warning=market.market_warning,
            date_utc=str(item["candle_date_time_utc"]),
            date_kst=str(item["candle_date_time_kst"]),
            opening_price=float(item["opening_price"]),
            high_price=float(item["high_price"]),
            low_price=float(item["low_price"]),
            trade_price=float(item["trade_price"]),
            candle_acc_trade_volume=float(item["candle_acc_trade_volume"]),
            candle_acc_trade_price=float(item["candle_acc_trade_price"]),
            timestamp=None if item.get("timestamp") is None else int(item["timestamp"]),
        )
        for item in payload
    ]
    rows.sort(key=lambda row: row.date_utc)
    return rows


def fetch_minute_candle_batch(
    market: Market,
    unit: int,
    count: int,
    to: str | None = None,
) -> list[CandleRow]:
    params: dict[str, object] = {"market": market.market, "count": count}
    if to:
        params["to"] = to

    try:
        payload = request_json(f"/v1/candles/minutes/{unit}", params)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{market.market}: HTTP {exc.code}") from exc

    rows = [
        CandleRow(
            market=market.market,
            korean_name=market.korean_name,
            english_name=market.english_name,
            market_warning=market.market_warning,
            date_utc=str(item["candle_date_time_utc"]),
            date_kst=str(item["candle_date_time_kst"]),
            opening_price=float(item["opening_price"]),
            high_price=float(item["high_price"]),
            low_price=float(item["low_price"]),
            trade_price=float(item["trade_price"]),
            candle_acc_trade_volume=float(item["candle_acc_trade_volume"]),
            candle_acc_trade_price=float(item["candle_acc_trade_price"]),
            timestamp=None if item.get("timestamp") is None else int(item["timestamp"]),
        )
        for item in payload
    ]
    rows.sort(key=lambda row: row.date_utc)
    return rows


def iter_daily_candle_batches(
    market: Market,
    days: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    pause_seconds: float = DEFAULT_PAUSE_SECONDS,
    to: str | None = None,
) -> Iterator[list[CandleRow]]:
    remaining = days
    cursor = to

    while remaining is None or remaining > 0:
        request_count = batch_size if remaining is None else min(batch_size, remaining)
        batch = fetch_daily_candle_batch(market=market, count=request_count, to=cursor)
        if not batch:
            break

        yield batch

        if remaining is not None:
            remaining -= len(batch)

        oldest_dt = datetime.strptime(batch[0].date_utc, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        cursor = (oldest_dt - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S")

        if len(batch) < request_count:
            break
        time.sleep(pause_seconds)


def collect_daily_candles(
    market: Market,
    days: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    pause_seconds: float = DEFAULT_PAUSE_SECONDS,
    to: str | None = None,
    ) -> list[CandleRow]:
    rows: list[CandleRow] = []
    for batch in iter_daily_candle_batches(
        market=market,
        days=days,
        batch_size=batch_size,
        pause_seconds=pause_seconds,
        to=to,
    ):
        rows.extend(batch)
    rows.sort(key=lambda row: row.date_utc)
    return rows


def iter_minute_candle_batches(
    market: Market,
    unit: int,
    candles: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    pause_seconds: float = DEFAULT_PAUSE_SECONDS,
    to: str | None = None,
) -> Iterator[list[CandleRow]]:
    remaining = candles
    cursor = to

    while remaining is None or remaining > 0:
        request_count = batch_size if remaining is None else min(batch_size, remaining)
        batch = fetch_minute_candle_batch(
            market=market,
            unit=unit,
            count=request_count,
            to=cursor,
        )
        if not batch:
            break

        yield batch

        if remaining is not None:
            remaining -= len(batch)

        oldest_dt = datetime.strptime(batch[0].date_utc, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        cursor = (oldest_dt - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S")

        if len(batch) < request_count:
            break
        time.sleep(pause_seconds)


def collect_minute_candles(
    market: Market,
    unit: int,
    candles: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    pause_seconds: float = DEFAULT_PAUSE_SECONDS,
    to: str | None = None,
) -> list[CandleRow]:
    rows: list[CandleRow] = []
    for batch in iter_minute_candle_batches(
        market=market,
        unit=unit,
        candles=candles,
        batch_size=batch_size,
        pause_seconds=pause_seconds,
        to=to,
    ):
        rows.extend(batch)
    rows.sort(key=lambda row: row.date_utc)
    return rows
