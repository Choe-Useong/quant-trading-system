#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_DIR = ROOT / "data" / "kr_etfs" / "raw"
DEFAULT_MASTER_FILE = DEFAULT_RAW_DIR / "etf_master.parquet"
DEFAULT_START = "2000-01-01"
MAX_PYKRX_BULK_DATES = 250
KRX_LOGIN_PAGE = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001.cmd"
KRX_LOGIN_JSP = "https://data.krx.co.kr/contents/MDC/COMS/client/view/login.jsp?site=mdc"
KRX_LOGIN_URL = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001D1.cmd"
KRX_POSTPONE_PASSWORD_URL = (
    "https://data.krx.co.kr/contents/MDC/COMS/client/postponePasswordChange.cmd"
)
KRX_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
KRX_REQUEST_TIMEOUT_SECONDS = 15

PYKRX_OUTPUT_COLUMNS = [
    "Date",
    "Symbol",
    "Close",
    "Volume",
    "Amount",
    "NAV",
    "IndexValue",
]
# Yahoo cash distributions per share (KRW) by Yahoo's ex-date, long format.
DISTRIBUTION_FILE = "distributions_yahoo.parquet"
DISTRIBUTION_COLUMNS = ["Date", "Symbol", "Amount"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update Korean ETF raw price caches from pykrx and Yahoo Finance."
    )
    parser.add_argument("--master-file", default=str(DEFAULT_MASTER_FILE))
    parser.add_argument("--out-dir", default=str(DEFAULT_RAW_DIR))
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default="")
    parser.add_argument(
        "--symbols",
        default="",
        help="Optional comma-separated ETF symbols for a partial/sample update.",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Incrementally refresh existing caches from their latest date minus overlap.",
    )
    parser.add_argument("--overlap-days", type=int, default=10)
    parser.add_argument("--pykrx-sleep-seconds", type=float, default=0.0)
    parser.add_argument(
        "--pykrx-batch-size",
        type=int,
        default=25,
        help="Persist pykrx results after this many requested dates.",
    )
    parser.add_argument(
        "--yahoo-batch-size",
        type=int,
        default=25,
        help="Checkpoint interval for per-symbol Yahoo downloads.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from download_progress.json and already persisted checkpoint files.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print the latest saved progress status and exit.",
    )
    parser.add_argument(
        "--distributions-only",
        action="store_true",
        help=(
            "Fetch only Yahoo cash distributions from --start to --end into "
            f"{DISTRIBUTION_FILE} (replacing that window per fetched symbol); price files are not touched."
        ),
    )
    return parser.parse_args()


def _resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def _load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            __import__("os").environ.setdefault(key, value)


def _krx_headers() -> dict[str, str]:
    return {
        "User-Agent": KRX_USER_AGENT,
        "Referer": KRX_LOGIN_JSP,
        "X-Requested-With": "XMLHttpRequest",
    }


def _warmup_krx_session(session: requests.Session) -> None:
    session.get(
        KRX_LOGIN_PAGE,
        headers={"User-Agent": KRX_USER_AGENT},
        timeout=KRX_REQUEST_TIMEOUT_SECONDS,
    ).raise_for_status()
    session.get(
        KRX_LOGIN_JSP,
        headers={"User-Agent": KRX_USER_AGENT, "Referer": KRX_LOGIN_PAGE},
        timeout=KRX_REQUEST_TIMEOUT_SECONDS,
    ).raise_for_status()


def _krx_json_response(response: requests.Response, *, action: str) -> dict[str, object]:
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"KRX {action} returned a non-JSON response") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"KRX {action} returned an unexpected payload")
    return payload


def _krx_login_attempt(
    session: requests.Session,
    *,
    login_id: str,
    login_pw: str,
    skip_duplicate: bool = False,
) -> str:
    _warmup_krx_session(session)
    form = {
        "mbrNm": "",
        "telNo": "",
        "di": "",
        "certType": "",
        "mbrId": login_id,
        "pw": login_pw,
    }
    if skip_duplicate:
        form["skipDup"] = "Y"
    payload = _krx_json_response(
        session.post(
            KRX_LOGIN_URL,
            data=form,
            headers=_krx_headers(),
            timeout=KRX_REQUEST_TIMEOUT_SECONDS,
        ),
        action="login",
    )
    return str(payload.get("_error_code", ""))


def _ensure_krx_authentication() -> None:
    login_id = os.getenv("KRX_ID", "").strip()
    login_pw = os.getenv("KRX_PW", "").strip()
    if not login_id or not login_pw:
        raise RuntimeError("KRX_ID and KRX_PW must be set for the KRX ETF bulk request")

    with requests.Session() as session:
        error_code = _krx_login_attempt(
            session,
            login_id=login_id,
            login_pw=login_pw,
        )
        if error_code == "CD010":
            postpone_payload = _krx_json_response(
                session.post(
                    KRX_POSTPONE_PASSWORD_URL,
                    headers=_krx_headers(),
                    timeout=KRX_REQUEST_TIMEOUT_SECONDS,
                ),
                action="password-change postponement",
            )
            postpone_error = str(postpone_payload.get("_error_code", ""))
            if postpone_error not in {"", "0000", "CD001"}:
                raise RuntimeError(
                    "KRX password-change postponement failed: "
                    f"error_code={postpone_error}"
                )
            print("krx_password_change_postponed=true")
            error_code = _krx_login_attempt(
                session,
                login_id=login_id,
                login_pw=login_pw,
            )

        if error_code == "CD011":
            error_code = _krx_login_attempt(
                session,
                login_id=login_id,
                login_pw=login_pw,
                skip_duplicate=True,
            )
        if error_code != "CD001":
            raise RuntimeError(f"KRX login failed: error_code={error_code or 'missing'}")
    print("krx_authentication=ok")


