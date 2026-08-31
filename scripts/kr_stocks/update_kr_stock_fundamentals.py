#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from io import BytesIO
from pathlib import Path
from threading import Lock
from zipfile import ZipFile

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_DIR = ROOT / "data" / "kr_stocks" / "raw"
DEFAULT_CLOSE_FILE = DEFAULT_RAW_DIR / "close_fdr.parquet"

DART_CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
DART_COMPANY_URL = "https://opendart.fss.or.kr/api/company.json"
DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DATA_GO_SUMMARY_URL = (
    "https://apis.data.go.kr/1160100/service/"
    "GetFinaStatInfoService_V2/getSummFinaStat_V2"
)
DATA_GO_COMPANY_PROFILE_URL = (
    "https://apis.data.go.kr/1160100/service/"
    "GetCorpBasicInfoService_V2/getCorpOutline_V2"
)
DATA_GO_STOCK_ISSUANCE_URL = (
    "https://apis.data.go.kr/1160100/"
    "GetStkIssuInfoService/getStkIssuInfo"
)
DATA_GO_STOCK_DIVIDEND_URL = (
    "https://apis.data.go.kr/1160100/"
    "GetStocDiviInfoService_V2/getDiviInfo_V2"
)

CORP_MAP_FILE = "fundamental_corp_map.parquet"
DART_CORP_CODES_FILE = "fundamental_dart_corp_codes.parquet"
DISCLOSURE_FILE = "fundamental_disclosures.parquet"
SUMMARY_FILE = "fundamental_summary.parquet"
COMPANY_PROFILE_FILE = "fundamental_company_profiles.parquet"
INDUSTRY_MAP_FILE = "fundamental_industry_map.parquet"
STOCK_ISSUANCE_FILE = "fundamental_stock_issuance_disclosures.parquet"
STOCK_DIVIDEND_FILE = "fundamental_stock_dividends.parquet"
REPORT_FILE = "fundamental_download_report.json"

DISCLOSURE_COLUMNS = [
    "corp_cls",
    "corp_name",
    "corp_code",
    "stock_code",
    "report_nm",
    "rcept_no",
    "flr_nm",
    "rcept_dt",
    "rm",
]

SUMMARY_COLUMNS = [
    "basDt",
    "bizYear",
    "crno",
    "curCd",
    "fnclDcd",
    "fnclDcdNm",
    "enpSaleAmt",
    "enpBzopPft",
    "enpCrtmNpf",
    "iclsPalClcAmt",
    "enpTastAmt",
    "enpTdbtAmt",
    "enpTcptAmt",
    "enpCptlAmt",
    "fnclDebtRto",
]

COMPANY_PROFILE_SOURCE_COLUMNS = [
    "actnAudpnNm",
    "audtRptOpnnCtt",
    "bzno",
    "corpDcd",
    "corpDcdNm",
    "corpEnsnNm",
    "corpNm",
    "corpRegMrktDcd",
    "corpRegMrktDcdNm",
    "crno",
    "empeAvgCnwkTermCtt",
    "enpBsadr",
    "enpDtadr",
    "enpEmpeCnt",
    "enpEstbDt",
    "enpFxno",
    "enpHmpgUrl",
    "enpKosdaqLstgAbolDt",
    "enpKosdaqLstgDt",
    "enpKrxLstgAbolDt",
    "enpKrxLstgDt",
    "enpMainBizNm",
    "enpMntrBnkNm",
    "enpOzpno",
    "enpPbanCmpyNm",
    "enpPn1AvgSlryAmt",
    "enpRprFnm",
    "enpStacMm",
    "enpTlno",
    "enpXchgLstgAbolDt",
    "enpXchgLstgDt",
    "fssCorpChgDtm",
    "fssCorpUnqNo",
    "fstOpegDt",
    "lastOpegDt",
    "sicNm",
    "smenpYn",
]

COMPANY_PROFILE_COLUMNS = [
    "stock_code",
    "corp_code",
    "query_bas_dt",
    "fetched_at",
    *COMPANY_PROFILE_SOURCE_COLUMNS,
]

INDUSTRY_MAP_COLUMNS = [
    "stock_code",
    "corp_code",
    "crno",
    "corp_name",
    "listed_name",
    "industry_name",
    "industry_first_observed_date",
    "industry_last_observed_date",
    "main_business_name",
    "main_business_first_observed_date",
    "main_business_last_observed_date",
    "market_name",
    "employee_count",
    "establishment_date",
    "source_query_date",
    "fetched_at",
]

STOCK_ISSUANCE_COLUMNS = [
    "basDt",
    "bizYear",
    "corpPtrnSeNm",
    "corpSeNo",
    "corpNm",
    "crno",
    "rcptNo",
    "dataSno",
    "stckIssuTcntClsfNm",
    "issuSchStckTcnt",
    "acmlIssuStckTcnt",
    "acmlDcrsStckTcnt",
    "rdcpTcnt",
    "pftIcnrTcnt",
    "rdptStckRdptTcnt",
    "etcStckTcnt",
    "maxIssuStckTcnt",
    "trsstcCnt",
    "otsstcCnt",
    "stckItmsCd",
    "stacDt",
]

STOCK_ISSUANCE_KEY_COLUMNS = ["corpSeNo", "bizYear", "rcptNo", "dataSno"]

STOCK_DIVIDEND_COLUMNS = [
    "basDt",
    "crno",
    "isinCd",
    "stckIssuCmpyNm",
    "isinCdNm",
    "scrsItmsKcd",
    "scrsItmsKcdNm",
    "stckParPrc",
    "trsnmDptyDcd",
    "trsnmDptyDcdNm",
    "stckStacMd",
    "dvdnBasDt",
    "cashDvdnPayDt",
    "stckHndvDt",
    "stckDvdnRcd",
    "stckDvdnRcdNm",
    "stckGenrDvdnAmt",
    "stckGrdnDvdnAmt",
    "stckGenrCashDvdnRt",
    "stckGenrDvdnRt",
    "cashGrdnDvdnRt",
    "stckGrdnDvdnRt",
]

STOCK_DIVIDEND_KEY_COLUMNS = [
    "isinCd",
    "dvdnBasDt",
    "cashDvdnPayDt",
    "stckDvdnRcd",
]
STOCK_DIVIDEND_REQUIRED_KEY_COLUMNS = [
    "isinCd",
    "dvdnBasDt",
    "stckDvdnRcd",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download point-in-time inputs for Korean-stock fundamentals. "
            "OpenDART supplies company mappings and filing dates; data.go.kr supplies summary values."
        )
    )
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    parser.add_argument("--close-file", default=str(DEFAULT_CLOSE_FILE))
    parser.add_argument("--start-year", type=int, default=2015)
    parser.add_argument("--end-year", type=int, default=0)
    parser.add_argument("--end-date", default="")
    parser.add_argument(
        "--codes",
        default="",
        help="Optional comma-separated six-character stock-code allow-list.",
    )
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--request-retries", type=int, default=5)
    parser.add_argument("--request-sleep-seconds", type=float, default=0.05)
    parser.add_argument(
        "--dart-max-requests-per-minute",
        "--max-requests-per-minute",
        dest="dart_max_requests_per_minute",
        type=float,
        default=480.0,
        help="OpenDART request-start limit shared by all worker threads.",
    )
    parser.add_argument("--dart-max-requests-per-run", type=int, default=9000)
    parser.add_argument("--data-go-max-requests-per-minute", type=float, default=60.0)
    parser.add_argument("--data-go-max-requests-per-run", type=int, default=9000)
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=100,
        help="Save completed company mappings after this many new requests.",
    )
    parser.add_argument(
        "--corp-code-cache-max-age-days",
        type=float,
        default=7.0,
        help="Reuse the cached OpenDART company-code list for this many days.",
    )
    parser.add_argument("--refresh-corp-codes", action="store_true")
    parser.add_argument("--disclosure-overlap-days", type=int, default=120)
    parser.add_argument(
        "--small-universe-threshold",
        type=int,
        default=100,
        help="Use per-company financial requests at or below this many mapped companies.",
    )
    parser.add_argument("--skip-disclosures", action="store_true")
    parser.add_argument("--skip-financials", action="store_true")
    parser.add_argument(
        "--include-company-profiles",
        action="store_true",
        help=(
            "Download company-profile history and build a latest non-empty industry map. "
            "Existing companies are reused unless --refresh-company-profiles is set."
        ),
    )
    parser.add_argument(
        "--refresh-company-profiles",
        action="store_true",
        help="Refetch company profiles already present in the raw cache.",
    )
    parser.add_argument(
        "--include-stock-issuance",
        action="store_true",
        help=(
            "Download Financial Services Commission stock-issuance disclosures "
            "in addition to the existing fundamental inputs."
        ),
    )
    parser.add_argument(
        "--stock-issuance-only",
        action="store_true",
        help=(
            "Download and validate only stock-issuance disclosures without "
            "calling OpenDART or rebuilding the other fundamental inputs."
        ),
    )
    parser.add_argument("--stock-issuance-start-year", type=int, default=2015)
    parser.add_argument(
        "--stock-issuance-end-year",
        type=int,
        default=0,
        help="Last stock-issuance business year; 0 means the current year.",
    )
    parser.add_argument(
        "--include-dividends",
        action="store_true",
        help=(
            "Download Financial Services Commission stock-dividend history "
            "in addition to the existing fundamental inputs."
        ),
    )
    parser.add_argument(
        "--dividends-only",
        action="store_true",
        help=(
            "Download and validate only stock-dividend history without "
            "calling OpenDART or rebuilding the other fundamental inputs."
        ),
    )
    return parser.parse_args()


