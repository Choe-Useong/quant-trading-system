#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Train a walk-forward cross-sectional ML model from a v2 long panel and "
            "export predictions as a wide custom source parquet."
        )
    )
    parser.add_argument("--panel-parquet", required=True, help="Input long panel parquet")
    parser.add_argument("--feature-columns", required=True, help="Comma-separated numeric feature columns")
    parser.add_argument("--label-column", required=True, help="Target label column, e.g. fwd_rank_pct_12")
    parser.add_argument("--score-name", required=True, help="Output score name and parquet stem")
    parser.add_argument("--out-dir", required=True, help="Output directory")
    parser.add_argument(
        "--score-copy-dir",
        default="",
        help="Optional directory to also write only the wide score parquet, e.g. a v2 source cache dir",
    )
    parser.add_argument(
        "--model",
        choices=["hist_gradient_boosting", "random_forest", "extra_trees", "lightgbm"],
        default="hist_gradient_boosting",
        help="Estimator family",
    )
    parser.add_argument("--model-params-json", default="{}", help="Optional estimator params JSON")
    parser.add_argument(
        "--model-config-json",
        default="",
        help="Optional JSON file with {'model': name, 'params': {...}}; overrides --model and --model-params-json",
    )
    parser.add_argument("--train-months", type=int, default=36, help="Rolling training window in months; 0 means expanding")
    parser.add_argument("--predict-months", type=int, default=3, help="Prediction window length in months")
    parser.add_argument("--step-months", type=int, default=3, help="Fold step length in months")
    parser.add_argument(
        "--embargo-bars",
        type=int,
        default=0,
        help="Number of unique timestamps to exclude before each prediction start",
    )
    parser.add_argument("--min-train-rows", type=int, default=1000, help="Minimum usable train rows per fold")
    parser.add_argument("--min-predict-rows", type=int, default=1, help="Minimum prediction rows per fold")
    parser.add_argument(
        "--min-train-timestamps",
        type=int,
        default=20,
        help="Minimum unique train timestamps per fold",
    )
    parser.add_argument("--min-ic-count", type=int, default=5, help="Minimum cross-section count for timestamp IC")
    parser.add_argument(
        "--train-row-filter",
        choices=["none", "base", "selected"],
        default="base",
        help="Rows eligible for training",
    )
    parser.add_argument(
        "--predict-row-filter",
        choices=["none", "base", "selected"],
        default="base",
        help="Rows eligible for prediction output",
    )
    parser.add_argument("--timestamp-column", default="timestamp", help="Timestamp column name")
    parser.add_argument("--market-column", default="market", help="Market column name")
    parser.add_argument("--start-timestamp", default="", help="Optional earliest prediction timestamp")
    parser.add_argument("--end-timestamp", default="", help="Optional latest source timestamp")
    parser.add_argument("--tail-timestamps", type=int, default=None, help="Optional tail timestamp limit for smoke runs")
    parser.add_argument("--random-state", type=int, default=42, help="Random seed for stochastic estimators")
    parser.add_argument("--n-jobs", type=int, default=-1, help="n_jobs for tree ensembles that support it")
    return parser


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_json_object(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("--model-params-json must decode to a JSON object")
    return parsed


def _load_model_config(path_value: str) -> tuple[str | None, dict[str, Any]]:
    if not path_value:
        return None, {}
    path = Path(path_value)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("--model-config-json must decode to a JSON object")
    model_name = payload.get("model")
    if model_name is not None and not isinstance(model_name, str):
        raise ValueError("model config field 'model' must be a string")
    params = payload.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("model config field 'params' must be an object")
    return model_name, params


def _json_default(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _build_estimator(model_name: str, params: dict[str, Any], *, random_state: int, n_jobs: int) -> Any:
    if model_name == "hist_gradient_boosting":
        defaults: dict[str, Any] = {
            "max_iter": 200,
            "learning_rate": 0.05,
            "max_leaf_nodes": 31,
            "l2_regularization": 0.0,
            "random_state": random_state,
        }
        defaults.update(params)
        return HistGradientBoostingRegressor(**defaults)
    if model_name == "random_forest":
        defaults = {
            "n_estimators": 300,
            "min_samples_leaf": 10,
            "max_features": 1.0,
            "random_state": random_state,
            "n_jobs": n_jobs,
        }
        defaults.update(params)
        return RandomForestRegressor(**defaults)
    if model_name == "extra_trees":
        defaults = {
            "n_estimators": 300,
            "min_samples_leaf": 10,
            "max_features": 1.0,
            "random_state": random_state,
            "n_jobs": n_jobs,
        }
        defaults.update(params)
        return ExtraTreesRegressor(**defaults)
    if model_name == "lightgbm":
        try:
            from lightgbm import LGBMRegressor
        except ImportError as exc:
            raise ImportError("lightgbm is not installed. Install it with: py -m pip install lightgbm") from exc
        defaults = {
            "n_estimators": 300,
            "learning_rate": 0.03,
            "num_leaves": 31,
            "min_child_samples": 50,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "reg_lambda": 1.0,
            "random_state": random_state,
            "n_jobs": n_jobs,
            "verbosity": -1,
        }
        defaults.update(params)
        return LGBMRegressor(**defaults)
    raise ValueError(f"unknown model: {model_name}")


def _build_pipeline(model_name: str, params: dict[str, Any], *, random_state: int, n_jobs: int) -> Pipeline:
    return Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            ("model", _build_estimator(model_name, params, random_state=random_state, n_jobs=n_jobs)),
        ]
    )