def _load_pykrx_stock():
    _load_env()
    _ensure_krx_authentication()
    with contextlib.redirect_stdout(io.StringIO()):
        return importlib.import_module("pykrx.stock")


def _load_yfinance():
    try:
        return importlib.import_module("yfinance")
    except ImportError as exc:
        raise ImportError(
            "yfinance is required. Install it with: py -m pip install yfinance"
        ) from exc


def _normalize_symbol(value: object) -> str:
    return str(value).strip().upper()


def _parse_symbols(raw: str) -> set[str] | None:
    values = {_normalize_symbol(item) for item in raw.split(",") if item.strip()}
    return values or None


def _load_symbols(master_file: Path, requested: set[str] | None) -> list[str]:
    if not master_file.exists():
        raise FileNotFoundError(master_file)
    master = pd.read_parquet(master_file)
    if "Symbol" not in master.columns:
        raise ValueError(f"{master_file} must contain a Symbol column")
    symbols = sorted({_normalize_symbol(value) for value in master["Symbol"].dropna()})
    if requested is not None:
        missing = sorted(requested.difference(symbols))
        if missing:
            raise ValueError(f"Requested symbols not present in master: {missing}")
        symbols = [symbol for symbol in symbols if symbol in requested]
    if not symbols:
        raise SystemExit("No ETF symbols selected")
    return symbols


def _read_existing_pykrx(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=PYKRX_OUTPUT_COLUMNS)
    frame = pd.read_parquet(path)
    if frame.empty:
        return pd.DataFrame(columns=PYKRX_OUTPUT_COLUMNS)
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.normalize()
    frame["Symbol"] = frame["Symbol"].map(_normalize_symbol)
    return frame


