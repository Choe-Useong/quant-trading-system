#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_DIR = ROOT / "data" / "kr_stocks" / "raw"
DEFAULT_SOURCE_CACHE_DIR = ROOT / "data" / "stocks_cache" / "kr_stock_daily"

CORP_MAP_FILE = "fundamental_corp_map.parquet"
DISCLOSURE_FILE = "fundamental_disclosures.parquet"
SUMMARY_FILE = "fundamental_summary.parquet"
STOCK_ISSUANCE_FILE = "fundamental_stock_issuance_disclosures.parquet"
STOCK_DIVIDEND_FILE = "fundamental_stock_dividends.parquet"
MARCAP_FILE = "marcap_daily.parquet"
REPORT_FILE = "fundamental_cache_report.json"

ANNUAL_REPORT_TOKEN = "\uc0ac\uc5c5\ubcf4\uace0\uc11c"
ANNUAL_REPORT_PATTERN = re.compile(
    rf"{ANNUAL_REPORT_TOKEN}\s*\((\d{{4}})[.\-](\d{{2}})\)"
)

RAW_VALUE_COLUMNS = {
    "fundamental_revenue": "enpSaleAmt",
    "fundamental_operating_profit": "enpBzopPft",
    "fundamental_net_income": "enpCrtmNpf",
    "fundamental_comprehensive_income": "iclsPalClcAmt",
    "fundamental_total_assets": "enpTastAmt",
    "fundamental_total_liabilities": "enpTdbtAmt",
    "fundamental_total_equity": "enpTcptAmt",
    "fundamental_capital_stock": "enpCptlAmt",
}

DERIVED_ABSOLUTE_EVENT_COLUMNS = [
    "fundamental_beginning_total_equity",
]

DEFAULT_EVENT_COLUMNS = [
    "fundamental_operating_margin",
    "fundamental_net_margin",
    "fundamental_roe",
    "fundamental_roa",
    "fundamental_debt_to_equity",
    "fundamental_available",
    "fundamental_statement_type",
    "fundamental_financial_year",
]

STOCK_ISSUANCE_EVENT_COLUMNS = {
    "fundamental_issued_shares": "float64",
    "fundamental_treasury_shares": "float64",
    "fundamental_outstanding_shares": "float64",
    "fundamental_treasury_share_ratio": "float32",
    "fundamental_outstanding_share_ratio": "float32",
    "fundamental_stock_issuance_available": "float32",
    "fundamental_outstanding_share_available": "float32",
}

DIVIDEND_TTM_MONTHS = 12


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build point-in-time Korean-stock fundamental wide frames. "
            "Final filing values become available from the next trading day."
        )
    )
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    parser.add_argument("--source-cache-dir", default=str(DEFAULT_SOURCE_CACHE_DIR))
    parser.add_argument(
        "--out-dir",
        default="",
        help="Output directory. Defaults to --source-cache-dir.",
    )
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument(
        "--include-absolute",
        action="store_true",
        help="Also write absolute financial-statement value frames.",
    )
    parser.add_argument(
        "--include-stock-issuance",
        action="store_true",
        help=(
            "Build point-in-time issued, treasury, and outstanding-share frames "
            "from stock-issuance disclosures."
        ),
    )
    parser.add_argument(
        "--include-dividends",
        action="store_true",
        help=(
            "Build point-in-time trailing cash-dividend amount proxy frames. "
            "Payments become available on the day after the cash payment date."
        ),
    )
    return parser.parse_args()


def _resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def _normalize_code(value: object) -> str:
    return str(value or "").strip().upper().zfill(6)


def _required_path(path: Path) -> Path:
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def _load_calendar(
    source_cache_dir: Path,
    start: str,
    end: str,
) -> tuple[pd.DatetimeIndex, pd.Index]:
    trade_price = pd.read_parquet(_required_path(source_cache_dir / "trade_price.parquet"))
    trade_price.index = pd.to_datetime(trade_price.index).normalize()
    trade_price.columns = [_normalize_code(column) for column in trade_price.columns]
    trade_price = trade_price[~trade_price.index.duplicated(keep="last")].sort_index()
    if start:
        trade_price = trade_price.loc[trade_price.index >= pd.Timestamp(start).normalize()]
    if end:
        trade_price = trade_price.loc[trade_price.index <= pd.Timestamp(end).normalize()]
    if trade_price.empty or trade_price.shape[1] == 0:
        raise ValueError("trade_price calendar is empty after filtering")
    return trade_price.index, pd.Index(trade_price.columns)


