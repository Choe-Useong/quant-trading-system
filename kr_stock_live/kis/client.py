#!/usr/bin/env python3
from __future__ import annotations

import os
import json
import time
from pathlib import Path
from typing import Any

from live_common.env import ENV_PATH, load_dotenv
from live_common.kis_base import (
    KISBaseClient,
    KISError,
    KISConfig,
    KIS_PAPER_BASE_URL,
    KIS_REAL_BASE_URL,
    TOKEN_EXPIRY_SAFETY_SECONDS,
)


def _account_env_name(prefix: str, account_profile: str, field: str) -> str:
    suffix = "" if account_profile == "default" else f"_{account_profile.upper()}"
    return f"{prefix}_{field}{suffix}"


def load_kr_kis_config(
    *,
    account_profile: str = "default",
    env_path: Path = ENV_PATH,
) -> KISConfig:
    load_dotenv(env_path)
    env = os.environ.get("KIS_ENV", "live").strip().lower()
    if env not in {"live", "paper"}:
        raise KISError("KIS_ENV must be 'live' or 'paper'")

    account_profile = account_profile.strip().lower()
    if not account_profile:
        raise KISError("account_profile must not be empty")

    prefix = "KIS_LIVE" if env == "live" else "KIS_PAPER"
    app_key_name = _account_env_name(prefix, account_profile, "APP_KEY")
    app_secret_name = _account_env_name(prefix, account_profile, "APP_SECRET")
    fallback_app_key_name = f"{prefix}_APP_KEY"
    fallback_app_secret_name = f"{prefix}_APP_SECRET"
    account_no_name = _account_env_name(prefix, account_profile, "ACCOUNT_NO")
    product_code_name = _account_env_name(prefix, account_profile, "ACCOUNT_PRODUCT_CODE")

    app_key = os.environ.get(app_key_name, "").strip() or os.environ.get(fallback_app_key_name, "").strip()
    app_secret = os.environ.get(app_secret_name, "").strip() or os.environ.get(fallback_app_secret_name, "").strip()
    account_no = os.environ.get(account_no_name, "").strip()
    product_code = os.environ.get(product_code_name, "").strip()

    missing = [
        name
        for name, value in {
            app_key_name: app_key,
            app_secret_name: app_secret,
            account_no_name: account_no,
            product_code_name: product_code,
        }.items()
        if not value
    ]
    if missing:
        raise KISError(f"Missing KIS environment variables: {', '.join(missing)}")
    if len(account_no) != 8:
        raise KISError(f"{account_no_name} must be the first 8 digits of the account number")
    if len(product_code) != 2:
        raise KISError(f"{product_code_name} must be the last 2 digits of the account number")

    config = KISConfig(
        env=env,
        app_key=app_key,
        app_secret=app_secret,
        account_no=account_no,
        account_product_code=product_code,
        base_url=KIS_REAL_BASE_URL if env == "live" else KIS_PAPER_BASE_URL,
    )
    object.__setattr__(config, "account_profile", account_profile)
    return config