def _resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def _normalize_code(value: object) -> str:
    return str(value or "").strip().upper().zfill(6)


def _load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _required_env(name: str) -> str:
    value = str(os.environ.get(name, "")).strip()
    if not value:
        raise ValueError(f"{name} is missing from the environment or .env")
    return value


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float,
        retries: int,
        sleep_seconds: float,
        max_requests_per_minute: float = 480.0,
        max_requests: int | None = None,
        service_name: str = "API",
    ) -> None:
        self.timeout = max(float(timeout), 1.0)
        self.retries = max(int(retries), 1)
        self.sleep_seconds = max(float(sleep_seconds), 0.0)
        requests_per_minute = max(float(max_requests_per_minute), 1.0)
        self._request_interval_seconds = 60.0 / requests_per_minute
        self._rate_lock = Lock()
        self._next_request_at = 0.0
        self._request_count_lock = Lock()
        self._request_count = 0
        self._max_requests = None if max_requests is None else max(int(max_requests), 1)
        self.service_name = str(service_name)

    @property
    def request_count(self) -> int:
        with self._request_count_lock:
            return self._request_count

    def _reserve_request(self) -> None:
        with self._request_count_lock:
            if self._max_requests is not None and self._request_count >= self._max_requests:
                raise RuntimeError(
                    f"{self.service_name} request limit reached: {self._max_requests} per run"
                )
            self._request_count += 1

    def _wait_for_request_slot(self) -> None:
        with self._rate_lock:
            now = time.monotonic()
            delay = self._next_request_at - now
            if delay > 0.0:
                time.sleep(delay)
            self._next_request_at = time.monotonic() + self._request_interval_seconds

    def get_bytes(self, url: str, params: dict[str, object]) -> bytes:
        full_url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(full_url, headers={"User-Agent": "Mozilla/5.0"})
        for attempt in range(self.retries):
            self._reserve_request()
            self._wait_for_request_slot()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = response.read()
                if self.sleep_seconds:
                    time.sleep(self.sleep_seconds)
                return payload
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if not retryable or attempt + 1 >= self.retries:
                    body = exc.read().decode("utf-8", errors="replace")[:500]
                    raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                http.client.HTTPException,
            ):
                if attempt + 1 >= self.retries:
                    raise
            time.sleep(1.0 + attempt * 2.0)
        raise RuntimeError("unreachable")

    def get_json(self, url: str, params: dict[str, object]) -> dict:
        payload = self.get_bytes(url, params)
        return json.loads(payload.decode("utf-8", errors="replace"))


def _dart_json(client: HttpClient, url: str, params: dict[str, object], key: str) -> dict:
    payload = client.get_json(url, {"crtfc_key": key, **params})
    status = str(payload.get("status", ""))
    if status == "013":
        return payload
    if status != "000":
        raise RuntimeError(f"OpenDART error {status}: {payload.get('message')}")
    return payload


def _data_go_items(payload: dict) -> tuple[list[dict], int]:
    response = payload.get("response", {}) or {}
    header = response.get("header", {}) or {}
    result_code = str(header.get("resultCode", ""))
    if result_code not in {"00", "0", "0000"}:
        raise RuntimeError(
            f"data.go.kr error {result_code}: {header.get('resultMsg')}"
        )
    body = response.get("body", {}) or {}
    items_payload = body.get("items", {}) or {}
    items = items_payload.get("item", []) if isinstance(items_payload, dict) else []
    if isinstance(items, dict):
        items = [items]
    return list(items or []), int(body.get("totalCount") or 0)


def _atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    try:
        frame.to_parquet(temp_path, index=False)
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _atomic_write_json(payload: dict, path: Path) -> None:
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _normalize_stock_issuance(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=STOCK_ISSUANCE_COLUMNS)
    unexpected_columns = sorted(set(frame.columns) - set(STOCK_ISSUANCE_COLUMNS))
    for column in STOCK_ISSUANCE_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""
    columns = [*STOCK_ISSUANCE_COLUMNS, *unexpected_columns]
    for column in columns:
        frame[column] = frame[column].fillna("").astype(str).str.strip()
    return frame[columns].reset_index(drop=True)


def _stock_issuance_identity_stats(frame: pd.DataFrame) -> dict[str, int]:
    numeric = {
        column: pd.to_numeric(frame[column], errors="coerce")
        for column in [
            "acmlIssuStckTcnt",
            "acmlDcrsStckTcnt",
            "maxIssuStckTcnt",
            "trsstcCnt",
            "otsstcCnt",
        ]
    }
    issuance_identity_mask = (
        numeric["acmlIssuStckTcnt"].notna()
        & numeric["acmlDcrsStckTcnt"].notna()
        & numeric["maxIssuStckTcnt"].notna()
    )
    issuance_identity_failures = int(
        (
            numeric["acmlIssuStckTcnt"][issuance_identity_mask]
            - numeric["acmlDcrsStckTcnt"][issuance_identity_mask]
            != numeric["maxIssuStckTcnt"][issuance_identity_mask]
        ).sum()
    )
    outstanding_identity_mask = (
        numeric["maxIssuStckTcnt"].notna()
        & numeric["trsstcCnt"].notna()
        & numeric["otsstcCnt"].notna()
    )
    outstanding_identity_failures = int(
        (
            numeric["maxIssuStckTcnt"][outstanding_identity_mask]
            - numeric["trsstcCnt"][outstanding_identity_mask]
            != numeric["otsstcCnt"][outstanding_identity_mask]
        ).sum()
    )
    return {
        "issuance_identity_checked_rows": int(issuance_identity_mask.sum()),
        "issuance_identity_failures": issuance_identity_failures,
        "outstanding_identity_checked_rows": int(outstanding_identity_mask.sum()),
        "outstanding_identity_failures": outstanding_identity_failures,
    }