def _load_raw_frames(raw_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    corp_map = pd.read_parquet(_required_path(raw_dir / CORP_MAP_FILE))
    disclosures = pd.read_parquet(_required_path(raw_dir / DISCLOSURE_FILE))
    summary = pd.read_parquet(_required_path(raw_dir / SUMMARY_FILE))
    for frame in (corp_map, disclosures, summary):
        for column in frame.columns:
            frame[column] = frame[column].fillna("").astype(str)
    return corp_map, disclosures, summary


def _extract_period_ym(report_name: object) -> str:
    match = ANNUAL_REPORT_PATTERN.search(str(report_name or ""))
    if not match:
        return ""
    return f"{match.group(1)}{match.group(2)}"


def _select_final_annual_disclosures(disclosures: pd.DataFrame) -> pd.DataFrame:
    required = {"corp_code", "rcept_dt", "rcept_no", "report_nm"}
    missing = required - set(disclosures.columns)
    if missing:
        raise ValueError(f"Disclosure cache is missing columns: {sorted(missing)}")
    selected = disclosures.copy()
    selected["period_ym"] = selected["report_nm"].map(_extract_period_ym)
    selected["rcept_dt"] = selected["rcept_dt"].str.replace(r"\D", "", regex=True)
    selected = selected[
        selected["period_ym"].str.len().eq(6)
        & selected["rcept_dt"].str.len().eq(8)
        & selected["corp_code"].astype(bool)
    ].copy()
    if selected.empty:
        raise ValueError("No annual-report disclosures with parsable filing dates")
    selected = selected.sort_values(["corp_code", "period_ym", "rcept_dt", "rcept_no"])
    selected = selected.drop_duplicates(["corp_code", "period_ym"], keep="last")
    return selected[["corp_code", "period_ym", "rcept_dt", "rcept_no", "report_nm"]]


def _to_number(series: pd.Series) -> pd.Series:
    text = series.fillna("").astype(str).str.strip().str.replace(",", "", regex=False)
    negative = text.str.match(r"^\(.*\)$")
    text = text.str.replace(r"^\((.*)\)$", r"\1", regex=True)
    values = pd.to_numeric(text, errors="coerce")
    values.loc[negative & values.notna()] *= -1.0
    return values.astype("float64")


def _load_stock_issuance(raw_dir: Path) -> pd.DataFrame:
    frame = pd.read_parquet(_required_path(raw_dir / STOCK_ISSUANCE_FILE))
    for column in frame.columns:
        frame[column] = frame[column].fillna("").astype(str).str.strip()
    return frame


def _build_stock_issuance_events(
    *,
    stock_issuance: pd.DataFrame,
    corp_map: pd.DataFrame,
    raw_dir: Path,
    index: pd.DatetimeIndex,
    market_columns: pd.Index,
) -> tuple[pd.DataFrame, dict[str, object]]:
    required = {
        "corpSeNo",
        "bizYear",
        "rcptNo",
        "dataSno",
        "stckItmsCd",
        "stacDt",
        "maxIssuStckTcnt",
        "trsstcCnt",
        "otsstcCnt",
    }
    missing = required - set(stock_issuance.columns)
    if missing:
        raise ValueError(
            f"Stock-issuance cache is missing columns: {sorted(missing)}"
        )

    events = stock_issuance[
        ~stock_issuance["dataSno"].isin({"101", "102"})
    ].copy()
    raw_detail_rows = len(events)

    mapping = corp_map[["stock_code", "corp_code"]].copy()
    mapping["stock_code"] = mapping["stock_code"].map(_normalize_code)
    mapping["corp_code"] = mapping["corp_code"].astype(str).str.strip().str.zfill(8)
    mapping = mapping[
        mapping["stock_code"].isin(market_columns)
        & mapping["corp_code"].ne("00000000")
    ].drop_duplicates("corp_code", keep="last")
    corp_to_stock = dict(zip(mapping["corp_code"], mapping["stock_code"]))

    direct_code = events["stckItmsCd"].where(
        events["stckItmsCd"].str.len().eq(6), ""
    )
    fallback_code = events["corpSeNo"].map(corp_to_stock).fillna("")
    events["stock_code"] = direct_code.where(
        direct_code.ne(""), fallback_code
    ).map(lambda value: _normalize_code(value) if str(value).strip() else "")
    events["statement_date"] = pd.to_datetime(
        events["stacDt"], format="%Y%m%d", errors="coerce"
    )

    calendar = pd.DatetimeIndex(index).normalize().sort_values().unique()
    positions = calendar.searchsorted(events["statement_date"], side="right") - 1
    candidate_mask = (
        (positions >= 0)
        & events["statement_date"].notna()
        & events["stock_code"].isin(market_columns)
    )
    events["statement_trade_date"] = pd.NaT
    events.loc[candidate_mask, "statement_trade_date"] = calendar[
        positions[candidate_mask]
    ]
    candidate_events = events[candidate_mask].copy()
    group_keys = ["stock_code", "rcptNo", "stacDt"]
    candidate_receipts = int(
        candidate_events[group_keys].drop_duplicates().shape[0]
    )

    statement_trade_dates = sorted(
        pd.DatetimeIndex(
            candidate_events["statement_trade_date"].dropna().unique()
        ).to_pydatetime()
    )
    marcap = pd.read_parquet(
        _required_path(raw_dir / MARCAP_FILE),
        columns=["Date", "Code", "Stocks"],
        filters=[("Date", "in", statement_trade_dates)],
    )
    marcap["Date"] = pd.to_datetime(marcap["Date"]).dt.normalize()
    marcap["Code"] = marcap["Code"].map(_normalize_code)
    marcap = marcap.sort_values(["Date", "Code"]).drop_duplicates(
        ["Date", "Code"], keep="last"
    )

    candidate_events["fundamental_issued_shares"] = _to_number(
        candidate_events["maxIssuStckTcnt"]
    )
    matched = candidate_events.merge(
        marcap,
        left_on=["statement_trade_date", "stock_code"],
        right_on=["Date", "Code"],
        how="left",
        validate="many_to_one",
    )
    matched["share_count_match"] = (
        matched["fundamental_issued_shares"].notna()
        & matched["Stocks"].notna()
        & matched["fundamental_issued_shares"].eq(matched["Stocks"])
    )
    match_count = matched.groupby(group_keys, dropna=False)[
        "share_count_match"
    ].transform("sum")
    ambiguous_rows = int(
        (matched["share_count_match"] & match_count.gt(1)).sum()
    )
    selected = matched[
        matched["share_count_match"] & match_count.eq(1)
    ].copy()

    raw_treasury = _to_number(selected["trsstcCnt"])
    raw_outstanding = _to_number(selected["otsstcCnt"])
    issued = selected["fundamental_issued_shares"]
    both_consistent = (
        issued.gt(0.0)
        & raw_treasury.notna()
        & raw_outstanding.notna()
        & raw_treasury.between(0.0, issued)
        & raw_outstanding.between(0.0, issued)
        & (issued - raw_treasury).eq(raw_outstanding)
    )
    outstanding_only = (
        issued.gt(0.0)
        & raw_treasury.isna()
        & raw_outstanding.notna()
        & raw_outstanding.between(0.0, issued)
    )
    treasury_only = (
        issued.gt(0.0)
        & raw_treasury.notna()
        & raw_outstanding.isna()
        & raw_treasury.between(0.0, issued)
    )
    outstanding_valid = both_consistent | outstanding_only | treasury_only
    canonical_outstanding = pd.Series(
        np.nan, index=selected.index, dtype="float64"
    )
    canonical_outstanding.loc[both_consistent | outstanding_only] = (
        raw_outstanding.loc[both_consistent | outstanding_only]
    )
    canonical_outstanding.loc[treasury_only] = (
        issued.loc[treasury_only] - raw_treasury.loc[treasury_only]
    )
    canonical_treasury = issued - canonical_outstanding

    selected["fundamental_outstanding_shares"] = canonical_outstanding
    selected["fundamental_treasury_shares"] = canonical_treasury
    selected["fundamental_treasury_share_ratio"] = canonical_treasury.div(issued)
    selected["fundamental_outstanding_share_ratio"] = canonical_outstanding.div(
        issued
    )
    selected["fundamental_stock_issuance_available"] = 1.0
    selected["fundamental_outstanding_share_available"] = outstanding_valid.astype(
        "float64"
    )
    selected["report_date"] = pd.to_datetime(
        selected["rcptNo"].str.slice(0, 8),
        format="%Y%m%d",
        errors="coerce",
    )
    invalid_report_dates = int(selected["report_date"].isna().sum())
    selected = selected.dropna(subset=["report_date"]).copy()
    selected["activation_date"] = selected["report_date"] + pd.Timedelta(days=1)
    selected = selected.sort_values(
        [
            "activation_date",
            "stock_code",
            "statement_date",
            "rcptNo",
            "dataSno",
        ]
    ).drop_duplicates(["activation_date", "stock_code"], keep="last")

    unmatched = matched[~matched["share_count_match"]]
    diagnostics = {
        "raw_detail_rows": int(raw_detail_rows),
        "candidate_market_rows": int(len(candidate_events)),
        "candidate_receipts": candidate_receipts,
        "exact_share_match_rows": int(matched["share_count_match"].sum()),
        "ambiguous_match_rows": ambiguous_rows,
        "matched_event_rows": int(len(selected)),
        "matched_codes": int(selected["stock_code"].nunique()),
        "outstanding_valid_rows": int(
            selected["fundamental_outstanding_share_available"].sum()
        ),
        "outstanding_invalid_rows": int(
            selected["fundamental_outstanding_share_available"].eq(0.0).sum()
        ),
        "invalid_report_date_rows": invalid_report_dates,
        "matched_events_by_year": {
            str(year): int(count)
            for year, count in selected["bizYear"].value_counts().sort_index().items()
        },
        "unmatched_examples": unmatched[
            [
                "stock_code",
                "corpSeNo",
                "bizYear",
                "rcptNo",
                "stacDt",
                "maxIssuStckTcnt",
                "Stocks",
            ]
        ]
        .head(30)
        .astype(object)
        .where(lambda frame: frame.notna(), None)
        .to_dict("records"),
    }
    return selected, diagnostics


def _load_stock_dividends(raw_dir: Path) -> pd.DataFrame:
    frame = pd.read_parquet(_required_path(raw_dir / STOCK_DIVIDEND_FILE))
    for column in frame.columns:
        frame[column] = frame[column].fillna("").astype(str).str.strip()
    return frame


def _build_dividend_events(
    *,
    stock_dividends: pd.DataFrame,
    corp_map: pd.DataFrame,
    raw_dir: Path,
    index: pd.DatetimeIndex,
    market_columns: pd.Index,
) -> tuple[pd.DataFrame, pd.DataFrame, set[str], dict[str, object]]:
    required = {
        "crno",
        "isinCd",
        "stckIssuCmpyNm",
        "scrsItmsKcd",
        "dvdnBasDt",
        "cashDvdnPayDt",
        "stckDvdnRcd",
        "stckGenrDvdnAmt",
    }
    missing = required - set(stock_dividends.columns)
    if missing:
        raise ValueError(
            f"Stock-dividend cache is missing columns: {sorted(missing)}"
        )

    mapping = corp_map[["stock_code", "jurir_no"]].copy()
    mapping["stock_code"] = mapping["stock_code"].map(_normalize_code)
    mapping["jurir_no"] = (
        mapping["jurir_no"].astype(str).str.strip().str.replace(r"\D", "", regex=True)
    )
    mapping = mapping[
        mapping["stock_code"].isin(market_columns)
        & mapping["jurir_no"].str.len().eq(13)
    ].drop_duplicates("jurir_no", keep="last")
    crno_to_stock = dict(zip(mapping["jurir_no"], mapping["stock_code"]))

    common = stock_dividends[
        stock_dividends["scrsItmsKcd"].eq("0101")
    ].copy()
    isin = common["isinCd"].astype(str).str.strip().str.upper()
    direct_code = isin.str.extract(
        r"^KR7(?P<stock_code>\d{6})[A-Z0-9]{3}$",
        expand=False,
    )
    fallback_code = common["crno"].map(crno_to_stock).fillna("")
    common["stock_code"] = direct_code.where(
        direct_code.notna(), fallback_code
    ).fillna("")
    common["stock_code"] = common["stock_code"].map(
        lambda value: _normalize_code(value) if str(value).strip() else ""
    )
    mapped_common = common[common["stock_code"].isin(market_columns)].copy()
    covered_codes = set(mapped_common["stock_code"])

    amount = _to_number(mapped_common["stckGenrDvdnAmt"])
    payment_date = pd.to_datetime(
        mapped_common["cashDvdnPayDt"],
        format="%Y%m%d",
        errors="coerce",
    )
    record_date = pd.to_datetime(
        mapped_common["dvdnBasDt"],
        format="%Y%m%d",
        errors="coerce",
    )
    candidate_cash = (
        mapped_common["stckDvdnRcd"].isin({"02", "03"})
        & amount.gt(0.0)
        & record_date.notna()
    )
    payment_rows = mapped_common[candidate_cash].copy()
    payment_rows["fundamental_cash_dividend_per_share"] = amount[candidate_cash]
    payment_rows["payment_date"] = payment_date[candidate_cash]
    payment_rows["record_date"] = record_date[candidate_cash]
    payment_rows["activation_date"] = (
        payment_rows["payment_date"] + pd.Timedelta(days=1)
    )

    calendar = pd.DatetimeIndex(index).normalize().sort_values().unique()
    positions = (
        calendar.searchsorted(payment_rows["record_date"], side="right") - 1
    )
    record_date_in_range = (
        (positions >= 0)
        & payment_rows["record_date"].le(calendar.max())
    )
    payment_rows["record_trade_date"] = pd.NaT
    payment_rows.loc[record_date_in_range, "record_trade_date"] = calendar[
        positions[record_date_in_range]
    ]
    record_trade_dates = sorted(
        pd.DatetimeIndex(
            payment_rows["record_trade_date"].dropna().unique()
        ).to_pydatetime()
    )
    marcap = pd.read_parquet(
        _required_path(raw_dir / MARCAP_FILE),
        columns=["Date", "Code", "Stocks"],
        filters=[("Date", "in", record_trade_dates)],
    )
    marcap["Date"] = pd.to_datetime(marcap["Date"]).dt.normalize()
    marcap["Code"] = marcap["Code"].map(_normalize_code)
    marcap = marcap.sort_values(["Date", "Code"]).drop_duplicates(
        ["Date", "Code"], keep="last"
    )
    payment_rows = payment_rows.merge(
        marcap,
        left_on=["record_trade_date", "stock_code"],
        right_on=["Date", "Code"],
        how="left",
        validate="many_to_one",
    )
    payment_rows["record_share_count"] = pd.to_numeric(
        payment_rows["Stocks"], errors="coerce"
    )
    valid_share_count = payment_rows["record_share_count"].gt(0.0)
    usable_payment = payment_rows["payment_date"].notna()
    missing_record_share_rows = int(
        (usable_payment & ~valid_share_count).sum()
    )
    payment_rows["fundamental_cash_dividend_amount_proxy"] = (
        payment_rows["fundamental_cash_dividend_per_share"]
        * payment_rows["record_share_count"].where(valid_share_count)
    )
    events = payment_rows[usable_payment & valid_share_count].copy()
    events = (
        events.groupby(["activation_date", "stock_code"], as_index=False)
        .agg(
            fundamental_cash_dividend_amount_proxy=(
                "fundamental_cash_dividend_amount_proxy",
                "sum",
            ),
            payment_date=("payment_date", "max"),
            payment_event_count=("isinCd", "size"),
        )
        .sort_values(["activation_date", "stock_code"])
    )

    unmatched = common[~common["stock_code"].isin(market_columns)]
    diagnostics = {
        "raw_rows": int(len(stock_dividends)),
        "common_share_rows": int(len(common)),
        "mapped_common_share_rows": int(len(mapped_common)),
        "source_covered_codes": int(len(covered_codes)),
        "candidate_positive_cash_rows": int(candidate_cash.sum()),
        "usable_cash_payment_rows": int(
            (candidate_cash & payment_date.notna()).sum()
        ),
        "cash_payment_events": int(len(events)),
        "cash_payment_codes": int(events["stock_code"].nunique()),
        "missing_record_share_rows": missing_record_share_rows,
        "multi_payment_same_day_events": int(
            events["payment_event_count"].gt(1).sum()
        ),
        "payment_date_start": (
            events["payment_date"].min().date().isoformat()
            if not events.empty
            else None
        ),
        "payment_date_end": (
            events["payment_date"].max().date().isoformat()
            if not events.empty
            else None
        ),
        "unmatched_examples": unmatched[
            ["isinCd", "crno", "stckIssuCmpyNm", "dvdnBasDt"]
        ]
        .head(30)
        .to_dict("records"),
    }
    return events, payment_rows, covered_codes, diagnostics


def _build_dividend_ttm_frames(
    *,
    events: pd.DataFrame,
    covered_codes: set[str],
    index: pd.DatetimeIndex,
    columns: pd.Index,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    values = np.full((len(index), len(columns)), np.nan, dtype="float32")
    column_positions = {code: position for position, code in enumerate(columns)}
    covered_positions = [
        column_positions[code]
        for code in covered_codes
        if code in column_positions
    ]
    trade_dates = pd.DatetimeIndex(index).normalize()
    warmup_mask = trade_dates >= (
        trade_dates.min() + pd.DateOffset(months=DIVIDEND_TTM_MONTHS)
    )
    if covered_positions:
        values[np.ix_(warmup_mask, covered_positions)] = 0.0

    trade_ns = trade_dates.asi8
    cutoff_ns = (
        trade_dates - pd.DateOffset(months=DIVIDEND_TTM_MONTHS)
    ).asi8
    for stock_code, group in events.groupby("stock_code", sort=False):
        position = column_positions.get(stock_code)
        if position is None:
            continue
        grouped = (
            group.groupby("activation_date")[
                "fundamental_cash_dividend_amount_proxy"
            ]
            .sum()
            .sort_index()
        )
        event_dates_ns = pd.DatetimeIndex(grouped.index).asi8
        event_amounts = grouped.to_numpy(dtype="float64", copy=False)
        cumulative = np.concatenate(([0.0], np.cumsum(event_amounts)))
        right = np.searchsorted(event_dates_ns, trade_ns, side="right")
        left = np.searchsorted(event_dates_ns, cutoff_ns, side="right")
        rolling_values = (cumulative[right] - cumulative[left]).astype("float32")
        values[warmup_mask, position] = rolling_values[warmup_mask]

    ttm = pd.DataFrame(values, index=index, columns=columns)
    available = ttm.notna().astype("float32")
    return ttm, available


def _build_dividend_latest_fy_frames(
    *,
    payment_rows: pd.DataFrame,
    annual_events: pd.DataFrame,
    covered_codes: set[str],
    index: pd.DatetimeIndex,
    columns: pd.Index,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    required_annual = {
        "stock_code",
        "bizYear",
        "basDt",
        "activation_date",
    }
    missing_annual = required_annual - set(annual_events.columns)
    if missing_annual:
        raise ValueError(
            f"Annual fundamental events are missing columns: {sorted(missing_annual)}"
        )

    periods = annual_events[
        ["stock_code", "bizYear", "basDt", "activation_date"]
    ].copy()
    periods["stock_code"] = periods["stock_code"].map(_normalize_code)
    periods["fiscal_year"] = periods["bizYear"].astype(str).str.strip()
    periods["fiscal_end"] = pd.to_datetime(
        periods["basDt"], format="%Y%m%d", errors="coerce"
    )
    periods["report_activation_date"] = pd.to_datetime(
        periods["activation_date"], errors="coerce"
    ).dt.normalize()
    periods = periods[
        periods["stock_code"].isin(covered_codes)
        & periods["fiscal_year"].ne("")
        & periods["fiscal_end"].notna()
        & periods["report_activation_date"].notna()
    ].copy()
    periods = periods.sort_values(
        [
            "stock_code",
            "fiscal_end",
            "report_activation_date",
        ]
    ).drop_duplicates(["stock_code", "fiscal_year"], keep="last")
    periods["previous_fiscal_end"] = periods.groupby("stock_code")[
        "fiscal_end"
    ].shift()
    first_period = periods["previous_fiscal_end"].isna()
    periods.loc[first_period, "previous_fiscal_end"] = (
        periods.loc[first_period, "fiscal_end"] - pd.DateOffset(years=1)
    )

    assigned_parts: list[pd.DataFrame] = []
    assigned_rows = 0
    for stock_code, stock_payments in payment_rows.groupby(
        "stock_code", sort=False
    ):
        stock_periods = periods[
            periods["stock_code"].eq(stock_code)
        ].sort_values("fiscal_end")
        if stock_periods.empty:
            continue
        fiscal_ends = stock_periods["fiscal_end"].to_numpy(
            dtype="datetime64[ns]"
        )
        record_dates = stock_payments["record_date"].to_numpy(
            dtype="datetime64[ns]"
        )
        positions = np.searchsorted(fiscal_ends, record_dates, side="left")
        valid_positions = positions < len(stock_periods)
        if not bool(valid_positions.any()):
            continue
        selected_rows = np.flatnonzero(valid_positions)
        selected_period_positions = positions[valid_positions]
        assigned = stock_payments.iloc[selected_rows].copy()
        selected_periods = stock_periods.iloc[selected_period_positions]
        assigned["fiscal_year"] = selected_periods[
            "fiscal_year"
        ].to_numpy()
        assigned["period_start_exclusive"] = selected_periods[
            "previous_fiscal_end"
        ].to_numpy()
        within_period = assigned["record_date"].gt(
            assigned["period_start_exclusive"]
        )
        assigned = assigned[within_period].copy()
        assigned_rows += len(assigned)
        if not assigned.empty:
            assigned_parts.append(assigned)

    if assigned_parts:
        assigned_payments = pd.concat(assigned_parts, ignore_index=True)
    else:
        assigned_payments = payment_rows.iloc[0:0].copy()
        assigned_payments["fiscal_year"] = pd.Series(dtype="object")

    assigned_payments["event_complete"] = (
        assigned_payments["payment_date"].notna()
        & assigned_payments[
            "fundamental_cash_dividend_amount_proxy"
        ].notna()
    )
    if assigned_payments.empty:
        fiscal_payments = pd.DataFrame(
            columns=[
                "stock_code",
                "fiscal_year",
                "cash_event_count",
                "complete_cash_event_count",
                "fundamental_cash_dividend_latest_fy_amount_proxy",
                "last_payment_activation_date",
            ]
        )
    else:
        fiscal_payments = (
            assigned_payments.groupby(
                ["stock_code", "fiscal_year"], as_index=False
            )
            .agg(
                cash_event_count=("isinCd", "size"),
                complete_cash_event_count=("event_complete", "sum"),
                fundamental_cash_dividend_latest_fy_amount_proxy=(
                    "fundamental_cash_dividend_amount_proxy",
                    lambda values: values.sum(min_count=1),
                ),
                last_payment_activation_date=("activation_date", "max"),
            )
        )

    fiscal_events = periods.merge(
        fiscal_payments,
        on=["stock_code", "fiscal_year"],
        how="left",
        validate="one_to_one",
    )
    fiscal_events["cash_event_count"] = (
        fiscal_events["cash_event_count"].fillna(0).astype("int64")
    )
    fiscal_events["complete_cash_event_count"] = (
        fiscal_events["complete_cash_event_count"].fillna(0).astype("int64")
    )
    fiscal_events["event_complete"] = fiscal_events[
        "cash_event_count"
    ].eq(fiscal_events["complete_cash_event_count"])
    no_cash_dividend = fiscal_events["cash_event_count"].eq(0)
    fiscal_events.loc[
        no_cash_dividend,
        "fundamental_cash_dividend_latest_fy_amount_proxy",
    ] = 0.0

    positive_complete = (
        fiscal_events["event_complete"]
        & fiscal_events["cash_event_count"].gt(0)
    )
    payment_or_report = pd.concat(
        [
            fiscal_events["report_activation_date"],
            pd.to_datetime(
                fiscal_events["last_payment_activation_date"],
                errors="coerce",
            ),
        ],
        axis=1,
    ).max(axis=1)
    fiscal_events["activation_date"] = fiscal_events[
        "report_activation_date"
    ]
    fiscal_events.loc[positive_complete, "activation_date"] = (
        payment_or_report[positive_complete]
    )
    fiscal_events["fundamental_cash_dividend_latest_fy_available"] = (
        fiscal_events["event_complete"].astype("float32")
    )
    fiscal_events.loc[
        ~fiscal_events["event_complete"],
        "fundamental_cash_dividend_latest_fy_amount_proxy",
    ] = 0.0

    amount = _event_to_wide(
        fiscal_events,
        value_column="fundamental_cash_dividend_latest_fy_amount_proxy",
        index=index,
        columns=columns,
    )
    available = _event_to_wide(
        fiscal_events,
        value_column="fundamental_cash_dividend_latest_fy_available",
        index=index,
        columns=columns,
        dtype="float32",
    )
    amount = amount.where(available.eq(1.0))

    diagnostics = {
        "fiscal_period_rows": int(len(fiscal_events)),
        "fiscal_period_codes": int(fiscal_events["stock_code"].nunique()),
        "assigned_positive_cash_rows": int(assigned_rows),
        "unassigned_positive_cash_rows": int(len(payment_rows) - assigned_rows),
        "complete_fiscal_period_rows": int(
            fiscal_events["event_complete"].sum()
        ),
        "incomplete_fiscal_period_rows": int(
            (~fiscal_events["event_complete"]).sum()
        ),
        "zero_dividend_fiscal_period_rows": int(
            (fiscal_events["event_complete"] & no_cash_dividend).sum()
        ),
        "positive_dividend_fiscal_period_rows": int(
            positive_complete.sum()
        ),
    }
    return amount, available, diagnostics


def _build_latest_fy_net_share_supply_frames(
    *,
    annual_events: pd.DataFrame,
    stock_issuance_events: pd.DataFrame,
    split_adjusted_issued_shares: pd.DataFrame,
    index: pd.DatetimeIndex,
    columns: pd.Index,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    required_annual = {
        "stock_code",
        "bizYear",
        "basDt",
        "rcept_no",
        "activation_date",
    }
    missing_annual = required_annual - set(annual_events.columns)
    if missing_annual:
        raise ValueError(
            f"Annual fundamental events are missing columns: {sorted(missing_annual)}"
        )
    required_issuance = {
        "stock_code",
        "bizYear",
        "stacDt",
        "rcptNo",
        "fundamental_outstanding_share_ratio",
    }
    missing_issuance = required_issuance - set(stock_issuance_events.columns)
    if missing_issuance:
        raise ValueError(
            "Stock-issuance events are missing columns: "
            f"{sorted(missing_issuance)}"
        )

    periods = annual_events[
        [
            "stock_code",
            "bizYear",
            "basDt",
            "rcept_no",
            "activation_date",
        ]
    ].copy()
    periods["stock_code"] = periods["stock_code"].map(_normalize_code)
    periods["fiscal_year"] = pd.to_numeric(
        periods["bizYear"], errors="coerce"
    )
    periods["fiscal_end"] = pd.to_datetime(
        periods["basDt"], format="%Y%m%d", errors="coerce"
    )
    periods["activation_date"] = pd.to_datetime(
        periods["activation_date"], errors="coerce"
    ).dt.normalize()
    periods["rcept_no"] = periods["rcept_no"].astype(str).str.strip()
    periods = periods[
        periods["stock_code"].isin(columns)
        & periods["fiscal_year"].notna()
        & periods["fiscal_end"].notna()
        & periods["activation_date"].notna()
        & periods["rcept_no"].ne("")
    ].copy()
    periods = periods.sort_values(
        ["stock_code", "fiscal_end", "activation_date", "rcept_no"]
    ).drop_duplicates(["stock_code", "fiscal_year"], keep="last")

    issuance = stock_issuance_events[
        [
            "stock_code",
            "bizYear",
            "stacDt",
            "rcptNo",
            "fundamental_outstanding_share_ratio",
        ]
    ].copy()
    issuance["stock_code"] = issuance["stock_code"].map(_normalize_code)
    issuance["fiscal_year"] = pd.to_numeric(
        issuance["bizYear"], errors="coerce"
    )
    issuance["stacDt"] = (
        issuance["stacDt"].astype(str).str.replace(r"\D", "", regex=True)
    )
    issuance["rcptNo"] = issuance["rcptNo"].astype(str).str.strip()
    issuance = issuance.sort_values(
        ["stock_code", "fiscal_year", "stacDt", "rcptNo"]
    ).drop_duplicates(
        ["stock_code", "fiscal_year", "stacDt", "rcptNo"],
        keep="last",
    )
    periods = periods.merge(
        issuance.assign(exact_issuance_match=1.0),
        left_on=["stock_code", "fiscal_year", "basDt", "rcept_no"],
        right_on=["stock_code", "fiscal_year", "stacDt", "rcptNo"],
        how="left",
        validate="one_to_one",
    )

    calendar = pd.DatetimeIndex(index).normalize()
    fiscal_end_positions = (
        calendar.searchsorted(periods["fiscal_end"], side="right") - 1
    )
    column_positions = {
        str(code): position for position, code in enumerate(columns)
    }
    market_positions = periods["stock_code"].map(column_positions)
    lookup_valid = (
        periods["exact_issuance_match"].eq(1.0)
        & (fiscal_end_positions >= 0)
        & (fiscal_end_positions < len(calendar))
        & market_positions.notna()
        & periods["fundamental_outstanding_share_ratio"].gt(0.0)
        & periods["fundamental_outstanding_share_ratio"].le(1.0)
    )
    periods["split_adjusted_outstanding_shares"] = np.nan
    if bool(lookup_valid.any()):
        values = split_adjusted_issued_shares.reindex(
            index=index, columns=columns
        ).to_numpy(dtype="float64", copy=False)
        periods.loc[
            lookup_valid,
            "split_adjusted_outstanding_shares",
        ] = values[
            fiscal_end_positions[lookup_valid],
            market_positions[lookup_valid].astype("int64"),
        ] * periods.loc[
            lookup_valid,
            "fundamental_outstanding_share_ratio",
        ]

    periods = periods.sort_values(
        ["stock_code", "fiscal_end", "activation_date"]
    )
    periods["previous_fiscal_year"] = periods.groupby("stock_code")[
        "fiscal_year"
    ].shift()
    periods["previous_adjusted_outstanding_shares"] = periods.groupby(
        "stock_code"
    )["split_adjusted_outstanding_shares"].shift()
    current_shares = periods["split_adjusted_outstanding_shares"]
    previous_shares = periods["previous_adjusted_outstanding_shares"]
    consecutive_year = periods["fiscal_year"].eq(
        periods["previous_fiscal_year"] + 1.0
    )
    valid_change = (
        consecutive_year
        & current_shares.gt(0.0)
        & previous_shares.gt(0.0)
    )
    periods["fundamental_net_share_supply_latest_fy_change"] = (
        current_shares.div(previous_shares).sub(1.0)
    ).where(valid_change)
    periods["fundamental_net_share_supply_latest_fy_available"] = (
        valid_change.astype("float32")
    )
    periods["change_for_wide"] = periods[
        "fundamental_net_share_supply_latest_fy_change"
    ].fillna(0.0)

    change = _event_to_wide(
        periods,
        value_column="change_for_wide",
        index=index,
        columns=columns,
    )
    available = _event_to_wide(
        periods,
        value_column="fundamental_net_share_supply_latest_fy_available",
        index=index,
        columns=columns,
        dtype="float32",
    )
    change = change.where(available.eq(1.0))
    diagnostics = {
        "annual_period_rows": int(len(periods)),
        "exact_issuance_match_rows": int(
            periods["exact_issuance_match"].eq(1.0).sum()
        ),
        "valid_change_rows": int(valid_change.sum()),
        "invalid_change_rows": int((~valid_change).sum()),
        "valid_change_codes": int(
            periods.loc[valid_change, "stock_code"].nunique()
        ),
    }
    return change, available, diagnostics


def _select_financial_statements(summary: pd.DataFrame) -> pd.DataFrame:
    required = {"crno", "bizYear", "basDt", "fnclDcd", *RAW_VALUE_COLUMNS.values()}
    missing = required - set(summary.columns)
    if missing:
        raise ValueError(f"Financial summary cache is missing columns: {sorted(missing)}")
    selected = summary.copy()
    selected["fnclDcd"] = selected["fnclDcd"].astype(str).str.strip()
    selected = selected[selected["fnclDcd"].isin({"110", "120"})].copy()
    selected["statement_preference"] = selected["fnclDcd"].map({"110": 0, "120": 1})
    selected["basDt"] = selected["basDt"].str.replace(r"\D", "", regex=True)
    selected = selected[selected["basDt"].str.len().eq(8) & selected["crno"].astype(bool)]
    selected = selected.sort_values(
        ["crno", "bizYear", "basDt", "statement_preference"]
    ).drop_duplicates(["crno", "bizYear", "basDt"], keep="first")
    for output_column, raw_column in RAW_VALUE_COLUMNS.items():
        selected[output_column] = _to_number(selected[raw_column])
    selected["fundamental_statement_type"] = pd.to_numeric(
        selected["fnclDcd"], errors="coerce"
    )
    selected["fundamental_financial_year"] = pd.to_numeric(
        selected["bizYear"], errors="coerce"
    )
    selected["period_ym"] = selected["basDt"].str.slice(0, 6)
    return selected


def _safe_ratio(numerator: pd.Series, denominator: pd.Series, *, positive: bool) -> pd.Series:
    valid = denominator.gt(0.0) if positive else denominator.ne(0.0)
    out = numerator.div(denominator.where(valid))
    return out.replace([np.inf, -np.inf], np.nan)


def _attach_beginning_total_equity(
    events: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, int]]:
    required = {
        "stock_code",
        "fundamental_financial_year",
        "fundamental_statement_type",
        "fundamental_total_equity",
        "activation_date",
        "basDt",
    }
    missing = required - set(events.columns)
    if missing:
        raise ValueError(
            "Beginning-equity calculation is missing columns: "
            f"{sorted(missing)}"
        )

    ordered = events.copy()
    ordered["_beginning_equity_order"] = np.arange(len(ordered))
    ordered = ordered.sort_values(
        [
            "stock_code",
            "fundamental_financial_year",
            "activation_date",
            "basDt",
        ]
    )
    grouped = ordered.groupby("stock_code", sort=False)
    previous_year = grouped["fundamental_financial_year"].shift()
    previous_statement_type = grouped["fundamental_statement_type"].shift()
    previous_equity = grouped["fundamental_total_equity"].shift()
    previous_activation_date = grouped["activation_date"].shift()

    has_previous = previous_year.notna()
    consecutive_year = ordered["fundamental_financial_year"].eq(
        previous_year + 1.0
    )
    same_statement_type = ordered["fundamental_statement_type"].eq(
        previous_statement_type
    )
    available_at_activation = previous_activation_date.le(
        ordered["activation_date"]
    )
    valid = (
        consecutive_year
        & same_statement_type
        & available_at_activation
        & previous_equity.notna()
    )
    ordered["fundamental_beginning_total_equity"] = previous_equity.where(valid)

    diagnostics = {
        "valid_rows": int(valid.sum()),
        "missing_rows": int((~valid).sum()),
        "year_gap_rows": int((has_previous & ~consecutive_year).sum()),
        "statement_type_mismatch_rows": int(
            (consecutive_year & ~same_statement_type).sum()
        ),
        "previous_filing_not_yet_available_rows": int(
            (consecutive_year & same_statement_type & ~available_at_activation).sum()
        ),
        "missing_previous_equity_rows": int(
            (
                consecutive_year
                & same_statement_type
                & available_at_activation
                & previous_equity.isna()
            ).sum()
        ),
    }
    ordered = (
        ordered.sort_values("_beginning_equity_order")
        .drop(columns="_beginning_equity_order")
        .reset_index(drop=True)
    )
    return ordered, diagnostics


def _build_fundamental_events(
    corp_map: pd.DataFrame,
    disclosures: pd.DataFrame,
    summary: pd.DataFrame,
    market_columns: pd.Index,
) -> tuple[pd.DataFrame, dict[str, object]]:
    required_map = {"stock_code", "corp_code", "jurir_no"}
    missing_map = required_map - set(corp_map.columns)
    if missing_map:
        raise ValueError(f"Corporate map is missing columns: {sorted(missing_map)}")
    mapping = corp_map.copy()
    mapping["stock_code"] = mapping["stock_code"].map(_normalize_code)
    mapping = mapping[
        mapping["stock_code"].isin(market_columns)
        & mapping["jurir_no"].astype(str).str.strip().ne("")
    ].copy()
    mapping = mapping.drop_duplicates("stock_code", keep="last")

    statements = _select_financial_statements(summary)
    statements = statements.merge(
        mapping[["stock_code", "corp_code", "jurir_no"]],
        left_on="crno",
        right_on="jurir_no",
        how="inner",
        validate="many_to_one",
    )
    final_reports = _select_final_annual_disclosures(disclosures)
    events = statements.merge(
        final_reports,
        on=["corp_code", "period_ym"],
        how="left",
        validate="many_to_one",
    )
    unmatched = events[events["rcept_dt"].isna() | events["rcept_dt"].eq("")].copy()
    events = events[events["rcept_dt"].notna() & events["rcept_dt"].ne("")].copy()
    events["report_date"] = pd.to_datetime(
        events["rcept_dt"], format="%Y%m%d", errors="coerce"
    )
    events = events.dropna(subset=["report_date"])
    # OpenDART provides the filing date but not a reliable market-time timestamp.
    events["activation_date"] = events["report_date"] + pd.Timedelta(days=1)
    events, beginning_equity_diagnostics = _attach_beginning_total_equity(events)

    events["fundamental_operating_margin"] = _safe_ratio(
        events["fundamental_operating_profit"],
        events["fundamental_revenue"],
        positive=False,
    )
    events["fundamental_net_margin"] = _safe_ratio(
        events["fundamental_net_income"],
        events["fundamental_revenue"],
        positive=False,
    )
    events["fundamental_roe"] = _safe_ratio(
        events["fundamental_net_income"],
        events["fundamental_total_equity"],
        positive=True,
    )
    events["fundamental_roa"] = _safe_ratio(
        events["fundamental_net_income"],
        events["fundamental_total_assets"],
        positive=True,
    )
    events["fundamental_debt_to_equity"] = _safe_ratio(
        events["fundamental_total_liabilities"],
        events["fundamental_total_equity"],
        positive=True,
    )
    events["fundamental_available"] = 1.0
    events = events.sort_values(
        ["activation_date", "stock_code", "basDt", "rcept_dt", "rcept_no"]
    ).drop_duplicates(["activation_date", "stock_code"], keep="last")

    diagnostics = {
        "mapped_common_share_codes": int(mapping["stock_code"].nunique()),
        "selected_statement_rows": int(len(statements)),
        "matched_event_rows": int(len(events)),
        "unmatched_statement_rows": int(len(unmatched)),
        "unmatched_examples": unmatched[
            ["stock_code", "corp_code", "bizYear", "basDt", "period_ym"]
        ].head(30).to_dict("records"),
        "consolidated_rows": int(events["fnclDcd"].eq("110").sum()),
        "separate_fallback_rows": int(events["fnclDcd"].eq("120").sum()),
        "beginning_total_equity": beginning_equity_diagnostics,
    }
    return events, diagnostics


def _event_to_wide(
    events: pd.DataFrame,
    *,
    value_column: str,
    index: pd.DatetimeIndex,
    columns: pd.Index,
    dtype: str = "float32",
) -> pd.DataFrame:
    selected = events[["activation_date", "stock_code", value_column]].dropna(
        subset=[value_column]
    )
    if selected.empty:
        return pd.DataFrame(np.nan, index=index, columns=columns, dtype=dtype)
    event_frame = selected.pivot_table(
        index="activation_date",
        columns="stock_code",
        values=value_column,
        aggfunc="last",
    ).sort_index()
    combined_index = index.union(pd.DatetimeIndex(event_frame.index)).sort_values()
    wide = event_frame.reindex(combined_index).ffill().reindex(index=index, columns=columns)
    return wide.astype(dtype)


def _common_share_flag(
    corp_map: pd.DataFrame,
    *,
    index: pd.DatetimeIndex,
    columns: pd.Index,
) -> pd.DataFrame:
    common_codes = set(corp_map["stock_code"].map(_normalize_code)) & set(columns)
    values = np.zeros((len(index), len(columns)), dtype="float32")
    positions = [position for position, code in enumerate(columns) if code in common_codes]
    if positions:
        values[:, positions] = 1.0
    return pd.DataFrame(values, index=index, columns=columns)


def _write_report(
    *,
    out_dir: Path,
    raw_dir: Path,
    source_cache_dir: Path,
    index: pd.DatetimeIndex,
    columns: pd.Index,
    events: pd.DataFrame,
    diagnostics: dict[str, object],
    frame_stats: dict[str, dict[str, int]],
    stock_issuance_diagnostics: dict[str, object] | None = None,
    dividend_diagnostics: dict[str, object] | None = None,
) -> None:
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "raw_dir": str(raw_dir),
        "source_cache_dir": str(source_cache_dir),
        "out_dir": str(out_dir),
        "availability_rule": "final annual filing value from the next trading day",
        "calendar_start": index.min().date().isoformat(),
        "calendar_end": index.max().date().isoformat(),
        "calendar_rows": int(len(index)),
        "market_columns": int(len(columns)),
        "event_start": (
            events["activation_date"].min().date().isoformat() if not events.empty else None
        ),
        "event_end": (
            events["activation_date"].max().date().isoformat() if not events.empty else None
        ),
        **diagnostics,
        "frames": frame_stats,
    }
    if stock_issuance_diagnostics is not None:
        report["stock_issuance_availability_rule"] = (
            "exact issued-share match at statement date; available from the "
            "first trading day after the filing receipt date; latest fiscal-"
            "year net share supply compares consecutive annual filings using "
            "split-adjusted outstanding shares"
        )
        report["stock_issuance"] = stock_issuance_diagnostics
    if dividend_diagnostics is not None:
        report["dividend_availability_rule"] = (
            "positive common-share DPS multiplied by issued shares at the "
            "dividend record date enters the trailing 12-calendar-month "
            "amount proxy on the day after the cash payment date; the latest "
            "fiscal-year proxy replaces the prior fiscal year after both the "
            "annual filing and every positive cash payment are available"
        )
        report["stock_dividends"] = dividend_diagnostics
    (out_dir / REPORT_FILE).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    raw_dir = _resolve(args.raw_dir)
    source_cache_dir = _resolve(args.source_cache_dir)
    out_dir = _resolve(args.out_dir) if args.out_dir else source_cache_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    index, columns = _load_calendar(source_cache_dir, args.start, args.end)
    corp_map, disclosures, summary = _load_raw_frames(raw_dir)
    events, diagnostics = _build_fundamental_events(
        corp_map, disclosures, summary, columns
    )
    if events.empty:
        raise ValueError("No point-in-time fundamental events matched the source-cache universe")
    stock_issuance_events: pd.DataFrame | None = None
    stock_issuance_diagnostics: dict[str, object] | None = None
    if args.include_stock_issuance:
        stock_issuance_events, stock_issuance_diagnostics = (
            _build_stock_issuance_events(
                stock_issuance=_load_stock_issuance(raw_dir),
                corp_map=corp_map,
                raw_dir=raw_dir,
                index=index,
                market_columns=columns,
            )
        )
        if stock_issuance_events.empty:
            raise ValueError(
                "No point-in-time stock-issuance events matched the source-cache universe"
            )
    dividend_events: pd.DataFrame | None = None
    dividend_payment_rows: pd.DataFrame | None = None
    dividend_covered_codes: set[str] = set()
    dividend_diagnostics: dict[str, object] | None = None
    if args.include_dividends:
        (
            dividend_events,
            dividend_payment_rows,
            dividend_covered_codes,
            dividend_diagnostics,
        ) = _build_dividend_events(
            stock_dividends=_load_stock_dividends(raw_dir),
            corp_map=corp_map,
            raw_dir=raw_dir,
            index=index,
            market_columns=columns,
        )
        if not dividend_covered_codes:
            raise ValueError(
                "No stock-dividend source coverage matched the source-cache universe"
            )

    frame_stats: dict[str, dict[str, int]] = {}
    common_flag = _common_share_flag(corp_map, index=index, columns=columns)
    common_flag.to_parquet(out_dir / "common_share_flag.parquet")
    frame_stats["common_share_flag"] = {
        "rows": int(common_flag.shape[0]),
        "columns": int(common_flag.shape[1]),
        "non_null": int(common_flag.notna().sum().sum()),
    }
    print(f"wrote: {out_dir / 'common_share_flag.parquet'}")

    value_columns = list(DEFAULT_EVENT_COLUMNS)
    if args.include_absolute:
        value_columns.extend(RAW_VALUE_COLUMNS)
        value_columns.extend(DERIVED_ABSOLUTE_EVENT_COLUMNS)
    for value_column in value_columns:
        frame = _event_to_wide(
            events,
            value_column=value_column,
            index=index,
            columns=columns,
        )
        frame.to_parquet(out_dir / f"{value_column}.parquet")
        frame_stats[value_column] = {
            "rows": int(frame.shape[0]),
            "columns": int(frame.shape[1]),
            "non_null": int(frame.notna().sum().sum()),
        }
        print(f"wrote: {out_dir / f'{value_column}.parquet'}")

    if stock_issuance_events is not None:
        outstanding_available = _event_to_wide(
            stock_issuance_events,
            value_column="fundamental_outstanding_share_available",
            index=index,
            columns=columns,
            dtype="float32",
        )
        outstanding_available_path = (
            out_dir / "fundamental_outstanding_share_available.parquet"
        )
        outstanding_available.to_parquet(outstanding_available_path)
        frame_stats["fundamental_outstanding_share_available"] = {
            "rows": int(outstanding_available.shape[0]),
            "columns": int(outstanding_available.shape[1]),
            "non_null": int(outstanding_available.notna().sum().sum()),
        }
        print(f"wrote: {outstanding_available_path}")

        dependent_columns = {
            "fundamental_treasury_shares",
            "fundamental_outstanding_shares",
            "fundamental_treasury_share_ratio",
            "fundamental_outstanding_share_ratio",
        }
        outstanding_ratio: pd.DataFrame | None = None
        for value_column, dtype in STOCK_ISSUANCE_EVENT_COLUMNS.items():
            if value_column == "fundamental_outstanding_share_available":
                continue
            frame = _event_to_wide(
                stock_issuance_events,
                value_column=value_column,
                index=index,
                columns=columns,
                dtype=dtype,
            )
            if value_column in dependent_columns:
                frame = frame.where(outstanding_available.eq(1.0))
            frame.to_parquet(out_dir / f"{value_column}.parquet")
            frame_stats[value_column] = {
                "rows": int(frame.shape[0]),
                "columns": int(frame.shape[1]),
                "non_null": int(frame.notna().sum().sum()),
            }
            print(f"wrote: {out_dir / f'{value_column}.parquet'}")
            if value_column == "fundamental_outstanding_share_ratio":
                outstanding_ratio = frame

        if outstanding_ratio is None:
            raise RuntimeError("Outstanding-share ratio frame was not generated")
        market_cap = pd.read_parquet(
            _required_path(source_cache_dir / "market_cap.parquet")
        )
        market_cap.index = pd.to_datetime(market_cap.index).normalize()
        market_cap.columns = [_normalize_code(column) for column in market_cap.columns]
        market_cap = market_cap.reindex(index=index, columns=columns)
        outstanding_market_cap = market_cap.mul(
            outstanding_ratio.astype("float64")
        )
        outstanding_market_cap_path = (
            out_dir / "fundamental_outstanding_market_cap.parquet"
        )
        outstanding_market_cap.to_parquet(outstanding_market_cap_path)
        frame_stats["fundamental_outstanding_market_cap"] = {
            "rows": int(outstanding_market_cap.shape[0]),
            "columns": int(outstanding_market_cap.shape[1]),
            "non_null": int(outstanding_market_cap.notna().sum().sum()),
        }
        print(f"wrote: {outstanding_market_cap_path}")

        trade_price = pd.read_parquet(
            _required_path(source_cache_dir / "trade_price.parquet")
        )
        trade_price.index = pd.to_datetime(trade_price.index).normalize()
        trade_price.columns = [
            _normalize_code(column) for column in trade_price.columns
        ]
        trade_price = trade_price.reindex(index=index, columns=columns)
        split_adjusted_issued_shares = (
            market_cap.div(trade_price.where(trade_price.gt(0.0)))
            .replace([np.inf, -np.inf], np.nan)
            .astype("float32")
        )
        (
            latest_fy_share_supply_change,
            latest_fy_share_supply_available,
            latest_fy_share_supply_diagnostics,
        ) = _build_latest_fy_net_share_supply_frames(
            annual_events=events,
            stock_issuance_events=stock_issuance_events,
            split_adjusted_issued_shares=(
                split_adjusted_issued_shares
            ),
            index=index,
            columns=columns,
        )
        latest_fy_share_supply_change_path = (
            out_dir
            / "fundamental_net_share_supply_latest_fy_change.parquet"
        )
        latest_fy_share_supply_change.to_parquet(
            latest_fy_share_supply_change_path
        )
        frame_stats["fundamental_net_share_supply_latest_fy_change"] = {
            "rows": int(latest_fy_share_supply_change.shape[0]),
            "columns": int(latest_fy_share_supply_change.shape[1]),
            "non_null": int(
                latest_fy_share_supply_change.notna().sum().sum()
            ),
        }
        print(f"wrote: {latest_fy_share_supply_change_path}")

        latest_fy_share_supply_available_path = (
            out_dir
            / "fundamental_net_share_supply_latest_fy_available.parquet"
        )
        latest_fy_share_supply_available.to_parquet(
            latest_fy_share_supply_available_path
        )
        frame_stats["fundamental_net_share_supply_latest_fy_available"] = {
            "rows": int(latest_fy_share_supply_available.shape[0]),
            "columns": int(latest_fy_share_supply_available.shape[1]),
            "non_null": int(
                latest_fy_share_supply_available.notna().sum().sum()
            ),
        }
        print(f"wrote: {latest_fy_share_supply_available_path}")
        if stock_issuance_diagnostics is not None:
            stock_issuance_diagnostics["latest_fiscal_year"] = (
                latest_fy_share_supply_diagnostics
            )

    if dividend_events is not None:
        dividend_amount_ttm, dividend_available = _build_dividend_ttm_frames(
            events=dividend_events,
            covered_codes=dividend_covered_codes,
            index=index,
            columns=columns,
        )
        dividend_amount_ttm_path = (
            out_dir / "fundamental_cash_dividend_ttm_amount_proxy.parquet"
        )
        dividend_amount_ttm.to_parquet(dividend_amount_ttm_path)
        frame_stats["fundamental_cash_dividend_ttm_amount_proxy"] = {
            "rows": int(dividend_amount_ttm.shape[0]),
            "columns": int(dividend_amount_ttm.shape[1]),
            "non_null": int(dividend_amount_ttm.notna().sum().sum()),
        }
        print(f"wrote: {dividend_amount_ttm_path}")
        obsolete_per_share_path = (
            out_dir / "fundamental_cash_dividend_ttm_per_share.parquet"
        )
        if obsolete_per_share_path.exists():
            obsolete_per_share_path.unlink()
            print(f"removed obsolete: {obsolete_per_share_path}")

        dividend_available_path = (
            out_dir / "fundamental_cash_dividend_available.parquet"
        )
        dividend_available.to_parquet(dividend_available_path)
        frame_stats["fundamental_cash_dividend_available"] = {
            "rows": int(dividend_available.shape[0]),
            "columns": int(dividend_available.shape[1]),
            "non_null": int(dividend_available.notna().sum().sum()),
        }
        print(f"wrote: {dividend_available_path}")

        if dividend_payment_rows is None:
            raise RuntimeError("Dividend payment rows were not generated")
        (
            dividend_latest_fy_amount,
            dividend_latest_fy_available,
            dividend_latest_fy_diagnostics,
        ) = _build_dividend_latest_fy_frames(
            payment_rows=dividend_payment_rows,
            annual_events=events,
            covered_codes=dividend_covered_codes,
            index=index,
            columns=columns,
        )
        dividend_latest_fy_amount_path = (
            out_dir
            / "fundamental_cash_dividend_latest_fy_amount_proxy.parquet"
        )
        dividend_latest_fy_amount.to_parquet(
            dividend_latest_fy_amount_path
        )
        frame_stats["fundamental_cash_dividend_latest_fy_amount_proxy"] = {
            "rows": int(dividend_latest_fy_amount.shape[0]),
            "columns": int(dividend_latest_fy_amount.shape[1]),
            "non_null": int(
                dividend_latest_fy_amount.notna().sum().sum()
            ),
        }
        print(f"wrote: {dividend_latest_fy_amount_path}")

        dividend_latest_fy_available_path = (
            out_dir
            / "fundamental_cash_dividend_latest_fy_available.parquet"
        )
        dividend_latest_fy_available.to_parquet(
            dividend_latest_fy_available_path
        )
        frame_stats["fundamental_cash_dividend_latest_fy_available"] = {
            "rows": int(dividend_latest_fy_available.shape[0]),
            "columns": int(dividend_latest_fy_available.shape[1]),
            "non_null": int(
                dividend_latest_fy_available.notna().sum().sum()
            ),
        }
        print(f"wrote: {dividend_latest_fy_available_path}")
        if dividend_diagnostics is not None:
            dividend_diagnostics["latest_fiscal_year"] = (
                dividend_latest_fy_diagnostics
            )

    _write_report(
        out_dir=out_dir,
        raw_dir=raw_dir,
        source_cache_dir=source_cache_dir,
        index=index,
        columns=columns,
        events=events,
        diagnostics=diagnostics,
        frame_stats=frame_stats,
        stock_issuance_diagnostics=stock_issuance_diagnostics,
        dividend_diagnostics=dividend_diagnostics,
    )
    print(f"saved: {out_dir / REPORT_FILE}")
    print("diagnostics:", diagnostics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
