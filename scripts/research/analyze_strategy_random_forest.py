from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import GroupShuffleSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder


ROOT = Path(__file__).resolve().parents[2]
INPUT_PATH = ROOT / "data" / "research" / "strategy_exhaustive_table.parquet"
OUT_DIR = ROOT / "data" / "research" / "rf_meta_analysis"

TARGETS = {
    "sharpe_ratio": "sharpe_ratio",
    "cagr_pct": "cagr_pct",
    "max_drawdown_abs_pct": "max_drawdown_abs_pct",
}

BASE_CATEGORICAL = [
    "record_type",
    "source_kind",
    "sample_type",
    "asset_hint",
    "benchmark_market",
    "rebalance_frequency",
    "timeframe_norm",
    "execution_price_column",
    "portfolio_name",
    "weighting",
]

BASE_NUMERIC = [
    "timeframe_minutes_norm",
    "periods_per_year",
    "short",
    "mid",
    "long",
    "fast_short",
    "fast_long",
    "confirm_short",
    "confirm_long",
    "a_short",
    "a_long",
    "b_short",
    "b_long",
    "mom_window",
    "adx_threshold",
    "adx_window",
    "vol_short",
    "vol_long",
    "atr_window",
    "atr_k",
    "step_up",
    "step_down",
    "sigma_k",
    "std_window",
    "lag",
    "momentum_window",
    "ma_window",
    "slope_window",
    "weak_short",
    "weak_long",
    "strong_short",
    "strong_long",
    "turnover_bucket",
    "gaussian_bucket",
    "ret_window",
    "z_window",
    "min_age_days",
    "impact_bucket",
    "short_window",
    "long_window",
    "rank_momentum_bucket",
    "momentum_bucket",
    "rank_avg_window",
    "turn180_bucket",
    "turn30_bucket",
    "near_high_bucket",
    "leader_ma_long",
    "volatility_bucket",
    "vol_rank_avg_window",
    "vol_window",
    "turn_rank_bucket",
    "sleeve_count",
    "sleeve_weight",
]


def available_columns(frame: pd.DataFrame, columns: list[str]) -> list[str]:
    return [column for column in columns if column in frame.columns]


def coerce_numeric(frame: pd.DataFrame, columns: list[str]) -> None:
    for column in columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")


def load_frame() -> pd.DataFrame:
    frame = pd.read_parquet(INPUT_PATH)
    extra_numeric = ["sharpe_ratio", "cagr_pct", "max_drawdown_pct"]
    coerce_numeric(frame, available_columns(frame, BASE_NUMERIC + list(TARGETS.values()) + extra_numeric))
    frame = frame.replace([np.inf, -np.inf], np.nan)
    if "max_drawdown_pct" in frame.columns:
        frame["max_drawdown_abs_pct"] = frame["max_drawdown_pct"].abs()
    return frame


def prepare_xy(frame: pd.DataFrame, target_column: str) -> tuple[pd.DataFrame, pd.Series, pd.Series, list[str], list[str]]:
    numeric_columns = available_columns(frame, BASE_NUMERIC)
    categorical_columns = available_columns(frame, BASE_CATEGORICAL)

    work = frame.copy()
    work = work[work[target_column].notna()].copy()
    work = work[work["dataset_name"].notna()].copy()
    work = work.replace([np.inf, -np.inf], np.nan)

    y = pd.to_numeric(work[target_column], errors="coerce")
    valid = y.notna() & np.isfinite(y)
    work = work.loc[valid].copy()
    y = y.loc[valid].reset_index(drop=True)

    numeric_columns = [column for column in numeric_columns if not work[column].isna().all()]
    categorical_columns = [column for column in categorical_columns if not work[column].isna().all()]
    feature_columns = numeric_columns + categorical_columns
    x = work[feature_columns].reset_index(drop=True).copy()
    groups = work["dataset_name"].reset_index(drop=True)
    return x, y, groups, numeric_columns, categorical_columns