def _stock_issuance_stats(
    frame: pd.DataFrame,
    *,
    expected_year_counts: dict[int, int] | None = None,
) -> dict:
    if frame.empty:
        year_rows: dict[str, int] = {}
        identity_by_year: dict[str, dict[str, int]] = {}
    else:
        year_rows = {
            str(year): int(count)
            for year, count in frame["bizYear"].value_counts().sort_index().items()
        }
        identity_by_year = {
            str(year): _stock_issuance_identity_stats(group)
            for year, group in frame.groupby("bizYear", sort=True)
        }
    missing_key_rows = (
        int(frame[STOCK_ISSUANCE_KEY_COLUMNS].eq("").any(axis=1).sum())
        if not frame.empty
        else 0
    )
    duplicate_key_rows = (
        int(frame.duplicated(STOCK_ISSUANCE_KEY_COLUMNS, keep=False).sum())
        if not frame.empty
        else 0
    )
    identity_stats = _stock_issuance_identity_stats(frame)

    return {
        "rows": int(len(frame)),
        "companies": int(frame["corpSeNo"].nunique()) if not frame.empty else 0,
        "year_rows": year_rows,
        "expected_year_rows": {
            str(year): int(count)
            for year, count in sorted((expected_year_counts or {}).items())
        },
        "missing_key_rows": missing_key_rows,
        "duplicate_key_rows": duplicate_key_rows,
        **identity_stats,
        "identity_by_year": identity_by_year,
    }


def _fetch_stock_issuance_disclosures(
    *,
    client: HttpClient,
    data_key: str,
    start_year: int,
    end_year: int,
) -> tuple[pd.DataFrame, dict[int, int]]:
    rows: list[dict] = []
    expected_year_counts: dict[int, int] = {}
    page_size = 10000
    for year in range(start_year, end_year + 1):
        year_rows: list[dict] = []
        page = 1
        expected_total: int | None = None
        while True:
            page_rows, total_count = _data_go_items(
                client.get_json(
                    DATA_GO_STOCK_ISSUANCE_URL,
                    {
                        "serviceKey": data_key,
                        "resultType": "json",
                        "pageNo": page,
                        "numOfRows": page_size,
                        "bizYear": year,
                    },
                )
            )
            if expected_total is None:
                expected_total = total_count
            elif total_count != expected_total:
                raise RuntimeError(
                    "Stock-issuance totalCount changed during pagination: "
                    f"year={year} expected={expected_total} observed={total_count}"
                )
            year_rows.extend(page_rows)
            if len(year_rows) >= total_count:
                break
            if not page_rows:
                raise RuntimeError(
                    "Stock-issuance pagination ended before totalCount: "
                    f"year={year} fetched={len(year_rows)} expected={total_count}"
                )
            page += 1
        if len(year_rows) != int(expected_total or 0):
            raise RuntimeError(
                "Stock-issuance row count differs from totalCount: "
                f"year={year} fetched={len(year_rows)} expected={expected_total}"
            )
        expected_year_counts[year] = int(expected_total or 0)
        rows.extend(year_rows)
        print(
            f"data.go.kr stock issuance progress: {year} "
            f"rows={len(year_rows)} pages={page}"
        )

    frame = _normalize_stock_issuance(pd.DataFrame(rows))
    stats = _stock_issuance_stats(
        frame, expected_year_counts=expected_year_counts
    )
    if stats["missing_key_rows"]:
        raise ValueError(
            "Stock-issuance disclosures contain rows with an incomplete primary key: "
            f"{stats['missing_key_rows']}"
        )
    if stats["duplicate_key_rows"]:
        raise ValueError(
            "Stock-issuance disclosures contain duplicate primary keys: "
            f"{stats['duplicate_key_rows']}"
        )
    if stats["issuance_identity_failures"] or stats["outstanding_identity_failures"]:
        print(
            "warning: stock-issuance source quantity identities differ: "
            f"issuance={stats['issuance_identity_failures']} "
            f"outstanding={stats['outstanding_identity_failures']}"
        )
    return frame, expected_year_counts


def _update_stock_issuance_report(
    *,
    raw_dir: Path,
    stats: dict,
    request_count: int,
) -> None:
    report_path = raw_dir / REPORT_FILE
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        report = {"created_at": datetime.now().isoformat(timespec="seconds")}
    report["updated_at"] = datetime.now().isoformat(timespec="seconds")
    report["stock_issuance"] = {
        **stats,
        "api_requests_this_run": int(request_count),
    }
    outputs = report.setdefault("outputs", {})
    outputs["stock_issuance_disclosures"] = str(raw_dir / STOCK_ISSUANCE_FILE)
    _atomic_write_json(report, report_path)


def _normalize_stock_dividends(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=STOCK_DIVIDEND_COLUMNS)
    unexpected_columns = sorted(set(frame.columns) - set(STOCK_DIVIDEND_COLUMNS))
    for column in STOCK_DIVIDEND_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""
    columns = [*STOCK_DIVIDEND_COLUMNS, *unexpected_columns]
    for column in columns:
        frame[column] = frame[column].fillna("").astype(str).str.strip()
    return frame[columns].reset_index(drop=True)


def _stock_dividend_stats(frame: pd.DataFrame) -> dict[str, object]:
    if frame.empty:
        return {
            "rows": 0,
            "securities": 0,
            "companies": 0,
            "snapshot_dates": [],
            "missing_key_rows": 0,
            "duplicate_key_rows": 0,
            "common_share_rows": 0,
            "cash_event_rows": 0,
            "usable_cash_payment_rows": 0,
            "usable_cash_payment_securities": 0,
            "payment_date_start": None,
            "payment_date_end": None,
        }

    amount = pd.to_numeric(frame["stckGenrDvdnAmt"], errors="coerce")
    payment_date = pd.to_datetime(
        frame["cashDvdnPayDt"], format="%Y%m%d", errors="coerce"
    )
    common_share = frame["scrsItmsKcd"].eq("0101")
    cash_event = frame["stckDvdnRcd"].isin({"02", "03"})
    usable = common_share & cash_event & amount.gt(0.0) & payment_date.notna()
    usable_payment_dates = payment_date[usable]

    return {
        "rows": int(len(frame)),
        "securities": int(frame["isinCd"].nunique()),
        "companies": int(frame["crno"].replace("", pd.NA).nunique()),
        "snapshot_dates": sorted(frame["basDt"].dropna().unique().tolist()),
        "missing_key_rows": int(
            frame[STOCK_DIVIDEND_REQUIRED_KEY_COLUMNS].eq("").any(axis=1).sum()
        ),
        "duplicate_key_rows": int(
            frame.duplicated(STOCK_DIVIDEND_KEY_COLUMNS, keep=False).sum()
        ),
        "common_share_rows": int(common_share.sum()),
        "cash_event_rows": int(cash_event.sum()),
        "usable_cash_payment_rows": int(usable.sum()),
        "usable_cash_payment_securities": int(
            frame.loc[usable, "isinCd"].nunique()
        ),
        "payment_date_start": (
            usable_payment_dates.min().date().isoformat()
            if not usable_payment_dates.empty
            else None
        ),
        "payment_date_end": (
            usable_payment_dates.max().date().isoformat()
            if not usable_payment_dates.empty
            else None
        ),
    }