def _row_mask(frame: pd.DataFrame, row_filter: str) -> pd.Series:
    if row_filter == "none":
        return pd.Series(True, index=frame.index)
    column = "base_eligible" if row_filter == "base" else row_filter
    if column not in frame.columns:
        raise ValueError(f"row filter '{row_filter}' requires column '{column}' in panel")
    return frame[column].fillna(False).astype(bool)


def _spearman_corr(x: pd.Series, y: pd.Series) -> float:
    work = pd.DataFrame({"x": x, "y": y}).replace([np.inf, -np.inf], np.nan).dropna()
    if len(work.index) < 2:
        return float("nan")
    xr = work["x"].rank(method="average")
    yr = work["y"].rank(method="average")
    value = xr.corr(yr)
    return float(value) if np.isfinite(value) else float("nan")


def _pearson_corr(x: pd.Series, y: pd.Series) -> float:
    work = pd.DataFrame({"x": x, "y": y}).replace([np.inf, -np.inf], np.nan).dropna()
    if len(work.index) < 2:
        return float("nan")
    value = work["x"].corr(work["y"])
    return float(value) if np.isfinite(value) else float("nan")


def _timestamp_ic_rows(
    prediction_long: pd.DataFrame,
    *,
    timestamp_column: str,
    label_column: str,
    prediction_column: str,
    min_ic_count: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    grouped = prediction_long.groupby(timestamp_column, sort=True)
    for timestamp, group in grouped:
        valid = group[[prediction_column, label_column]].replace([np.inf, -np.inf], np.nan).dropna()
        count = int(len(valid.index))
        if count < min_ic_count:
            continue
        rows.append(
            {
                timestamp_column: timestamp,
                "count": count,
                "pearson_ic": _pearson_corr(valid[prediction_column], valid[label_column]),
                "spearman_ic": _spearman_corr(valid[prediction_column], valid[label_column]),
            }
        )
    return rows


def _usable_feature_columns(frame: pd.DataFrame, feature_columns: list[str]) -> list[str]:
    usable: list[str] = []
    for column in feature_columns:
        numeric = pd.to_numeric(frame[column], errors="coerce")
        if bool(np.isfinite(numeric.to_numpy(dtype=float)).any()):
            usable.append(column)
    return usable


def _coerce_numeric_columns(frame: pd.DataFrame, columns: list[str]) -> None:
    for column in columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")


def _feature_importance_rows(pipeline: Pipeline, feature_columns: list[str], fold_id: int) -> list[dict[str, Any]]:
    model = pipeline.named_steps["model"]
    values = getattr(model, "feature_importances_", None)
    if values is None:
        return []
    return [
        {"fold_id": fold_id, "feature": feature, "importance": float(importance)}
        for feature, importance in zip(feature_columns, values)
    ]


def _fold_start_timestamps(
    timestamps: pd.DatetimeIndex,
    *,
    train_months: int,
    predict_months: int,
    step_months: int,
    start_timestamp: str,
) -> list[pd.Timestamp]:
    if len(timestamps) == 0:
        return []
    min_ts = timestamps.min()
    max_ts = timestamps.max()
    if start_timestamp:
        first_target = pd.Timestamp(start_timestamp)
    elif train_months > 0:
        first_target = min_ts + pd.DateOffset(months=train_months)
    else:
        first_target = timestamps[1] if len(timestamps) > 1 else min_ts

    starts: list[pd.Timestamp] = []
    current = pd.Timestamp(first_target)
    while current <= max_ts:
        idx = timestamps.searchsorted(current, side="left")
        if idx >= len(timestamps):
            break
        actual = pd.Timestamp(timestamps[idx])
        if not starts or actual > starts[-1]:
            starts.append(actual)
        current = current + pd.DateOffset(months=step_months)
        if predict_months <= 0 or step_months <= 0:
            break
    return starts


def _load_panel(args: argparse.Namespace, feature_columns: list[str]) -> pd.DataFrame:
    panel = pd.read_parquet(args.panel_parquet)
    required_columns = [args.timestamp_column, args.market_column, args.label_column, *feature_columns]
    missing = [column for column in required_columns if column not in panel.columns]
    if missing:
        raise ValueError(f"panel is missing required columns: {missing}")
    panel = panel.copy()
    panel[args.timestamp_column] = pd.to_datetime(panel[args.timestamp_column], utc=False)
    if args.end_timestamp:
        panel = panel[panel[args.timestamp_column] <= pd.Timestamp(args.end_timestamp)].copy()
    if args.tail_timestamps is not None:
        if args.tail_timestamps <= 0:
            raise ValueError("--tail-timestamps must be positive")
        timestamps = pd.DatetimeIndex(sorted(panel[args.timestamp_column].dropna().unique()))
        keep = set(timestamps[-args.tail_timestamps :])
        panel = panel[panel[args.timestamp_column].isin(keep)].copy()
    panel[args.market_column] = panel[args.market_column].astype(str).str.upper()
    _coerce_numeric_columns(panel, [args.label_column, *feature_columns])
    panel = panel.replace([np.inf, -np.inf], np.nan)
    return panel.sort_values([args.timestamp_column, args.market_column]).reset_index(drop=True)


def main() -> int:
    args = build_parser().parse_args()
    if args.train_months < 0:
        raise ValueError("--train-months must be non-negative")
    if args.predict_months <= 0:
        raise ValueError("--predict-months must be positive")
    if args.step_months <= 0:
        raise ValueError("--step-months must be positive")
    if args.embargo_bars < 0:
        raise ValueError("--embargo-bars must be non-negative")

    feature_columns = _parse_csv(args.feature_columns)
    if not feature_columns:
        raise ValueError("--feature-columns must include at least one column")
    config_model_name, config_model_params = _load_model_config(args.model_config_json)
    model_name = config_model_name or args.model
    model_params = config_model_params if config_model_name is not None or args.model_config_json else _parse_json_object(args.model_params_json)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    panel = _load_panel(args, feature_columns)
    timestamps = pd.DatetimeIndex(sorted(panel[args.timestamp_column].dropna().unique()))
    fold_starts = _fold_start_timestamps(
        timestamps,
        train_months=args.train_months,
        predict_months=args.predict_months,
        step_months=args.step_months,
        start_timestamp=args.start_timestamp,
    )
    if not fold_starts:
        raise SystemExit("no walk-forward folds were generated")

    train_base_mask = _row_mask(panel, args.train_row_filter) & panel[args.label_column].notna()
    predict_base_mask = _row_mask(panel, args.predict_row_filter)

    prediction_frames: list[pd.DataFrame] = []
    fold_rows: list[dict[str, Any]] = []
    importance_rows: list[dict[str, Any]] = []

    for fold_id, predict_start in enumerate(fold_starts, start=1):
        predict_end = predict_start + pd.DateOffset(months=args.predict_months)
        predict_start_idx = timestamps.searchsorted(predict_start, side="left")
        train_end_idx = max(0, int(predict_start_idx) - int(args.embargo_bars))
        train_end = pd.Timestamp(timestamps[train_end_idx]) if train_end_idx < len(timestamps) else predict_start
        if train_end_idx <= 0:
            train_end = predict_start

        train_mask = panel[args.timestamp_column] < train_end
        if args.train_months > 0:
            train_start = predict_start - pd.DateOffset(months=args.train_months)
            train_mask &= panel[args.timestamp_column] >= train_start
        else:
            train_start = pd.NaT

        predict_mask = (
            (panel[args.timestamp_column] >= predict_start)
            & (panel[args.timestamp_column] < predict_end)
            & predict_base_mask
        )
        train_rows = panel.loc[train_mask & train_base_mask].copy()
        predict_rows = panel.loc[predict_mask].copy()

        usable_features = _usable_feature_columns(train_rows, feature_columns)
        train_valid = train_rows[args.label_column].notna()
        train_rows = train_rows.loc[train_valid].copy()
        train_timestamp_count = int(train_rows[args.timestamp_column].nunique())
        fold_info: dict[str, Any] = {
            "fold_id": fold_id,
            "train_start": train_start,
            "train_end_exclusive": train_end,
            "predict_start": predict_start,
            "predict_end_exclusive": predict_end,
            "usable_features": usable_features,
            "train_rows": int(len(train_rows.index)),
            "train_timestamps": train_timestamp_count,
            "predict_rows": int(len(predict_rows.index)),
            "status": "skipped",
        }

        if len(train_rows.index) < args.min_train_rows:
            fold_info["skip_reason"] = "insufficient_train_rows"
            fold_rows.append(fold_info)
            continue
        if train_timestamp_count < args.min_train_timestamps:
            fold_info["skip_reason"] = "insufficient_train_timestamps"
            fold_rows.append(fold_info)
            continue
        if len(predict_rows.index) < args.min_predict_rows:
            fold_info["skip_reason"] = "insufficient_predict_rows"
            fold_rows.append(fold_info)
            continue
        if not usable_features:
            fold_info["skip_reason"] = "no_usable_features"
            fold_rows.append(fold_info)
            continue

        pipeline = _build_pipeline(
            model_name,
            model_params,
            random_state=args.random_state + fold_id - 1,
            n_jobs=args.n_jobs,
        )
        pipeline.fit(train_rows[usable_features], train_rows[args.label_column])
        predictions = pipeline.predict(predict_rows[usable_features])

        pred_frame = predict_rows[[args.timestamp_column, args.market_column, args.label_column]].copy()
        pred_frame["prediction"] = predictions.astype(float)
        pred_frame["fold_id"] = fold_id
        pred_frame["train_start"] = train_start
        pred_frame["train_end_exclusive"] = train_end
        pred_frame["predict_start"] = predict_start
        pred_frame["predict_end_exclusive"] = predict_end
        prediction_frames.append(pred_frame)

        fold_info.update(
            {
                "status": "ok",
                "prediction_mean": float(np.nanmean(predictions)) if len(predictions) else float("nan"),
                "label_mean": float(pred_frame[args.label_column].mean()),
                "pearson_ic": _pearson_corr(pred_frame["prediction"], pred_frame[args.label_column]),
                "spearman_ic": _spearman_corr(pred_frame["prediction"], pred_frame[args.label_column]),
            }
        )
        fold_rows.append(fold_info)
        importance_rows.extend(_feature_importance_rows(pipeline, usable_features, fold_id))
        print(
            f"[{fold_id}/{len(fold_starts)}] ok "
            f"train={len(train_rows.index)} predict={len(predict_rows.index)} "
            f"features={len(usable_features)}"
        )

    if not prediction_frames:
        raise SystemExit("no predictions were produced")

    prediction_long = pd.concat(prediction_frames, ignore_index=True)
    prediction_long = prediction_long.sort_values(
        ["fold_id", args.timestamp_column, args.market_column],
        kind="stable",
    )
    prediction_long = prediction_long.drop_duplicates(
        subset=[args.timestamp_column, args.market_column],
        keep="first",
    )
    prediction_column = args.score_name
    prediction_long = prediction_long.rename(columns={"prediction": prediction_column})

    score_wide = prediction_long.pivot(
        index=args.timestamp_column,
        columns=args.market_column,
        values=prediction_column,
    ).sort_index().sort_index(axis=1)
    score_wide.index.name = None

    score_path = out_dir / f"{args.score_name}.parquet"
    score_copy_path = Path(args.score_copy_dir) / f"{args.score_name}.parquet" if args.score_copy_dir else None
    long_path = out_dir / "prediction_long.parquet"
    fold_path = out_dir / "fold_summary.csv"
    ic_path = out_dir / "prediction_ic_by_timestamp.csv"
    importance_path = out_dir / "feature_importance_by_fold.csv"
    summary_path = out_dir / "prediction_summary.json"

    score_wide.to_parquet(score_path)
    if score_copy_path is not None:
        score_copy_path.parent.mkdir(parents=True, exist_ok=True)
        score_wide.to_parquet(score_copy_path)
    prediction_long.to_parquet(long_path, index=False)
    fold_summary = pd.DataFrame(fold_rows)
    fold_summary.to_csv(fold_path, index=False, encoding="utf-8-sig")

    ic_rows = _timestamp_ic_rows(
        prediction_long,
        timestamp_column=args.timestamp_column,
        label_column=args.label_column,
        prediction_column=prediction_column,
        min_ic_count=args.min_ic_count,
    )
    ic_frame = pd.DataFrame(ic_rows)
    ic_frame.to_csv(ic_path, index=False, encoding="utf-8-sig")
    if importance_rows:
        pd.DataFrame(importance_rows).to_csv(importance_path, index=False, encoding="utf-8-sig")

    ok_folds = fold_summary[fold_summary["status"].eq("ok")] if "status" in fold_summary else pd.DataFrame()
    summary = {
        "panel_parquet": str(args.panel_parquet),
        "score_path": str(score_path),
        "score_copy_path": str(score_copy_path) if score_copy_path is not None else "",
        "prediction_long_path": str(long_path),
        "fold_summary_path": str(fold_path),
        "ic_path": str(ic_path),
        "feature_importance_path": str(importance_path) if importance_rows else "",
        "score_name": args.score_name,
        "model": model_name,
        "model_config_json": str(args.model_config_json),
        "model_params": model_params,
        "feature_columns": feature_columns,
        "label_column": args.label_column,
        "train_months": args.train_months,
        "predict_months": args.predict_months,
        "step_months": args.step_months,
        "embargo_bars": args.embargo_bars,
        "train_row_filter": args.train_row_filter,
        "predict_row_filter": args.predict_row_filter,
        "folds_total": int(len(fold_rows)),
        "folds_ok": int(len(ok_folds.index)),
        "predicted_rows": int(len(prediction_long.index)),
        "predicted_timestamps": int(score_wide.shape[0]),
        "predicted_markets": int(score_wide.shape[1]),
        "prediction_start": str(score_wide.index.min()) if len(score_wide.index) else "",
        "prediction_end": str(score_wide.index.max()) if len(score_wide.index) else "",
        "mean_fold_pearson_ic": float(ok_folds["pearson_ic"].mean()) if "pearson_ic" in ok_folds else float("nan"),
        "mean_fold_spearman_ic": float(ok_folds["spearman_ic"].mean()) if "spearman_ic" in ok_folds else float("nan"),
        "mean_timestamp_pearson_ic": float(ic_frame["pearson_ic"].mean()) if "pearson_ic" in ic_frame else float("nan"),
        "mean_timestamp_spearman_ic": float(ic_frame["spearman_ic"].mean()) if "spearman_ic" in ic_frame else float("nan"),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")

    print(f"Wrote score source to {score_path}")
    print(f"Wrote long predictions to {long_path}")
    print(f"Folds ok: {summary['folds_ok']}/{summary['folds_total']}")
    print(f"Rows: {summary['predicted_rows']}")
    print(f"Mean timestamp Spearman IC: {summary['mean_timestamp_spearman_ic']:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