class KRKISClient(KISBaseClient):
    def _token_cache_path(self) -> Path:
        profile = getattr(self.config, "account_profile", "default")
        return Path(__file__).resolve().parents[1] / ".cache" / f"kis_token_{self.config.env}_{profile}.json"

    def _load_cached_token(self) -> str | None:
        cache_path = self._token_cache_path()
        if not cache_path.exists():
            return None
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        token = str(payload.get("access_token", "")).strip()
        expires_at = float(payload.get("expires_at", 0.0) or 0.0)
        if not token or expires_at <= time.time() + TOKEN_EXPIRY_SAFETY_SECONDS:
            return None
        self._access_token = token
        self._access_token_expires_at = expires_at
        return token

    def _write_cached_token(self, token: str, expires_at: float) -> None:
        cache_path = self._token_cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "env": self.config.env,
            "account_profile": getattr(self.config, "account_profile", "default"),
            "account": f"{self.config.account_no[:2]}******-{self.config.account_product_code}",
            "access_token": token,
            "expires_at": expires_at,
            "saved_at": time.time(),
        }
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def domestic_price(self, symbol: str, market_division: str = "J") -> dict[str, Any]:
        return self.get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100",
            {
                "FID_COND_MRKT_DIV_CODE": market_division,
                "FID_INPUT_ISCD": symbol,
            },
        )

    def domestic_balance(
        self,
        *,
        afhr_flpr_yn: str = "N",
        inqr_dvsn: str = "01",
        unpr_dvsn: str = "01",
        fund_sttl_icld_yn: str = "N",
        fncg_amt_auto_rdpt_yn: str = "N",
        prcs_dvsn: str = "00",
        ctx_area_fk100: str = "",
        ctx_area_nk100: str = "",
    ) -> dict[str, Any]:
        tr_id = "TTTC8434R" if self.is_live else "VTTC8434R"
        return self.get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id,
            {
                "CANO": self.config.account_no,
                "ACNT_PRDT_CD": self.config.account_product_code,
                "AFHR_FLPR_YN": afhr_flpr_yn,
                "OFL_YN": "",
                "INQR_DVSN": inqr_dvsn,
                "UNPR_DVSN": unpr_dvsn,
                "FUND_STTL_ICLD_YN": fund_sttl_icld_yn,
                "FNCG_AMT_AUTO_RDPT_YN": fncg_amt_auto_rdpt_yn,
                "PRCS_DVSN": prcs_dvsn,
                "CTX_AREA_FK100": ctx_area_fk100,
                "CTX_AREA_NK100": ctx_area_nk100,
            },
        )

    def domestic_orderable_amount(
        self,
        *,
        symbol: str,
        price: str | float,
        order_division: str = "01",
        cma_evlu_amt_icld_yn: str = "N",
        ovrs_icld_yn: str = "N",
    ) -> dict[str, Any]:
        tr_id = "TTTC8908R" if self.is_live else "VTTC8908R"
        return self.get(
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            tr_id,
            {
                "CANO": self.config.account_no,
                "ACNT_PRDT_CD": self.config.account_product_code,
                "PDNO": symbol,
                "ORD_UNPR": str(price),
                "ORD_DVSN": order_division,
                "CMA_EVLU_AMT_ICLD_YN": cma_evlu_amt_icld_yn,
                "OVRS_ICLD_YN": ovrs_icld_yn,
            },
        )

    def domestic_order(
        self,
        *,
        side: str,
        symbol: str,
        qty: int,
        price: str | float,
        order_division: str = "00",
    ) -> dict[str, Any]:
        side = side.lower().strip()
        if side not in {"buy", "sell"}:
            raise KISError("side must be buy or sell")
        if qty <= 0:
            raise KISError("qty must be positive")

        tr_id = ("TTTC0012U" if self.is_live else "VTTC0012U") if side == "buy" else (
            "TTTC0011U" if self.is_live else "VTTC0011U"
        )
        return self.post(
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id,
            {
                "CANO": self.config.account_no,
                "ACNT_PRDT_CD": self.config.account_product_code,
                "PDNO": symbol,
                "ORD_DVSN": order_division,
                "ORD_QTY": str(int(qty)),
                "ORD_UNPR": str(price),
                "EXCG_ID_DVSN_CD": "KRX",
                "SLL_TYPE": "01" if side == "sell" else "",
                "CNDT_PRIC": "",
            },
        )

    def domestic_order_history(
        self,
        *,
        start_date: str,
        end_date: str,
        symbol: str = "",
        side_filter: str = "00",
        fill_filter: str = "00",
        inquiry_division: str = "00",
        inquiry_division_3: str = "00",
        inquiry_division_1: str = "",
        order_branch: str = "",
        order_no: str = "",
        ctx_area_fk100: str = "",
        ctx_area_nk100: str = "",
    ) -> dict[str, Any]:
        tr_id = "TTTC0081R" if self.is_live else "VTTC0081R"
        return self.get(
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            tr_id,
            {
                "CANO": self.config.account_no,
                "ACNT_PRDT_CD": self.config.account_product_code,
                "INQR_STRT_DT": start_date,
                "INQR_END_DT": end_date,
                "SLL_BUY_DVSN_CD": side_filter,
                "PDNO": symbol,
                "CCLD_DVSN": fill_filter,
                "INQR_DVSN": inquiry_division,
                "INQR_DVSN_3": inquiry_division_3,
                "ORD_GNO_BRNO": order_branch,
                "ODNO": order_no,
                "INQR_DVSN_1": inquiry_division_1,
                "EXCG_ID_DVSN_CD": "KRX",
                "CTX_AREA_FK100": ctx_area_fk100,
                "CTX_AREA_NK100": ctx_area_nk100,
            },
        )