def _fetch_stock_dividends(
    *,
    client: HttpClient,
    data_key: str,
) -> pd.DataFrame:
    rows: list[dict] = []
    page_size = 10000
    page = 1
    expected_total: int | None = None
    while True:
        page_rows, total_count = _data_go_items(
            client.get_json(
                DATA_GO_STOCK_DIVIDEND_URL,
                {
                    "serviceKey": data_key,
                    "resultType": "json",
                    "pageNo": page,
                    "numOfRows": page_size,
                },
            )
        )
        if expected_total is None:
            expected_total = total_count
        elif total_count != expected_total:
            raise RuntimeError(
                "Stock-dividend totalCount changed during pagination: "
                f"expected={expected_total} observed={total_count}"
            )
        rows.extend(page_rows)
        print(
            "data.go.kr stock dividend progress: "
            f"{len(rows)}/{int(expected_total or 0)}"
        )
        if len(rows) >= int(expected_total or 0):
            break
        if not page_rows:
            raise RuntimeError(
                "Stock-dividend pagination ended before totalCount: "
                f"fetched={len(rows)} expected={expected_total}"
            )
        page += 1

    if len(rows) != int(expected_total or 0):
        raise RuntimeError(
            "Stock-dividend row count differs from totalCount: "
            f"fetched={len(rows)} expected={expected_total}"
        )

    frame = _normalize_stock_dividends(pd.DataFrame(rows))
    stats = _stock_dividend_stats(frame)
    if stats["missing_key_rows"]:
        raise ValueError(
            "Stock-dividend history contains rows with an incomplete primary key: "
            f"{stats['missing_key_rows']}"
        )
    if stats["duplicate_key_rows"]:
        raise ValueError(
            "Stock-dividend history contains duplicate primary keys: "
            f"{stats['duplicate_key_rows']}"
        )
    return frame


def _update_stock_dividend_report(
    *,
    raw_dir: Path,
    stats: dict[str, object],
    request_count: int,
) -> None:
    report_path = raw_dir / REPORT_FILE
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        report = {"created_at": datetime.now().isoformat(timespec="seconds")}
    report["updated_at"] = datetime.now().isoformat(timespec="seconds")
    report["stock_dividends"] = {
        **stats,
        "api_requests_this_run": int(request_count),
        "availability_basis": "cash payment date",
        "source": DATA_GO_STOCK_DIVIDEND_URL,
    }
    outputs = report.setdefault("outputs", {})
    outputs["stock_dividends"] = str(raw_dir / STOCK_DIVIDEND_FILE)
    _atomic_write_json(report, report_path)


def _parse_codes(raw: str) -> set[str] | None:
    codes = {_normalize_code(value) for value in str(raw).split(",") if value.strip()}
    return codes or None


def _load_candidate_codes(close_file: Path, allow_list: set[str] | None) -> set[str]:
    if not close_file.exists():
        raise FileNotFoundError(close_file)
    columns = {_normalize_code(value) for value in pd.read_parquet(close_file).columns}
    if allow_list is not None:
        missing = allow_list - columns
        if missing:
            raise ValueError(f"Requested codes are absent from close cache: {sorted(missing)}")
        columns &= allow_list
    if not columns:
        raise ValueError("No candidate stock codes")
    return columns


