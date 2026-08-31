#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from live_common.env import ENV_PATH, load_dotenv
from live_common.kis_base import (
    KISBaseClient,
    KISConfig,
    KISError,
    KIS_PAPER_BASE_URL,
    KIS_REAL_BASE_URL,
    redacted_account,
)


def _token_cache_path(env: str) -> Path:
    return ROOT_DIR / "us_stock_live" / ".cache" / f"kis_token_{env}.json"


def load_kis_config(env_path: Path = ENV_PATH) -> KISConfig:
    load_dotenv(env_path)
    env = os.environ.get("KIS_ENV", "live").strip().lower()
    if env not in {"live", "paper"}:
        raise KISError("KIS_ENV must be 'live' or 'paper'")

    prefix = "KIS_LIVE" if env == "live" else "KIS_PAPER"
    app_key = os.environ.get(f"{prefix}_APP_KEY", "").strip()
    app_secret = os.environ.get(f"{prefix}_APP_SECRET", "").strip()
    account_no = os.environ.get(f"{prefix}_ACCOUNT_NO", "").strip()
    product_code = os.environ.get(f"{prefix}_ACCOUNT_PRODUCT_CODE", "").strip()

    missing = [
        name
        for name, value in {
            f"{prefix}_APP_KEY": app_key,
            f"{prefix}_APP_SECRET": app_secret,
            f"{prefix}_ACCOUNT_NO": account_no,
            f"{prefix}_ACCOUNT_PRODUCT_CODE": product_code,
        }.items()
        if not value
    ]
    if missing:
        raise KISError(f"Missing KIS environment variables: {', '.join(missing)}")
    if len(account_no) != 8:
        raise KISError(f"{prefix}_ACCOUNT_NO must be the first 8 digits of the account number")
    if len(product_code) != 2:
        raise KISError(f"{prefix}_ACCOUNT_PRODUCT_CODE must be the last 2 digits of the account number")

    return KISConfig(
        env=env,
        app_key=app_key,
        app_secret=app_secret,
        account_no=account_no,
        account_product_code=product_code,
        base_url=KIS_REAL_BASE_URL if env == "live" else KIS_PAPER_BASE_URL,
    )


class KISClient(KISBaseClient):
    def _token_cache_path(self) -> Path:
        return _token_cache_path(self.config.env)

    def overseas_price(self, symbol: str, exchange_code: str = "NAS") -> dict[str, Any]:
        return self.get(
            "/uapi/overseas-price/v1/quotations/price",
            "HHDFS00000300",
            {
                "AUTH": "",
                "EXCD": exchange_code,
                "SYMB": symbol.upper(),
            },
        )

    def overseas_balance(
        self,
        *,
        exchange_code: str = "NASD",
        currency: str = "USD",
        ctx_area_fk200: str = "",
        ctx_area_nk200: str = "",
    ) -> dict[str, Any]:
        tr_id = "TTTS3012R" if self.is_live else "VTTS3012R"
        return self.get(
            "/uapi/overseas-stock/v1/trading/inquire-balance",
            tr_id,
            {
                "CANO": self.config.account_no,
                "ACNT_PRDT_CD": self.config.account_product_code,
                "OVRS_EXCG_CD": exchange_code,
                "TR_CRCY_CD": currency,
                "CTX_AREA_FK200": ctx_area_fk200,
                "CTX_AREA_NK200": ctx_area_nk200,
            },
        )

    def overseas_orderable_amount(
        self,
        *,
        symbol: str,
        price: str | float,
        exchange_code: str = "AMEX",
    ) -> dict[str, Any]:
        tr_id = "TTTS3007R" if self.is_live else "VTTS3007R"
        return self.get(
            "/uapi/overseas-stock/v1/trading/inquire-psamount",
            tr_id,
            {
                "CANO": self.config.account_no,
                "ACNT_PRDT_CD": self.config.account_product_code,
                "OVRS_EXCG_CD": exchange_code,
                "OVRS_ORD_UNPR": str(price),
                "ITEM_CD": symbol.upper(),
            },
        )

    def overseas_order(
        self,
        *,
        side: str,
        symbol: str,
        qty: int,
        limit_price: str | float,
        exchange_code: str,
        order_division: str = "00",
    ) -> dict[str, Any]:
        side = side.lower().strip()
        if side not in {"buy", "sell"}:
            raise KISError("side must be buy or sell")
        if qty <= 0:
            raise KISError("qty must be positive")
        if exchange_code not in {"NASD", "NYSE", "AMEX"}:
            raise KISError("US overseas_order exchange_code must be NASD, NYSE, or AMEX")

        if side == "buy":
            tr_id = "TTTT1002U" if self.is_live else "VTTT1002U"
            sell_type = ""
        else:
            tr_id = "TTTT1006U" if self.is_live else "VTTT1006U"
            sell_type = "00"

        return self.post(
            "/uapi/overseas-stock/v1/trading/order",
            tr_id,
            {
                "CANO": self.config.account_no,
                "ACNT_PRDT_CD": self.config.account_product_code,
                "OVRS_EXCG_CD": exchange_code,
                "PDNO": symbol.upper(),
                "ORD_QTY": str(int(qty)),
                "OVRS_ORD_UNPR": str(limit_price),
                "CTAC_TLNO": "",
                "MGCO_APTM_ODNO": "",
                "SLL_TYPE": sell_type,
                "ORD_SVR_DVSN_CD": "0",
                "ORD_DVSN": order_division,
            },
        )

    def overseas_order_history(
        self,
        *,
        start_date: str,
        end_date: str,
        symbol: str = "%",
        exchange_code: str = "NASD",
        side_filter: str = "00",
        fill_filter: str = "00",
        sort_order: str = "DS",
        order_date: str = "",
        order_branch: str = "",
        order_no: str = "",
        ctx_area_fk200: str = "",
        ctx_area_nk200: str = "",
    ) -> dict[str, Any]:
        tr_id = "TTTS3035R" if self.is_live else "VTTS3035R"
        return self.get(
            "/uapi/overseas-stock/v1/trading/inquire-ccnl",
            tr_id,
            {
                "CANO": self.config.account_no,
                "ACNT_PRDT_CD": self.config.account_product_code,
                "PDNO": symbol.upper(),
                "ORD_STRT_DT": start_date,
                "ORD_END_DT": end_date,
                "SLL_BUY_DVSN": side_filter,
                "CCLD_NCCS_DVSN": fill_filter,
                "OVRS_EXCG_CD": exchange_code,
                "SORT_SQN": sort_order,
                "ORD_DT": order_date,
                "ORD_GNO_BRNO": order_branch,
                "ODNO": order_no,
                "CTX_AREA_FK200": ctx_area_fk200,
                "CTX_AREA_NK200": ctx_area_nk200,
            },
        )