def build_pipeline(numeric_columns: list[str], categorical_columns: list[str]) -> Pipeline:
    numeric_pipe = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
        ]
    )
    categorical_pipe = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="most_frequent")),
            ("onehot", OneHotEncoder(handle_unknown="ignore")),
        ]
    )
    preprocessor = ColumnTransformer(
        transformers=[
            ("num", numeric_pipe, numeric_columns),
            ("cat", categorical_pipe, categorical_columns),
        ]
    )
    model = RandomForestRegressor(
        n_estimators=100,
        random_state=42,
        n_jobs=-1,
        min_samples_leaf=3,
    )
    return Pipeline(
        steps=[
            ("preprocessor", preprocessor),
            ("model", model),
        ]
    )


def feature_names(pipeline: Pipeline, numeric_columns: list[str], categorical_columns: list[str]) -> list[str]:
    preprocessor: ColumnTransformer = pipeline.named_steps["preprocessor"]
    names: list[str] = []
    names.extend(numeric_columns)
    if categorical_columns:
        cat_pipe: Pipeline = preprocessor.named_transformers_["cat"]
        onehot: OneHotEncoder = cat_pipe.named_steps["onehot"]
        names.extend(onehot.get_feature_names_out(categorical_columns).tolist())
    return names


def run_target(frame: pd.DataFrame, target_name: str, target_column: str) -> dict:
    x, y, groups, numeric_columns, categorical_columns = prepare_xy(frame, target_column)
    splitter = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
    train_idx, test_idx = next(splitter.split(x, y, groups=groups))
    x_train = x.iloc[train_idx].copy()
    x_test = x.iloc[test_idx].copy()
    y_train = y.iloc[train_idx]
    y_test = y.iloc[test_idx]

    numeric_columns = [column for column in numeric_columns if not x_train[column].isna().all()]
    categorical_columns = [column for column in categorical_columns if not x_train[column].isna().all()]
    keep_columns = numeric_columns + categorical_columns
    x_train = x_train[keep_columns]
    x_test = x_test[keep_columns]

    pipeline = build_pipeline(numeric_columns, categorical_columns)
    pipeline.fit(x_train, y_train)
    pred = pipeline.predict(x_test)

    metrics = {
        "target": target_name,
        "target_column": target_column,
        "rows_total": int(len(x)),
        "rows_train": int(len(x_train)),
        "rows_test": int(len(x_test)),
        "groups_train": int(groups.iloc[train_idx].nunique()),
        "groups_test": int(groups.iloc[test_idx].nunique()),
        "r2": float(r2_score(y_test, pred)),
        "mae": float(mean_absolute_error(y_test, pred)),
        "target_mean_train": float(np.mean(y_train)),
        "target_mean_test": float(np.mean(y_test)),
    }

    transformed_test = pipeline.named_steps["preprocessor"].transform(x_test)
    rf_model: RandomForestRegressor = pipeline.named_steps["model"]
    perm = permutation_importance(
        rf_model,
        transformed_test,
        y_test,
        n_repeats=5,
        random_state=42,
        n_jobs=-1,
    )
    names = feature_names(pipeline, numeric_columns, categorical_columns)
    importance = pd.DataFrame(
        {
            "feature": names,
            "importance_mean": perm.importances_mean,
            "importance_std": perm.importances_std,
        }
    ).sort_values("importance_mean", ascending=False)
    importance.to_csv(OUT_DIR / f"rf_importance_{target_name}.csv", index=False)
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target",
        choices=["all", *TARGETS.keys()],
        default="all",
        help="Run one target or all targets sequentially.",
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    frame = load_frame()
    target_items = TARGETS.items() if args.target == "all" else [(args.target, TARGETS[args.target])]
    results = []
    for target_name, target_column in target_items:
        metrics = run_target(frame, target_name, target_column)
        results.append(metrics)
        (OUT_DIR / f"rf_summary_{target_name}.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    if args.target == "all":
        (OUT_DIR / "rf_summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Wrote random-forest reports to {OUT_DIR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