def _read_existing_signal(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_parquet(path)
    frame.index = pd.to_datetime(frame.index).normalize()
    frame.columns = [_normalize_symbol(column) for column in frame.columns]
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame = frame.loc[:, ~pd.Index(frame.columns).duplicated()]
    return frame.sort_index(axis=1)


def _read_existing_distributions(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=DISTRIBUTION_COLUMNS)
    return _normalize_distributions(pd.read_parquet(path))


def _normalize_distributions(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(
            {"Date": pd.Series(dtype="datetime64[ns]"), "Symbol": pd.Series(dtype=str),
             "Amount": pd.Series(dtype="float64")}
        )
    out = frame.loc[:, DISTRIBUTION_COLUMNS].copy()
    out["Date"] = pd.to_datetime(out["Date"]).dt.normalize()
    out["Symbol"] = out["Symbol"].map(_normalize_symbol)
    out["Amount"] = pd.to_numeric(out["Amount"], errors="coerce").astype("float64")
    out = out.drop_duplicates(subset=["Date", "Symbol"], keep="last")
    return out.sort_values(["Symbol", "Date"]).reset_index(drop=True)


def _merge_distributions(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
    *,
    fetched_symbols: set[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """Replace each fetched symbol's events inside [start, end] with the fetched ones.

    A symbol whose request failed or returned nothing keeps its stored events.
    """
    existing = _normalize_distributions(existing)
    replaced = existing["Symbol"].isin(fetched_symbols) & existing["Date"].between(start, end)
    parts = [frame for frame in (existing.loc[~replaced], incoming) if not frame.empty]
    if not parts:
        return _normalize_distributions(pd.DataFrame(columns=DISTRIBUTION_COLUMNS))
    return _normalize_distributions(pd.concat(parts, ignore_index=True))


def _progress_path(out_dir: Path) -> Path:
    return out_dir / "download_progress.json"


def _read_progress(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_progress(path: Path, progress: dict[str, object]) -> None:
    progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


def _validate_parquet_file(path: Path) -> None:
    pyarrow_parquet = importlib.import_module("pyarrow.parquet")
    pyarrow_parquet.ParquetFile(path).metadata


def _atomic_write_parquet(frame: pd.DataFrame, path: Path, **kwargs: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        frame.to_parquet(tmp_path, **kwargs)
        _validate_parquet_file(tmp_path)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "NA"
    seconds = max(int(seconds), 0)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _print_status(progress: dict[str, object]) -> None:
    phase = str(progress.get("phase", "unknown"))
    print(f"phase: {phase}")
    for name in ("pykrx", "yahoo"):
        section = dict(progress.get(name, {}))
        total = int(section.get("total", 0))
        if name == "pykrx" and "completed_dates" in section:
            unit = "dates"
        else:
            unit = "symbols"
        completed = len(section.get(f"completed_{unit}", []))
        remaining = max(total - completed, 0)
        failed = len(section.get(f"failed_{unit}", []))
        no_data = len(section.get(f"no_data_{unit}", []))
        eta = section.get("eta_seconds")
        print(
            f"{name}: completed {completed}/{total}, remaining {remaining}, "
            f"failed {failed}, no_data {no_data}, eta {_format_duration(eta)}"
        )


def _pykrx_fetch_dates(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    if start > end:
        return []
    return [pd.Timestamp(value).normalize() for value in pd.bdate_range(start, end)]


def _new_progress(
    *,
    mode: str,
    symbols: list[str],
    pykrx_fetch_start: pd.Timestamp,
    yahoo_fetch_start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, object]:
    pykrx_dates = _pykrx_fetch_dates(pykrx_fetch_start, end)
    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": None,
        "mode": mode,
        "phase": "pykrx",
        "symbols_requested": len(symbols),
        "fetch_window": {
            "pykrx_start": pykrx_fetch_start.date().isoformat(),
            "yahoo_start": yahoo_fetch_start.date().isoformat(),
            "end": end.date().isoformat(),
        },
        "pykrx": {
            "total": len(pykrx_dates),
            "completed_dates": [],
            "failed_dates": [],
            "no_data_dates": [],
            "elapsed_seconds": 0.0,
            "eta_seconds": None,
        },
        "yahoo": {
            "total": len(symbols),
            "completed_symbols": [],
            "failed_symbols": [],
            "no_data_symbols": [],
            "elapsed_seconds": 0.0,
            "eta_seconds": None,
        },
    }


def _validate_resume_progress(
    progress: dict[str, object],
    *,
    mode: str,
    symbols: list[str],
    pykrx_fetch_start: pd.Timestamp,
    yahoo_fetch_start: pd.Timestamp,
    end: pd.Timestamp,
) -> None:
    expected_window = {
        "pykrx_start": pykrx_fetch_start.date().isoformat(),
        "yahoo_start": yahoo_fetch_start.date().isoformat(),
        "end": end.date().isoformat(),
    }
    if progress.get("mode") != mode:
        raise ValueError("Cannot resume: progress mode differs from current run")
    if int(progress.get("symbols_requested", -1)) != len(symbols):
        raise ValueError("Cannot resume: selected symbol count differs from current run")
    if progress.get("fetch_window") != expected_window:
        raise ValueError("Cannot resume: fetch window differs from current run")
    pykrx_section = dict(progress.get("pykrx", {}))
    if "completed_dates" not in pykrx_section:
        raise ValueError(
            "Cannot resume: saved pykrx progress uses the retired per-symbol mode; "
            "run once without --resume"
        )


def _resolve_fetch_start(
    *,
    configured_start: str,
    update: bool,
    overlap_days: int,
    existing_dates: pd.Series | pd.Index,
) -> pd.Timestamp:
    start = pd.Timestamp(configured_start).normalize()
    if not update or len(existing_dates) == 0:
        return start
    existing = pd.to_datetime(existing_dates)
    last_date = existing.max().normalize()
    return max(start, last_date - pd.Timedelta(days=max(int(overlap_days), 0)))


def _resolve_fetch_end(raw: str) -> pd.Timestamp:
    if raw:
        return pd.Timestamp(raw).normalize()
    now = pd.Timestamp.now(tz="Asia/Seoul")
    end = now.tz_localize(None).normalize()
    # KRX publishes the final daytime close after the market has closed.
    if (now.hour, now.minute) < (18, 10):
        end -= pd.Timedelta(days=1)
    return end


def _normalize_pykrx_cross_section(
    frame: pd.DataFrame,
    *,
    date: pd.Timestamp,
    symbols: set[str],
) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=PYKRX_OUTPUT_COLUMNS)
    expected = {
        "\uc885\uac00": "Close",
        "\uac70\ub798\ub7c9": "Volume",
        "\uac70\ub798\ub300\uae08": "Amount",
        "NAV": "NAV",
        "\uae30\ucd08\uc9c0\uc218": "IndexValue",
    }
    missing = set(expected).difference(frame.columns)
    if missing:
        raise ValueError(f"pykrx ETF cross-section missing columns: {sorted(missing)}")
    out = frame.rename(columns=expected).reset_index()
    symbol_column = out.columns[0]
    out = out.rename(columns={symbol_column: "Symbol"})
    out["Symbol"] = out["Symbol"].map(_normalize_symbol)
    out = out[out["Symbol"].isin(symbols)].copy()
    out["Date"] = pd.Timestamp(date).normalize()
    out = out[PYKRX_OUTPUT_COLUMNS].copy()
    for column in ["Close", "Volume", "Amount", "NAV", "IndexValue"]:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    return out


def _download_pykrx(
    symbols: list[str],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    sleep_seconds: float,
    batch_size: int,
    existing: pd.DataFrame,
    output_path: Path,
    progress_path: Path,
    progress: dict[str, object],
) -> tuple[pd.DataFrame, list[str], list[str]]:
    try:
        stock = _load_pykrx_stock()
    except Exception as exc:
        raise RuntimeError(
            "Unable to initialize pykrx. Check KRX authentication or IP blocking "
            "before retrying; the updater will not fall back to per-symbol requests."
        ) from exc

    merged = existing.copy()
    section = dict(progress["pykrx"])
    completed = set(section.get("completed_dates", []))
    failed = set(section.get("failed_dates", []))
    no_data = set(section.get("no_data_dates", []))
    fetch_dates = _pykrx_fetch_dates(start, end)
    pending = [date for date in fetch_dates if date.date().isoformat() not in completed]
    symbol_set = set(symbols)
    total = len(fetch_dates)
    batch_size = max(int(batch_size), 1)
    elapsed_before = float(section.get("elapsed_seconds", 0.0))
    started_at = time.monotonic()
    parts: list[pd.DataFrame] = []

    def checkpoint() -> None:
        nonlocal merged, parts, elapsed_before, started_at, section
        if parts:
            merged = _merge_pykrx(merged, pd.concat(parts, ignore_index=True))
        else:
            merged = _merge_pykrx(merged, pd.DataFrame(columns=PYKRX_OUTPUT_COLUMNS))
        _atomic_write_parquet(merged, output_path, index=False)

        elapsed = elapsed_before + (time.monotonic() - started_at)
        completed_count = len(completed)
        eta = None
        if completed_count > 0 and completed_count < total:
            eta = elapsed / completed_count * (total - completed_count)
        section = {
            "total": total,
            "completed_dates": sorted(completed),
            "failed_dates": sorted(failed),
            "no_data_dates": sorted(no_data),
            "elapsed_seconds": elapsed,
            "eta_seconds": eta,
        }
        progress["pykrx"] = section
        _write_progress(progress_path, progress)
        print(
            f"pykrx dates: completed {completed_count}/{total}, "
            f"remaining {total - completed_count}, failed {len(failed)}, "
            f"no_data {len(no_data)}, "
            f"eta {_format_duration(eta)}"
        )
        parts = []
        elapsed_before = elapsed
        started_at = time.monotonic()

    for date in pending:
        date_key = date.date().isoformat()
        try:
            frame = stock.get_etf_ohlcv_by_ticker(date.strftime("%Y%m%d"))
            normalized = _normalize_pykrx_cross_section(
                frame,
                date=date,
                symbols=symbol_set,
            )
        except Exception as exc:
            failed.add(date_key)
            no_data.discard(date_key)
            checkpoint()
            raise RuntimeError(
                f"pykrx bulk ETF request failed for {date_key}; stopped before "
                "issuing additional KRX requests"
            ) from exc

        if normalized.empty:
            no_data.add(date_key)
        else:
            parts.append(normalized)
            no_data.discard(date_key)
        failed.discard(date_key)
        completed.add(date_key)
        if sleep_seconds > 0:
            time.sleep(float(sleep_seconds))
        if len(completed) % batch_size == 0:
            checkpoint()

    if pending or not output_path.exists():
        checkpoint()
    requested_dates = {date.date().isoformat() for date in fetch_dates}
    valid_dates = requested_dates.difference(failed).difference(no_data)
    if requested_dates and not valid_dates:
        raise RuntimeError(
            "All requested pykrx ETF business dates returned no data; "
            "authentication or the KRX endpoint is unavailable"
        )
    return merged, sorted(failed), sorted(no_data)


def _split_batches(values: list[str], size: int) -> list[list[str]]:
    size = max(int(size), 1)
    return [values[i : i + size] for i in range(0, len(values), size)]


def _extract_adj_close(data: pd.DataFrame, yahoo_tickers: list[str]) -> pd.DataFrame:
    if data.empty:
        return pd.DataFrame()
    if isinstance(data.columns, pd.MultiIndex):
        if "Adj Close" not in data.columns.get_level_values(0):
            raise ValueError("Yahoo response missing Adj Close field")
        frame = data["Adj Close"].copy()
    else:
        if "Adj Close" not in data.columns:
            raise ValueError("Yahoo response missing Adj Close field")
        frame = data[["Adj Close"]].copy()
        if len(yahoo_tickers) != 1:
            raise ValueError("Unexpected single-index Yahoo response for multiple tickers")
        frame = frame.rename(columns={"Adj Close": yahoo_tickers[0]})
    if isinstance(frame, pd.Series):
        frame = frame.to_frame()
    frame.index = pd.to_datetime(frame.index).normalize()
    frame.columns = [str(column).replace(".KS", "").upper() for column in frame.columns]
    frame = frame.apply(pd.to_numeric, errors="coerce")
    frame = frame.dropna(axis=1, how="all")
    return frame.sort_index().sort_index(axis=1)


def _yahoo_field(data: pd.DataFrame, field: str) -> pd.Series:
    """One field of a single-ticker yf.download response as a Series."""
    if isinstance(data.columns, pd.MultiIndex):
        if field not in data.columns.get_level_values(0):
            raise ValueError(f"Yahoo response missing {field} field")
        values = data[field]
        if isinstance(values, pd.DataFrame):
            if values.shape[1] != 1:
                raise ValueError(f"Unexpected multi-ticker Yahoo {field} field")
            values = values.iloc[:, 0]
    else:
        if field not in data.columns:
            raise ValueError(f"Yahoo response missing {field} field")
        values = data[field]
    values = pd.to_numeric(values, errors="coerce")
    values.index = pd.to_datetime(values.index).normalize()
    return values


def _split_yahoo_actions(data: pd.DataFrame, symbol: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Price rows (as before actions were requested) and positive cash distributions.

    With actions=True Yahoo adds a row on a distribution date that has no price row; those rows are
    dropped from the price part so the signal cache keeps the same rows as an actions=False request.
    """
    if data.empty or "Dividends" not in data.columns.get_level_values(0):
        return data, _normalize_distributions(pd.DataFrame(columns=DISTRIBUTION_COLUMNS))
    dividends = _yahoo_field(data, "Dividends")
    adj_close = _yahoo_field(data, "Adj Close")
    action_only = (adj_close.isna() & dividends.fillna(0.0).gt(0.0)).to_numpy()
    paid = dividends[dividends.gt(0.0)]
    events = pd.DataFrame({"Date": paid.index, "Symbol": symbol, "Amount": paid.to_numpy(dtype="float64")})
    return data.loc[~action_only], _normalize_distributions(events)


def _download_yahoo_history(yf, symbol: str, *, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    return yf.download(
        tickers=f"{symbol}.KS",
        start=start.strftime("%Y-%m-%d"),
        end=(end + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
        interval="1d",
        auto_adjust=False,
        actions=True,
        progress=False,
        threads=False,
        group_by="column",
    )


def _download_yahoo_distributions(
    symbols: list[str],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    batch_size: int,
    existing: pd.DataFrame,
    output_path: Path,
) -> tuple[pd.DataFrame, list[str], list[str], list[dict[str, object]]]:
    """Distributions only: same per-symbol Yahoo request as the signal download, checkpointed per batch.

    Also returns nonzero Yahoo "Capital Gains" / "Stock Splits" rows, which are not distributions
    here and are reported for review.
    """
    yf = _load_yfinance()
    merged = _normalize_distributions(existing)
    failed: list[str] = []
    no_data: list[str] = []
    other_actions: list[dict[str, object]] = []
    started_at = time.monotonic()
    done = 0
    for batch in _split_batches(symbols, batch_size):
        fetched: set[str] = set()
        parts: list[pd.DataFrame] = []
        for symbol in batch:
            try:
                data = _download_yahoo_history(yf, symbol, start=start, end=end)
                if data.empty:
                    no_data.append(symbol)
                    continue
                _, events = _split_yahoo_actions(data, symbol)
                for field in ("Capital Gains", "Stock Splits"):
                    if field in data.columns.get_level_values(0):
                        values = _yahoo_field(data, field)
                        for date, value in values[values.fillna(0.0).ne(0.0)].items():
                            other_actions.append({"symbol": symbol, "date": date.date().isoformat(),
                                                  "field": field, "value": float(value)})
            except Exception:
                failed.append(symbol)
                continue
            fetched.add(symbol)
            parts.append(events)
        incoming = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=DISTRIBUTION_COLUMNS)
        merged = _merge_distributions(merged, incoming, fetched_symbols=fetched, start=start, end=end)
        _atomic_write_parquet(merged, output_path, index=False)
        done += len(batch)
        elapsed = time.monotonic() - started_at
        eta = elapsed / done * (len(symbols) - done) if done < len(symbols) else None
        print(
            f"distributions: completed {done}/{len(symbols)}, events {len(merged)}, "
            f"failed {len(failed)}, no_data {len(no_data)}, eta {_format_duration(eta)}"
        )
    return merged, sorted(failed), sorted(no_data), other_actions


def _download_yahoo_signal(
    symbols: list[str],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    batch_size: int,
    existing: pd.DataFrame,
    output_path: Path,
    progress_path: Path,
    progress: dict[str, object],
    distributions: pd.DataFrame,
    distribution_path: Path,
) -> tuple[pd.DataFrame, list[str], list[str], pd.DataFrame]:
    yf = _load_yfinance()
    merged = existing.copy()
    merged_distributions = _normalize_distributions(distributions)
    section = dict(progress["yahoo"])
    completed = set(section.get("completed_symbols", []))
    failed = set(section.get("failed_symbols", []))
    no_data = set(section.get("no_data_symbols", []))
    pending = [symbol for symbol in symbols if symbol not in completed]
    batches = _split_batches(pending, batch_size)
    started_at = time.monotonic()
    for batch in batches:
        parts: list[pd.DataFrame] = []
        event_parts: list[pd.DataFrame] = []
        fetched: set[str] = set()
        for symbol in batch:
            yahoo_ticker = f"{symbol}.KS"
            try:
                data = _download_yahoo_history(yf, symbol, start=start, end=end)
                price_rows, events = _split_yahoo_actions(data, symbol)
                frame = _extract_adj_close(price_rows, [yahoo_ticker])
                if not data.empty:
                    fetched.add(symbol)
                    event_parts.append(events)
                if symbol in frame.columns:
                    parts.append(frame[[symbol]])
                    failed.discard(symbol)
                    no_data.discard(symbol)
                else:
                    no_data.add(symbol)
                    failed.discard(symbol)
            except Exception:
                failed.add(symbol)
                no_data.discard(symbol)
            completed.add(symbol)
        frame = pd.concat(parts, axis=1).sort_index().sort_index(axis=1) if parts else pd.DataFrame()
        if not frame.empty:
            merged = _merge_signal(merged, frame)
        else:
            merged = _merge_signal(merged, pd.DataFrame())
        _atomic_write_parquet(merged, output_path)
        incoming_events = (
            pd.concat(event_parts, ignore_index=True) if event_parts else pd.DataFrame(columns=DISTRIBUTION_COLUMNS)
        )
        merged_distributions = _merge_distributions(
            merged_distributions, incoming_events, fetched_symbols=fetched, start=start, end=end
        )
        _atomic_write_parquet(merged_distributions, distribution_path, index=False)

        elapsed = float(section.get("elapsed_seconds", 0.0)) + (time.monotonic() - started_at)
        total = len(symbols)
        completed_count = len(completed)
        eta = None
        if completed_count > 0 and completed_count < total:
            eta = elapsed / completed_count * (total - completed_count)
        section = {
            "total": total,
            "completed_symbols": sorted(completed),
            "failed_symbols": sorted(failed),
            "no_data_symbols": sorted(no_data),
            "elapsed_seconds": elapsed,
            "eta_seconds": eta,
        }
        progress["yahoo"] = section
        _write_progress(progress_path, progress)
        print(
            f"yahoo: completed {completed_count}/{total}, "
            f"remaining {total - completed_count}, failed {len(failed)}, "
            f"no_data {len(no_data)}, "
            f"eta {_format_duration(eta)}"
        )
        started_at = time.monotonic()
    return merged, sorted(failed), sorted(no_data), merged_distributions


def _merge_pykrx(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    if existing.empty:
        merged = incoming.copy()
    elif incoming.empty:
        merged = existing.copy()
    else:
        merged = pd.concat([existing, incoming], ignore_index=True)
    if merged.empty:
        return pd.DataFrame(columns=PYKRX_OUTPUT_COLUMNS)
    merged["Date"] = pd.to_datetime(merged["Date"]).dt.normalize()
    merged["Symbol"] = merged["Symbol"].map(_normalize_symbol)
    merged = merged.drop_duplicates(subset=["Date", "Symbol"], keep="last")
    return merged.sort_values(["Date", "Symbol"]).reset_index(drop=True)


def _merge_signal(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    if existing.empty and incoming.empty:
        return pd.DataFrame()

    frames: list[pd.DataFrame] = []
    for frame in (existing, incoming):
        if frame.empty:
            continue
        normalized = frame.copy()
        normalized.index = pd.to_datetime(normalized.index).normalize()
        normalized.columns = [_normalize_symbol(column) for column in normalized.columns]
        normalized = normalized[~normalized.index.duplicated(keep="last")].sort_index()
        normalized = normalized.loc[:, ~pd.Index(normalized.columns).duplicated()]
        frames.append(normalized.apply(pd.to_numeric, errors="coerce"))

    if not frames:
        return pd.DataFrame()
    if len(frames) == 1:
        merged = frames[0]
    else:
        existing_frame, incoming_frame = frames
        merged = incoming_frame.combine_first(existing_frame)
    return merged.sort_index(axis=1)


def _validate_cross_source_recency(
    pykrx_frame: pd.DataFrame,
    signal_frame: pd.DataFrame,
) -> None:
    valid_pykrx = pykrx_frame.loc[pd.to_numeric(pykrx_frame["Close"], errors="coerce").notna()]
    if valid_pykrx.empty:
        raise RuntimeError("pykrx ETF data contains no valid close prices")
    pykrx_latest = pd.Timestamp(valid_pykrx["Date"].max()).normalize()

    signal_counts = signal_frame.notna().sum(axis=1)
    minimum_coverage = max(1, int(signal_frame.shape[1] * 0.5))
    valid_signal_dates = signal_counts.index[signal_counts >= minimum_coverage]
    if len(valid_signal_dates) == 0:
        raise RuntimeError("Yahoo ETF signal data has no sufficiently populated date")
    signal_latest = pd.Timestamp(valid_signal_dates.max()).normalize()

    if pykrx_latest < signal_latest:
        raise RuntimeError(
            "KRX ETF trade data is stale relative to Yahoo signal data: "
            f"pykrx_latest={pykrx_latest.date()} signal_latest={signal_latest.date()}"
        )
    print(
        "cross_source_recency=ok "
        f"pykrx_latest={pykrx_latest.date()} signal_latest={signal_latest.date()}"
    )


def _write_report(
    out_dir: Path,
    *,
    args: argparse.Namespace,
    symbols: list[str],
    pykrx_frame: pd.DataFrame,
    signal_frame: pd.DataFrame,
    pykrx_failed_dates: list[str],
    pykrx_no_data_dates: list[str],
    yahoo_failed: list[str],
    yahoo_no_data: list[str],
    pykrx_fetch_start: pd.Timestamp,
    yahoo_fetch_start: pd.Timestamp,
    end: pd.Timestamp,
    distributions: pd.DataFrame,
) -> None:
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "update" if args.update else "rebuild",
        "symbols_requested": len(symbols),
        "pykrx_fetch_start": pykrx_fetch_start.date().isoformat(),
        "yahoo_fetch_start": yahoo_fetch_start.date().isoformat(),
        "fetch_end": end.date().isoformat(),
        "pykrx": {
            "rows": int(pykrx_frame.shape[0]),
            "symbols": int(pykrx_frame["Symbol"].nunique()) if not pykrx_frame.empty else 0,
            "start": pykrx_frame["Date"].min().date().isoformat() if not pykrx_frame.empty else None,
            "end": pykrx_frame["Date"].max().date().isoformat() if not pykrx_frame.empty else None,
            "fetch_mode": "date_cross_section",
            "failed_dates": pykrx_failed_dates,
            "no_data_dates": pykrx_no_data_dates,
        },
        "yahoo_signal": {
            "rows": int(signal_frame.shape[0]),
            "symbols": int(signal_frame.shape[1]),
            "start": signal_frame.index.min().date().isoformat() if not signal_frame.empty else None,
            "end": signal_frame.index.max().date().isoformat() if not signal_frame.empty else None,
            "failed_symbols": yahoo_failed,
            "no_data_symbols": yahoo_no_data,
        },
        "yahoo_distributions": _distribution_summary(distributions),
        "outputs": {
            "ohlcv_pykrx": str(out_dir / "ohlcv_pykrx.parquet"),
            "signal_price_yahoo": str(out_dir / "signal_price_yahoo.parquet"),
            "distributions_yahoo": str(out_dir / DISTRIBUTION_FILE),
            "download_report": str(out_dir / "download_report.json"),
        },
    }
    previous = _read_report(out_dir)
    if "distribution_backfill" in previous.get("yahoo_distributions", {}):
        report["yahoo_distributions"]["distribution_backfill"] = previous["yahoo_distributions"]["distribution_backfill"]
    (out_dir / "download_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_report(out_dir: Path) -> dict[str, object]:
    path = out_dir / "download_report.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _distribution_summary(distributions: pd.DataFrame) -> dict[str, object]:
    return {
        "events": int(len(distributions)),
        "symbols": int(distributions["Symbol"].nunique()) if not distributions.empty else 0,
        "start": distributions["Date"].min().date().isoformat() if not distributions.empty else None,
        "end": distributions["Date"].max().date().isoformat() if not distributions.empty else None,
    }


def _run_distribution_backfill(args: argparse.Namespace, symbols: list[str], out_dir: Path) -> int:
    if args.update or args.resume:
        raise SystemExit("--distributions-only fetches --start..--end; do not combine it with --update or --resume")
    start = pd.Timestamp(args.start).normalize()
    end = _resolve_fetch_end(args.end)
    if start > end:
        raise SystemExit("Fetch start is after fetch end")
    path = out_dir / DISTRIBUTION_FILE
    distributions, failed, no_data, other_actions = _download_yahoo_distributions(
        symbols,
        start=start,
        end=end,
        batch_size=int(args.yahoo_batch_size),
        existing=_read_existing_distributions(path),
        output_path=path,
    )
    report = _read_report(out_dir)
    summary = _distribution_summary(distributions)
    summary["distribution_backfill"] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "start": start.date().isoformat(),
        "end": end.date().isoformat(),
        "symbols_requested": len(symbols),
        "failed_symbols": failed,
        "no_data_symbols": no_data,
        "nonzero_capital_gains_or_splits": other_actions,
    }
    report["yahoo_distributions"] = summary
    report.setdefault("outputs", {})["distributions_yahoo"] = str(path)
    (out_dir / "download_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {path}")
    print(f"distribution events: {summary['events']} symbols: {summary['symbols']} "
          f"{summary['start']} -> {summary['end']}")
    print(f"yahoo failed: {len(failed)} no data: {len(no_data)} "
          f"nonzero capital gains/splits rows: {len(other_actions)}")
    return 1 if failed else 0


def main() -> int:
    args = parse_args()
    master_file = _resolve(args.master_file)
    out_dir = _resolve(args.out_dir)
    progress_path = _progress_path(out_dir)
    if args.status:
        progress = _read_progress(progress_path)
        if progress is None:
            raise SystemExit(f"No saved progress found: {progress_path}")
        _print_status(progress)
        return 0
    selected_symbols = _parse_symbols(args.symbols)
    symbols = _load_symbols(master_file, selected_symbols)

    out_dir.mkdir(parents=True, exist_ok=True)
    if args.distributions_only:
        return _run_distribution_backfill(args, symbols, out_dir)
    pykrx_path = out_dir / "ohlcv_pykrx.parquet"
    signal_path = out_dir / "signal_price_yahoo.parquet"
    distribution_path = out_dir / DISTRIBUTION_FILE
    if args.update and not distribution_path.exists():
        raise SystemExit(
            f"{distribution_path} is missing; an --update run only fetches recent distributions. "
            "Run once with --distributions-only to fetch the history first."
        )
    existing_distributions = _read_existing_distributions(distribution_path)

    existing_pykrx = _read_existing_pykrx(pykrx_path)
    existing_signal = _read_existing_signal(signal_path)
    pykrx_fetch_start = _resolve_fetch_start(
        configured_start=args.start,
        update=bool(args.update),
        overlap_days=int(args.overlap_days),
        existing_dates=existing_pykrx["Date"] if not existing_pykrx.empty else pd.Series(dtype="datetime64[ns]"),
    )
    yahoo_fetch_start = _resolve_fetch_start(
        configured_start=args.start,
        update=bool(args.update),
        overlap_days=int(args.overlap_days),
        existing_dates=existing_signal.index,
    )
    end = _resolve_fetch_end(args.end)
    if pykrx_fetch_start > end or yahoo_fetch_start > end:
        raise SystemExit("Fetch start is after fetch end")
    pykrx_fetch_dates = _pykrx_fetch_dates(pykrx_fetch_start, end)
    if len(pykrx_fetch_dates) > MAX_PYKRX_BULK_DATES:
        raise SystemExit(
            "Refusing a large pykrx date-bulk request "
            f"({len(pykrx_fetch_dates)} business dates; limit "
            f"{MAX_PYKRX_BULK_DATES}). Use --update with an existing raw cache or "
            "request a bounded --start/--end range. A clean historical rebuild "
            "requires a seeded cache or an official bulk data source."
        )

    mode = "update" if args.update else "rebuild"
    if args.resume:
        progress = _read_progress(progress_path)
        if progress is None:
            raise SystemExit(f"Cannot resume without saved progress: {progress_path}")
        _validate_resume_progress(
            progress,
            mode=mode,
            symbols=symbols,
            pykrx_fetch_start=pykrx_fetch_start,
            yahoo_fetch_start=yahoo_fetch_start,
            end=end,
        )
    else:
        progress = _new_progress(
            mode=mode,
            symbols=symbols,
            pykrx_fetch_start=pykrx_fetch_start,
            yahoo_fetch_start=yahoo_fetch_start,
            end=end,
        )
        _write_progress(progress_path, progress)

    pykrx_frame, pykrx_failed_dates, pykrx_no_data_dates = _download_pykrx(
        symbols,
        start=pykrx_fetch_start,
        end=end,
        sleep_seconds=float(args.pykrx_sleep_seconds),
        batch_size=int(args.pykrx_batch_size),
        existing=existing_pykrx,
        output_path=pykrx_path,
        progress_path=progress_path,
        progress=progress,
    )
    progress["phase"] = "yahoo"
    _write_progress(progress_path, progress)
    signal_frame, yahoo_failed, yahoo_no_data, distributions = _download_yahoo_signal(
        symbols,
        start=yahoo_fetch_start,
        end=end,
        batch_size=int(args.yahoo_batch_size),
        existing=existing_signal,
        output_path=signal_path,
        progress_path=progress_path,
        progress=progress,
        distributions=existing_distributions,
        distribution_path=distribution_path,
    )
    _validate_cross_source_recency(pykrx_frame, signal_frame)

    _atomic_write_parquet(pykrx_frame, pykrx_path, index=False)
    _atomic_write_parquet(signal_frame, signal_path)
    _atomic_write_parquet(distributions, distribution_path, index=False)
    progress["phase"] = "completed"
    _write_progress(progress_path, progress)
    _write_report(
        out_dir,
        args=args,
        symbols=symbols,
        pykrx_frame=pykrx_frame,
        signal_frame=signal_frame,
        pykrx_failed_dates=pykrx_failed_dates,
        pykrx_no_data_dates=pykrx_no_data_dates,
        yahoo_failed=yahoo_failed,
        yahoo_no_data=yahoo_no_data,
        pykrx_fetch_start=pykrx_fetch_start,
        yahoo_fetch_start=yahoo_fetch_start,
        end=end,
        distributions=distributions,
    )

    print(f"Updated Korean ETF raw price caches in {out_dir}")
    print(f"Symbols requested: {len(symbols)}")
    print(
        f"pykrx rows: {pykrx_frame.shape[0]} symbols: "
        f"{pykrx_frame['Symbol'].nunique() if not pykrx_frame.empty else 0}"
    )
    print(f"yahoo signal rows: {signal_frame.shape[0]} symbols: {signal_frame.shape[1]}")
    print(f"yahoo distribution events: {len(distributions)}")
    print(f"pykrx failed dates: {len(pykrx_failed_dates)}")
    print(f"pykrx no-data dates: {len(pykrx_no_data_dates)}")
    print(f"yahoo failed: {len(yahoo_failed)}")
    print(f"yahoo no data: {len(yahoo_no_data)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