def _download_corp_code_map(client: HttpClient, key: str) -> pd.DataFrame:
    payload = client.get_bytes(DART_CORP_CODE_URL, {"crtfc_key": key})
    try:
        with ZipFile(BytesIO(payload)) as archive:
            xml_name = next(name for name in archive.namelist() if name.lower().endswith(".xml"))
            xml_payload = archive.read(xml_name)
    except Exception as exc:
        message = payload.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"OpenDART corpCode response is not a ZIP file: {message}") from exc

    rows = []
    root = ET.fromstring(xml_payload)
    for item in root.findall(".//list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        if not stock_code or not corp_code:
            continue
        rows.append(
            {
                "stock_code": _normalize_code(stock_code),
                "corp_code": corp_code,
                "corp_name": (item.findtext("corp_name") or "").strip(),
                "corp_eng_name": (item.findtext("corp_eng_name") or "").strip(),
                "modify_date": (item.findtext("modify_date") or "").strip(),
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError("OpenDART corpCode map is empty")
    return frame.drop_duplicates("stock_code", keep="last").sort_values("stock_code")


def _normalize_dart_corp_code_map(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"stock_code", "corp_code", "corp_name"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"OpenDART company-code cache is missing columns: {sorted(missing)}")
    normalized = frame.copy()
    for column in normalized.columns:
        normalized[column] = normalized[column].fillna("").astype(str)
    normalized["stock_code"] = normalized["stock_code"].map(_normalize_code)
    normalized = normalized[
        normalized["stock_code"].astype(bool) & normalized["corp_code"].astype(bool)
    ]
    if normalized.empty:
        raise ValueError("OpenDART company-code cache is empty")
    return (
        normalized.drop_duplicates("stock_code", keep="last")
        .sort_values("stock_code")
        .reset_index(drop=True)
    )


def _load_or_download_corp_code_map(
    *,
    client: HttpClient,
    dart_key: str,
    cache_path: Path,
    max_age_days: float,
    force_refresh: bool,
) -> pd.DataFrame:
    cached: pd.DataFrame | None = None
    if cache_path.exists():
        cached = _normalize_dart_corp_code_map(pd.read_parquet(cache_path))
        age_days = max(
            (time.time() - cache_path.stat().st_mtime) / (24.0 * 60.0 * 60.0),
            0.0,
        )
        if not force_refresh and age_days <= max(float(max_age_days), 0.0):
            print(
                f"reused: {cache_path} rows={len(cached)} age_days={age_days:.2f}"
            )
            return cached

    try:
        downloaded = _normalize_dart_corp_code_map(
            _download_corp_code_map(client, dart_key)
        )
    except Exception as exc:
        if cached is None:
            raise
        print(
            f"warning: OpenDART company-code refresh failed; reused cache: "
            f"{type(exc).__name__}: {exc}"
        )
        return cached

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    downloaded.to_parquet(cache_path, index=False)
    print(f"saved: {cache_path} rows={len(downloaded)}")
    return downloaded


def _read_existing(path: Path, columns: list[str] | None = None) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=columns or [])
    frame = pd.read_parquet(path)
    for column in frame.columns:
        frame[column] = frame[column].fillna("").astype(str)
    return frame


def _fetch_company(
    client: HttpClient,
    key: str,
    stock_code: str,
    corp_code: str,
    corp_name: str,
) -> dict:
    payload = _dart_json(client, DART_COMPANY_URL, {"corp_code": corp_code}, key)
    return {
        "stock_code": stock_code,
        "corp_code": corp_code,
        "corp_name": str(payload.get("corp_name") or corp_name),
        "corp_eng_name": str(payload.get("corp_name_eng") or ""),
        "jurir_no": str(payload.get("jurir_no") or ""),
        "bizr_no": str(payload.get("bizr_no") or ""),
        "corp_cls": str(payload.get("corp_cls") or ""),
        "modify_date": str(payload.get("modify_date") or ""),
    }


def _build_corp_map(
    *,
    client: HttpClient,
    dart_key: str,
    dart_map: pd.DataFrame,
    candidate_codes: set[str],
    existing: pd.DataFrame,
    workers: int,
    checkpoint_path: Path | None = None,
    checkpoint_every: int = 100,
) -> tuple[pd.DataFrame, set[str]]:
    direct = dart_map[dart_map["stock_code"].isin(candidate_codes)].copy()
    excluded = candidate_codes - set(direct["stock_code"])

    preserved_rows: list[dict] = []
    existing_by_code = {}
    if not existing.empty and "stock_code" in existing.columns:
        preserved_rows = [
            row.to_dict()
            for _, row in existing.iterrows()
            if _normalize_code(row.get("stock_code", "")) not in candidate_codes
        ]
        existing_by_code = {
            _normalize_code(row["stock_code"]): row.to_dict()
            for _, row in existing.iterrows()
            if str(row.get("jurir_no", "")).strip()
        }

    rows: list[dict] = preserved_rows
    futures = {}
    failures: dict[str, str] = {}

    def normalized_frame() -> pd.DataFrame:
        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame
        frame["stock_code"] = frame["stock_code"].map(_normalize_code)
        return (
            frame.drop_duplicates("stock_code", keep="last")
            .sort_values("stock_code")
            .reset_index(drop=True)
        )

    def save_checkpoint() -> None:
        if checkpoint_path is None:
            return
        frame = normalized_frame()
        if frame.empty:
            return
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(checkpoint_path, index=False)

    with ThreadPoolExecutor(max_workers=max(int(workers), 1)) as pool:
        for item in direct.to_dict("records"):
            stock_code = _normalize_code(item["stock_code"])
            existing_row = existing_by_code.get(stock_code)
            if existing_row is not None and str(existing_row.get("corp_code", "")) == str(
                item["corp_code"]
            ):
                rows.append(existing_row)
                continue
            future = pool.submit(
                _fetch_company,
                client,
                dart_key,
                stock_code,
                str(item["corp_code"]),
                str(item["corp_name"]),
            )
            futures[future] = stock_code
        completed = 0
        for future in as_completed(futures):
            stock_code = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:
                failures[stock_code] = f"{type(exc).__name__}: {exc}"
            completed += 1
            if completed % max(int(checkpoint_every), 1) == 0:
                save_checkpoint()
            if completed % 100 == 0 or completed == len(futures):
                print(f"OpenDART company progress: {completed}/{len(futures)}")

    save_checkpoint()
    frame = normalized_frame()
    if frame.empty:
        raise RuntimeError("No stock codes mapped to OpenDART companies")
    mapped_candidate_codes = set(frame["stock_code"]) & candidate_codes
    if not mapped_candidate_codes:
        raise RuntimeError("No requested stock codes mapped to OpenDART companies")
    if failures:
        examples = dict(list(sorted(failures.items()))[:10])
        raise RuntimeError(
            f"OpenDART company lookup failed for {len(failures)} codes after retries; "
            f"checkpoint saved, rerun the same command. examples={examples}"
        )
    return frame, excluded


def _fetch_disclosures_for_company(
    client: HttpClient,
    key: str,
    corp_code: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    page = 1
    rows: list[dict] = []
    while True:
        payload = _dart_json(
            client,
            DART_LIST_URL,
            {
                "corp_code": corp_code,
                "bgn_de": start_date,
                "end_de": end_date,
                "last_reprt_at": "N",
                "pblntf_ty": "A",
                "pblntf_detail_ty": "A001",
                "sort": "date",
                "sort_mth": "asc",
                "page_no": page,
                "page_count": 100,
            },
            key,
        )
        page_rows = payload.get("list") or []
        rows.extend(page_rows)
        total_page = int(payload.get("total_page") or 0)
        if page >= total_page or not page_rows:
            break
        page += 1
    return rows


def _fetch_initial_disclosures(
    *,
    client: HttpClient,
    dart_key: str,
    corp_codes: list[str],
    start_date: str,
    end_date: str,
    workers: int,
) -> pd.DataFrame:
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(int(workers), 1)) as pool:
        futures = {
            pool.submit(
                _fetch_disclosures_for_company,
                client,
                dart_key,
                corp_code,
                start_date,
                end_date,
            ): corp_code
            for corp_code in corp_codes
        }
        completed = 0
        for future in as_completed(futures):
            rows.extend(future.result())
            completed += 1
            if completed % 100 == 0 or completed == len(futures):
                print(f"OpenDART disclosure progress: {completed}/{len(futures)}")
    return _normalize_disclosures(pd.DataFrame(rows))


def _iter_date_windows(start: pd.Timestamp, end: pd.Timestamp, days: int = 90):
    cursor = start.normalize()
    while cursor <= end:
        window_end = min(cursor + pd.Timedelta(days=days - 1), end)
        yield cursor, window_end
        cursor = window_end + pd.Timedelta(days=1)


def _fetch_recent_disclosures(
    *,
    client: HttpClient,
    dart_key: str,
    corp_codes: set[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
    workers: int,
) -> pd.DataFrame:
    rows: list[dict] = []
    for window_start, window_end in _iter_date_windows(start, end):
        common_params = {
            "bgn_de": window_start.strftime("%Y%m%d"),
            "end_de": window_end.strftime("%Y%m%d"),
            "last_reprt_at": "N",
            "pblntf_ty": "A",
            "pblntf_detail_ty": "A001",
            "sort": "date",
            "sort_mth": "asc",
            "page_count": 100,
        }

        def fetch_page(page: int) -> dict:
            return _dart_json(
                client,
                DART_LIST_URL,
                {**common_params, "page_no": page},
                dart_key,
            )

        first_payload = fetch_page(1)
        payloads = [first_payload]
        total_page = int(first_payload.get("total_page") or 0)
        if total_page > 1:
            with ThreadPoolExecutor(max_workers=max(int(workers), 1)) as pool:
                futures = [pool.submit(fetch_page, page) for page in range(2, total_page + 1)]
                payloads.extend(future.result() for future in as_completed(futures))
        for payload in payloads:
            page_rows = payload.get("list") or []
            rows.extend(row for row in page_rows if str(row.get("corp_code")) in corp_codes)
    return _normalize_disclosures(pd.DataFrame(rows))


def _normalize_disclosures(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=DISCLOSURE_COLUMNS)
    for column in DISCLOSURE_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""
        frame[column] = frame[column].fillna("").astype(str)
    frame["stock_code"] = frame["stock_code"].map(
        lambda value: _normalize_code(value) if str(value).strip() else ""
    )
    return frame[DISCLOSURE_COLUMNS].drop_duplicates("rcept_no", keep="last")


def _merge_by_key(existing: pd.DataFrame, fetched: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    if existing.empty:
        return fetched.copy()
    if fetched.empty:
        return existing.copy()
    combined = pd.concat([existing, fetched], ignore_index=True)
    return combined.drop_duplicates(keys, keep="last")


def _fetch_summary_page(
    client: HttpClient,
    data_key: str,
    *,
    year: int,
    page: int,
    crno: str = "",
) -> tuple[list[dict], int]:
    params: dict[str, object] = {
        "serviceKey": data_key,
        "resultType": "json",
        "pageNo": page,
        "numOfRows": 10000,
        "bizYear": year,
    }
    if crno:
        params["crno"] = crno
    return _data_go_items(client.get_json(DATA_GO_SUMMARY_URL, params))


def _fetch_summary_for_year(
    client: HttpClient,
    data_key: str,
    year: int,
    allowed_crnos: set[str],
) -> list[dict]:
    rows: list[dict] = []
    page = 1
    while True:
        page_rows, total_count = _fetch_summary_page(
            client, data_key, year=year, page=page
        )
        rows.extend(row for row in page_rows if str(row.get("crno", "")) in allowed_crnos)
        if page * 10000 >= total_count or not page_rows:
            break
        page += 1
    return rows


def _fetch_summary_for_companies(
    client: HttpClient,
    data_key: str,
    year: int,
    crnos: list[str],
) -> list[dict]:
    rows: list[dict] = []
    for crno in crnos:
        page_rows, _ = _fetch_summary_page(
            client, data_key, year=year, page=1, crno=crno
        )
        rows.extend(page_rows)
    return rows


def _normalize_summary(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=SUMMARY_COLUMNS)
    for column in SUMMARY_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""
        frame[column] = frame[column].fillna("").astype(str)
    return frame[SUMMARY_COLUMNS].drop_duplicates(
        ["crno", "bizYear", "basDt", "fnclDcd"], keep="last"
    )


def _fetch_summaries(
    *,
    client: HttpClient,
    data_key: str,
    corp_map: pd.DataFrame,
    start_year: int,
    end_year: int,
    small_universe_threshold: int,
) -> pd.DataFrame:
    crnos = sorted({str(value) for value in corp_map["jurir_no"] if str(value).strip()})
    allowed = set(crnos)
    rows: list[dict] = []
    use_company_queries = len(crnos) <= max(int(small_universe_threshold), 0)
    for year in range(start_year, end_year + 1):
        if use_company_queries:
            fetched = _fetch_summary_for_companies(client, data_key, year, crnos)
        else:
            fetched = _fetch_summary_for_year(client, data_key, year, allowed)
        rows.extend(fetched)
        print(f"data.go.kr financial progress: {year} rows={len(fetched)}")
    return _normalize_summary(pd.DataFrame(rows))


def _fetch_company_profile(
    client: HttpClient,
    data_key: str,
    *,
    stock_code: str,
    corp_code: str,
    crno: str,
    query_bas_dt: str,
    fetched_at: str,
) -> list[dict]:
    rows: list[dict] = []
    page = 1
    while True:
        params = {
            "serviceKey": data_key,
            "resultType": "json",
            "pageNo": page,
            "numOfRows": 100,
            "crno": crno,
            "basDt": query_bas_dt,
        }
        page_rows, total_count = _data_go_items(
            client.get_json(DATA_GO_COMPANY_PROFILE_URL, params)
        )
        for source_row in page_rows:
            rows.append(
                {
                    "stock_code": stock_code,
                    "corp_code": corp_code,
                    "query_bas_dt": query_bas_dt,
                    "fetched_at": fetched_at,
                    **source_row,
                }
            )
        if page * 100 >= total_count or not page_rows:
            break
        page += 1
    return rows


def _normalize_company_profiles(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=COMPANY_PROFILE_COLUMNS)
    normalized = frame.copy()
    for column in COMPANY_PROFILE_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = ""
        normalized[column] = normalized[column].fillna("").astype(str).str.strip()
    normalized["stock_code"] = normalized["stock_code"].map(_normalize_code)
    normalized = normalized[
        normalized["stock_code"].astype(bool) & normalized["crno"].astype(bool)
    ]
    return (
        normalized[COMPANY_PROFILE_COLUMNS]
        .drop_duplicates(["crno", "fstOpegDt", "lastOpegDt"], keep="last")
        .sort_values(["stock_code", "fstOpegDt", "lastOpegDt"])
        .reset_index(drop=True)
    )


def _fetch_company_profiles(
    *,
    client: HttpClient,
    data_key: str,
    corp_map: pd.DataFrame,
    existing: pd.DataFrame,
    query_bas_dt: str,
    refresh: bool,
    checkpoint_path: Path,
    checkpoint_every: int,
) -> pd.DataFrame:
    profiles = _normalize_company_profiles(existing)
    completed_crnos = (
        set(profiles["crno"].astype(str)) - {""} if not refresh else set()
    )
    candidates = corp_map.copy()
    candidates["stock_code"] = candidates["stock_code"].map(_normalize_code)
    candidates["jurir_no"] = candidates["jurir_no"].fillna("").astype(str).str.strip()
    candidates = candidates[
        candidates["jurir_no"].astype(bool)
        & ~candidates["jurir_no"].isin(completed_crnos)
    ].drop_duplicates("jurir_no", keep="last")

    fetched_parts: list[pd.DataFrame] = []
    failures: dict[str, str] = {}
    fetched_at = datetime.now().isoformat(timespec="seconds")

    def save_checkpoint() -> pd.DataFrame:
        fetched = _normalize_company_profiles(
            pd.concat(fetched_parts, ignore_index=True)
            if fetched_parts
            else pd.DataFrame()
        )
        merged = _normalize_company_profiles(
            _merge_by_key(
                profiles,
                fetched,
                ["crno", "fstOpegDt", "lastOpegDt"],
            )
        )
        if not merged.empty:
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            merged.to_parquet(checkpoint_path, index=False)
        return merged

    total = len(candidates)
    for completed, row in enumerate(candidates.to_dict("records"), start=1):
        stock_code = _normalize_code(row["stock_code"])
        crno = str(row["jurir_no"]).strip()
        try:
            rows = _fetch_company_profile(
                client,
                data_key,
                stock_code=stock_code,
                corp_code=str(row["corp_code"]).strip(),
                crno=crno,
                query_bas_dt=query_bas_dt,
                fetched_at=fetched_at,
            )
            if rows:
                fetched_parts.append(pd.DataFrame(rows))
        except Exception as exc:
            failures[stock_code] = f"{type(exc).__name__}: {exc}"
        if completed % max(int(checkpoint_every), 1) == 0:
            save_checkpoint()
        if completed % 100 == 0 or completed == total:
            print(f"data.go.kr company profile progress: {completed}/{total}")

    merged = save_checkpoint()
    if failures:
        examples = dict(list(sorted(failures.items()))[:10])
        raise RuntimeError(
            f"data.go.kr company profile lookup failed for {len(failures)} codes; "
            f"checkpoint saved, rerun the same command. examples={examples}"
        )
    return merged


def _latest_non_empty_row(group: pd.DataFrame, column: str) -> pd.Series | None:
    selected = group[group[column].astype(str).str.strip().ne("")]
    if selected.empty:
        return None
    return selected.iloc[-1]


def _build_industry_map(
    profiles: pd.DataFrame,
    corp_map: pd.DataFrame,
) -> pd.DataFrame:
    profiles = _normalize_company_profiles(profiles)
    if profiles.empty:
        return pd.DataFrame(columns=INDUSTRY_MAP_COLUMNS)

    mapping = corp_map.copy()
    mapping["stock_code"] = mapping["stock_code"].map(_normalize_code)
    mapping["jurir_no"] = mapping["jurir_no"].fillna("").astype(str).str.strip()
    mapping = mapping.drop_duplicates("jurir_no", keep="last")
    mapped_names = {
        str(row["jurir_no"]): {
            "stock_code": _normalize_code(row["stock_code"]),
            "corp_code": str(row["corp_code"]).strip(),
            "corp_name": str(row.get("corp_name", "")).strip(),
        }
        for row in mapping.to_dict("records")
        if str(row["jurir_no"]).strip()
    }

    rows: list[dict] = []
    for crno, group in profiles.groupby("crno", sort=True):
        group = group.sort_values(
            ["fstOpegDt", "lastOpegDt", "query_bas_dt", "fetched_at"]
        )
        industry = _latest_non_empty_row(group, "sicNm")
        main_business = _latest_non_empty_row(group, "enpMainBizNm")
        latest = group.iloc[-1]
        identity = mapped_names.get(str(crno), {})
        rows.append(
            {
                "stock_code": identity.get("stock_code", latest["stock_code"]),
                "corp_code": identity.get("corp_code", latest["corp_code"]),
                "crno": str(crno),
                "corp_name": (
                    str(latest["corpNm"]).strip()
                    or str(identity.get("corp_name", "")).strip()
                ),
                "listed_name": str(latest["enpPbanCmpyNm"]).strip(),
                "industry_name": "" if industry is None else industry["sicNm"],
                "industry_first_observed_date": (
                    "" if industry is None else industry["fstOpegDt"]
                ),
                "industry_last_observed_date": (
                    "" if industry is None else industry["lastOpegDt"]
                ),
                "main_business_name": (
                    "" if main_business is None else main_business["enpMainBizNm"]
                ),
                "main_business_first_observed_date": (
                    "" if main_business is None else main_business["fstOpegDt"]
                ),
                "main_business_last_observed_date": (
                    "" if main_business is None else main_business["lastOpegDt"]
                ),
                "market_name": str(latest["corpRegMrktDcdNm"]).strip(),
                "employee_count": str(latest["enpEmpeCnt"]).strip(),
                "establishment_date": str(latest["enpEstbDt"]).strip(),
                "source_query_date": str(latest["query_bas_dt"]).strip(),
                "fetched_at": str(latest["fetched_at"]).strip(),
            }
        )
    return (
        pd.DataFrame(rows, columns=INDUSTRY_MAP_COLUMNS)
        .sort_values("stock_code")
        .reset_index(drop=True)
    )


def _write_report(
    *,
    raw_dir: Path,
    start_year: int,
    end_year: int,
    candidate_codes: set[str],
    excluded_codes: set[str],
    corp_map: pd.DataFrame,
    disclosures: pd.DataFrame,
    summary: pd.DataFrame,
    company_profiles: pd.DataFrame,
    industry_map: pd.DataFrame,
    dart_request_count: int,
    data_go_request_count: int,
) -> None:
    mapped_crnos = set(corp_map["jurir_no"].astype(str)) - {""}
    financial_crnos = set(summary["crno"].astype(str)) - {""}
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "start_year": start_year,
        "end_year": end_year,
        "candidate_codes": len(candidate_codes),
        "common_share_codes": int(len(corp_map)),
        "non_common_or_unmapped_codes": len(excluded_codes),
        "non_common_or_unmapped_examples": sorted(excluded_codes)[:30],
        "corp_map_with_jurir_no": int(corp_map["jurir_no"].astype(bool).sum()),
        "disclosure_rows": int(len(disclosures)),
        "disclosure_companies": int(disclosures["corp_code"].nunique()) if not disclosures.empty else 0,
        "financial_rows": int(len(summary)),
        "financial_crnos": len(financial_crnos),
        "financial_company_coverage": (
            len(mapped_crnos & financial_crnos) / len(mapped_crnos) if mapped_crnos else 0.0
        ),
        "company_profile_rows": int(len(company_profiles)),
        "company_profile_crnos": (
            int(company_profiles["crno"].nunique()) if not company_profiles.empty else 0
        ),
        "industry_map_rows": int(len(industry_map)),
        "industry_name_rows": (
            int(industry_map["industry_name"].astype(bool).sum())
            if not industry_map.empty
            else 0
        ),
        "api_requests": {
            "opendart": int(dart_request_count),
            "data_go_kr": int(data_go_request_count),
        },
        "outputs": {
            "dart_corp_codes": str(raw_dir / DART_CORP_CODES_FILE),
            "corp_map": str(raw_dir / CORP_MAP_FILE),
            "disclosures": str(raw_dir / DISCLOSURE_FILE),
            "summary": str(raw_dir / SUMMARY_FILE),
            "company_profiles": str(raw_dir / COMPANY_PROFILE_FILE),
            "industry_map": str(raw_dir / INDUSTRY_MAP_FILE),
        },
    }
    (raw_dir / REPORT_FILE).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    args = parse_args()
    _load_env()
    raw_dir = _resolve(args.raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    if args.stock_issuance_only and args.dividends_only:
        raise ValueError(
            "--stock-issuance-only and --dividends-only cannot be used together"
        )
    stock_issuance_start_year = int(args.stock_issuance_start_year)
    stock_issuance_end_year = int(
        args.stock_issuance_end_year or pd.Timestamp.today().year
    )
    if (args.include_stock_issuance or args.stock_issuance_only) and (
        stock_issuance_start_year < 2015
        or stock_issuance_end_year < stock_issuance_start_year
    ):
        raise ValueError(
            "Stock-issuance years must satisfy "
            "2015 <= stock-issuance-start-year <= stock-issuance-end-year"
        )

    if args.stock_issuance_only:
        data_key = _required_env("DATA_GO_KR_SERVICE_KEY")
        data_go_client = HttpClient(
            timeout=args.timeout_seconds,
            retries=args.request_retries,
            sleep_seconds=args.request_sleep_seconds,
            max_requests_per_minute=args.data_go_max_requests_per_minute,
            max_requests=args.data_go_max_requests_per_run,
            service_name="data.go.kr",
        )
        print(
            "data.go.kr API limits: "
            f"{args.data_go_max_requests_per_minute:g}/minute, "
            f"{args.data_go_max_requests_per_run}/run"
        )
        stock_issuance, expected_counts = _fetch_stock_issuance_disclosures(
            client=data_go_client,
            data_key=data_key,
            start_year=stock_issuance_start_year,
            end_year=stock_issuance_end_year,
        )
        stock_issuance_path = raw_dir / STOCK_ISSUANCE_FILE
        _atomic_write_parquet(stock_issuance, stock_issuance_path)
        stock_issuance_stats = _stock_issuance_stats(
            stock_issuance, expected_year_counts=expected_counts
        )
        _update_stock_issuance_report(
            raw_dir=raw_dir,
            stats=stock_issuance_stats,
            request_count=data_go_client.request_count,
        )
        print(f"saved: {stock_issuance_path} rows={len(stock_issuance)}")
        print(f"saved: {raw_dir / REPORT_FILE}")
        print("stock_issuance:", stock_issuance_stats)
        return 0

    if args.dividends_only:
        data_key = _required_env("DATA_GO_KR_SERVICE_KEY")
        data_go_client = HttpClient(
            timeout=args.timeout_seconds,
            retries=args.request_retries,
            sleep_seconds=args.request_sleep_seconds,
            max_requests_per_minute=args.data_go_max_requests_per_minute,
            max_requests=args.data_go_max_requests_per_run,
            service_name="data.go.kr",
        )
        print(
            "data.go.kr API limits: "
            f"{args.data_go_max_requests_per_minute:g}/minute, "
            f"{args.data_go_max_requests_per_run}/run"
        )
        stock_dividends = _fetch_stock_dividends(
            client=data_go_client,
            data_key=data_key,
        )
        stock_dividend_path = raw_dir / STOCK_DIVIDEND_FILE
        _atomic_write_parquet(stock_dividends, stock_dividend_path)
        stock_dividend_stats = _stock_dividend_stats(stock_dividends)
        _update_stock_dividend_report(
            raw_dir=raw_dir,
            stats=stock_dividend_stats,
            request_count=data_go_client.request_count,
        )
        print(f"saved: {stock_dividend_path} rows={len(stock_dividends)}")
        print(f"saved: {raw_dir / REPORT_FILE}")
        print("stock_dividends:", stock_dividend_stats)
        return 0

    dart_key = _required_env("OPENDART_API_KEY")
    data_key = _required_env("DATA_GO_KR_SERVICE_KEY")
    close_file = _resolve(args.close_file)
    end_year = int(args.end_year or (pd.Timestamp.today().year - 1))
    if args.start_year < 2015 or end_year < args.start_year:
        raise ValueError("Financial years must satisfy 2015 <= start-year <= end-year")
    end_date = pd.Timestamp(args.end_date or pd.Timestamp.today()).normalize()
    candidate_codes = _load_candidate_codes(close_file, _parse_codes(args.codes))

    dart_client = HttpClient(
        timeout=args.timeout_seconds,
        retries=args.request_retries,
        sleep_seconds=args.request_sleep_seconds,
        max_requests_per_minute=args.dart_max_requests_per_minute,
        max_requests=args.dart_max_requests_per_run,
        service_name="OpenDART",
    )
    data_go_client = HttpClient(
        timeout=args.timeout_seconds,
        retries=args.request_retries,
        sleep_seconds=args.request_sleep_seconds,
        max_requests_per_minute=args.data_go_max_requests_per_minute,
        max_requests=args.data_go_max_requests_per_run,
        service_name="data.go.kr",
    )
    print(
        "OpenDART API limits: "
        f"{args.dart_max_requests_per_minute:g}/minute, "
        f"{args.dart_max_requests_per_run}/run"
    )
    print(
        "data.go.kr API limits: "
        f"{args.data_go_max_requests_per_minute:g}/minute, "
        f"{args.data_go_max_requests_per_run}/run"
    )
    stock_issuance = _read_existing(raw_dir / STOCK_ISSUANCE_FILE)
    stock_issuance_stats: dict | None = None
    stock_issuance_request_count = 0
    if args.include_stock_issuance:
        request_count_before = data_go_client.request_count
        stock_issuance, expected_counts = _fetch_stock_issuance_disclosures(
            client=data_go_client,
            data_key=data_key,
            start_year=stock_issuance_start_year,
            end_year=stock_issuance_end_year,
        )
        stock_issuance_request_count = (
            data_go_client.request_count - request_count_before
        )
        _atomic_write_parquet(stock_issuance, raw_dir / STOCK_ISSUANCE_FILE)
        stock_issuance_stats = _stock_issuance_stats(
            stock_issuance, expected_year_counts=expected_counts
        )
        print(
            f"saved: {raw_dir / STOCK_ISSUANCE_FILE} "
            f"rows={len(stock_issuance)}"
        )
    elif not stock_issuance.empty:
        stock_issuance_stats = _stock_issuance_stats(stock_issuance)

    stock_dividends = _read_existing(raw_dir / STOCK_DIVIDEND_FILE)
    stock_dividend_stats: dict[str, object] | None = None
    stock_dividend_request_count = 0
    if args.include_dividends:
        request_count_before = data_go_client.request_count
        stock_dividends = _fetch_stock_dividends(
            client=data_go_client,
            data_key=data_key,
        )
        stock_dividend_request_count = (
            data_go_client.request_count - request_count_before
        )
        _atomic_write_parquet(stock_dividends, raw_dir / STOCK_DIVIDEND_FILE)
        stock_dividend_stats = _stock_dividend_stats(stock_dividends)
        print(
            f"saved: {raw_dir / STOCK_DIVIDEND_FILE} "
            f"rows={len(stock_dividends)}"
        )
    elif not stock_dividends.empty:
        stock_dividend_stats = _stock_dividend_stats(stock_dividends)

    existing_map = _read_existing(raw_dir / CORP_MAP_FILE)
    dart_map = _load_or_download_corp_code_map(
        client=dart_client,
        dart_key=dart_key,
        cache_path=raw_dir / DART_CORP_CODES_FILE,
        max_age_days=args.corp_code_cache_max_age_days,
        force_refresh=args.refresh_corp_codes,
    )
    full_corp_map, excluded_codes = _build_corp_map(
        client=dart_client,
        dart_key=dart_key,
        dart_map=dart_map,
        candidate_codes=candidate_codes,
        existing=existing_map,
        workers=args.max_workers,
        checkpoint_path=raw_dir / CORP_MAP_FILE,
        checkpoint_every=args.checkpoint_every,
    )
    full_corp_map.to_parquet(raw_dir / CORP_MAP_FILE, index=False)
    corp_map = full_corp_map[
        full_corp_map["stock_code"].map(_normalize_code).isin(candidate_codes)
    ].copy()
    print(
        f"saved: {raw_dir / CORP_MAP_FILE} rows={len(full_corp_map)} "
        f"selected_rows={len(corp_map)}"
    )

    existing_disclosures = _read_existing(raw_dir / DISCLOSURE_FILE, DISCLOSURE_COLUMNS)
    disclosures = existing_disclosures
    if not args.skip_disclosures:
        corp_codes = set(corp_map["corp_code"].astype(str)) - {""}
        existing_corp_codes = (
            set(existing_disclosures["corp_code"].astype(str)) - {""}
            if not existing_disclosures.empty
            else set()
        )
        missing_corp_codes = sorted(corp_codes - existing_corp_codes)
        fetched_parts: list[pd.DataFrame] = []
        if missing_corp_codes:
            fetched_parts.append(
                _fetch_initial_disclosures(
                    client=dart_client,
                    dart_key=dart_key,
                    corp_codes=missing_corp_codes,
                    start_date=f"{args.start_year}0101",
                    end_date=end_date.strftime("%Y%m%d"),
                    workers=args.max_workers,
                )
            )
        if not existing_disclosures.empty:
            latest = pd.to_datetime(
                existing_disclosures["rcept_dt"], format="%Y%m%d", errors="coerce"
            ).max()
            if pd.isna(latest):
                latest = pd.Timestamp(f"{args.start_year}-01-01")
            fetch_start = max(
                pd.Timestamp(f"{args.start_year}-01-01"),
                latest - pd.Timedelta(days=max(args.disclosure_overlap_days, 0)),
            )
            if fetch_start <= end_date:
                fetched_parts.append(
                    _fetch_recent_disclosures(
                        client=dart_client,
                        dart_key=dart_key,
                        corp_codes=corp_codes,
                        start=fetch_start,
                        end=end_date,
                        workers=args.max_workers,
                    )
                )
        fetched_disclosures = _normalize_disclosures(
            pd.concat(fetched_parts, ignore_index=True)
            if fetched_parts
            else pd.DataFrame()
        )
        disclosures = _merge_by_key(
            existing_disclosures, fetched_disclosures, ["rcept_no"]
        ).sort_values(["rcept_dt", "rcept_no"])
        disclosures.to_parquet(raw_dir / DISCLOSURE_FILE, index=False)
        print(f"saved: {raw_dir / DISCLOSURE_FILE} rows={len(disclosures)}")

    existing_summary = _read_existing(raw_dir / SUMMARY_FILE, SUMMARY_COLUMNS)
    summary = existing_summary
    if not args.skip_financials:
        fetched_summary = _fetch_summaries(
            client=data_go_client,
            data_key=data_key,
            corp_map=corp_map,
            start_year=args.start_year,
            end_year=end_year,
            small_universe_threshold=args.small_universe_threshold,
        )
        summary = _merge_by_key(
            existing_summary,
            fetched_summary,
            ["crno", "bizYear", "basDt", "fnclDcd"],
        ).sort_values(["bizYear", "crno", "fnclDcd"])
        summary.to_parquet(raw_dir / SUMMARY_FILE, index=False)
        print(f"saved: {raw_dir / SUMMARY_FILE} rows={len(summary)}")

    existing_profiles = _read_existing(
        raw_dir / COMPANY_PROFILE_FILE, COMPANY_PROFILE_COLUMNS
    )
    company_profiles = existing_profiles
    if args.include_company_profiles:
        company_profiles = _fetch_company_profiles(
            client=data_go_client,
            data_key=data_key,
            corp_map=corp_map,
            existing=existing_profiles,
            query_bas_dt=end_date.strftime("%Y%m%d"),
            refresh=args.refresh_company_profiles,
            checkpoint_path=raw_dir / COMPANY_PROFILE_FILE,
            checkpoint_every=args.checkpoint_every,
        )
        company_profiles.to_parquet(raw_dir / COMPANY_PROFILE_FILE, index=False)
        print(f"saved: {raw_dir / COMPANY_PROFILE_FILE} rows={len(company_profiles)}")

    industry_map = _build_industry_map(company_profiles, corp_map)
    if not industry_map.empty:
        industry_map.to_parquet(raw_dir / INDUSTRY_MAP_FILE, index=False)
        print(
            f"saved: {raw_dir / INDUSTRY_MAP_FILE} rows={len(industry_map)} "
            f"with_industry={industry_map['industry_name'].astype(bool).sum()}"
        )

    _write_report(
        raw_dir=raw_dir,
        start_year=args.start_year,
        end_year=end_year,
        candidate_codes=candidate_codes,
        excluded_codes=excluded_codes,
        corp_map=corp_map,
        disclosures=disclosures,
        summary=summary,
        company_profiles=company_profiles,
        industry_map=industry_map,
        dart_request_count=dart_client.request_count,
        data_go_request_count=data_go_client.request_count,
    )
    if stock_issuance_stats is not None:
        _update_stock_issuance_report(
            raw_dir=raw_dir,
            stats=stock_issuance_stats,
            request_count=stock_issuance_request_count,
        )
    if stock_dividend_stats is not None:
        _update_stock_dividend_report(
            raw_dir=raw_dir,
            stats=stock_dividend_stats,
            request_count=stock_dividend_request_count,
        )
    print(f"saved: {raw_dir / REPORT_FILE}")
    print(
        "coverage:",
        {
            "candidate_codes": len(candidate_codes),
            "common_share_codes": len(corp_map),
            "excluded_codes": len(excluded_codes),
            "financial_companies": summary["crno"].nunique() if not summary.empty else 0,
            "industry_companies": (
                industry_map["industry_name"].astype(bool).sum()
                if not industry_map.empty
                else 0
            ),
            "opendart_requests": dart_client.request_count,
            "data_go_requests": data_go_client.request_count,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
