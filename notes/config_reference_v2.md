# 설정 파일 레퍼런스

연구 config의 주요 옵션, 작성 규칙, 예시를 실제 코드가 읽는 키 기준으로 정리했습니다.

## 기본 구조

grid config는 보통 아래 블록으로 구성한다.

```json
{
  "candle_dir": "data/stocks/daily",
  "source_cache_dir": "data/stocks_cache/kr_etf_daily",
  "out_dir": "data/grid/example_v2",
  "run_name_template": "example_{top_n}_v2",
  "compute_rolling_ir": false,
  "print_run_summaries": false,
  "save_run_artifacts": false,
  "shared_feature_spec_template": [],
  "feature_spec_template": [],
  "universe_spec_template": {},
  "weight_spec_template": {},
  "vectorbt_spec_template": {},
  "grid": {},
  "constraints": []
}
```

주요 키:

| 키 | 역할 |
|---|---|
| `candle_dir` | 이전 형식 호환용 가격 디렉터리. 주식/ETF config에서는 보통 `source_cache_dir`가 핵심이다. |
| `source_cache_dir` | wide cache 위치. `trade_price`, `signal_price`, 거래대금, 시총 같은 원천 frame을 여기서 읽는다. |
| `out_dir` | grid 결과 저장 위치. 보통 `summary_results.csv`가 이 아래 생긴다. |
| `run_name_template` | grid 값으로 치환되는 실행 이름. 결과 row 식별에 중요하다. |
| `shared_feature_spec_template` | 모든 grid 조합이 공유하는 피처. 전략 핵심 점수, breadth, 공통 필터는 여기에 두는 편이 좋다. |
| `feature_spec_template` | grid 조합마다 달라지는 피처. 예: `rv_window`, `cutoff`가 바뀌는 볼컷. |
| `universe_spec_template` | 어떤 점수로 어떤 종목을 고를지 정한다. |
| `weight_spec_template` | 선택된 종목을 어떤 비중으로 들고 갈지 정한다. |
| `vectorbt_spec_template` | 백테스트 가격, 수수료, 벤치마크, 시작일을 정한다. |
| `grid` | 치환할 후보값들. 값은 숫자/문자/불리언처럼 hash 가능한 단순값만 권장한다. |
| `constraints` | 잘못된 grid 조합을 제거하는 조건식. |

## Feature Spec

피처는 크게 다섯 방식으로 만든다.

### 1. source + steps

가장 일반적인 형태다. 원천 frame 하나에서 transform을 순서대로 적용한다.

```json
{
  "source": "signal_price",
  "steps": [
    { "kind": "simple_return", "params": { "window": 1 } },
    { "kind": "cross_rank", "params": { "descending": true } },
    { "kind": "calendar_mean", "params": { "freq": "M", "signal_timing": "same_period" } },
    { "kind": "calendar_rolling_sum", "params": { "freq": "M", "periods": 9, "skip_periods": 1, "signal_timing": "next_period" } }
  ],
  "column_name": "rank_score_raw_m9_skip1"
}
```

위 예시는 다음 의미다.

`일간 수익률 -> 횡단 랭크 -> 월평균 -> 9개월 합산, 최근 1개월 제외 -> 다음 달 적용`

### 2. compare

이미 만든 피처를 임계값이나 다른 피처와 비교해 0/1 신호를 만든다.

```json
{
  "compare": {
    "left_feature": "return_3m",
    "operator": "gt",
    "right_value": 0.0
  },
  "column_name": "return_3m_pos"
}
```

지원 연산자는 `gt`, `ge`, `lt`, `le`, `eq`, `ne`다.

### 3. breadth

특정 후보군 안에서 신호가 켜진 비율을 만든다.

```json
{
  "breadth": {
    "driver_feature": "liquidity_prev_month_avg",
    "signal_feature": "return_3m_pos",
    "mode": "top_n",
    "top_n": 200,
    "ascending": false
  },
  "column_name": "breadth_top200_pos3m_ratio"
}
```

위 예시는 전월 평균 거래대금 상위 200개 안에서 3개월 수익률이 양수인 비율을 만든다.

### 4. components

여러 피처를 가중합으로 합친다.

```json
{
  "components": [
    { "feature_column": "score_a", "weight": 0.7 },
    { "feature_column": "score_b", "weight": 0.3 }
  ],
  "combine": "weighted_sum",
  "column_name": "combined_score"
}
```

### 5. logical / state

여러 0/1 피처를 논리 결합하거나, entry/exit 상태를 유지하는 데 쓴다. 사용할 때는 기존 예시를 확인하고 작은 테스트를 먼저 돌리는 편이 안전하다.

## 지원 source

현재 자주 쓰는 원천 column:

| source | 의미 |
|---|---|
| `trade_price` | 백테스트/거래 기준 가격 |
| `signal_price` | 신호 계산용 가격. 환산가격과 거래가격을 분리할 때 쓴다. |
| `index_price` | 지수 벤치마크 가격 |
| `opening_price`, `high_price`, `low_price` | OHLC 보조 가격 |
| `candle_acc_trade_volume` | 거래량 |
| `candle_acc_trade_price` | 거래대금 |
| `market_cap` | 시가총액 |
| `market_code` | 시장 구분 |
| `price_available`, `marcap_available` | 가격/시총 사용 가능 여부 |
| `custom:fundamental_beginning_total_equity` | 현재 공시 사업연도의 직전 연도말 자기자본. 사업연도가 연속되고 재무제표 유형이 같을 때만 제공 |
| `custom:fundamental_issued_shares` | 결산일 기준 발행주식수. 시장 자료의 주식수와 정확히 일치한 공시 상세행만 사용 |
| `custom:fundamental_treasury_shares` | 발행주식수에서 유통주식수를 차감한 자기주식수 |
| `custom:fundamental_outstanding_shares` | 자기주식을 제외한 유통주식수 |
| `custom:fundamental_treasury_share_ratio` | 자기주식수 / 발행주식수 |
| `custom:fundamental_outstanding_share_ratio` | 유통주식수 / 발행주식수 |
| `custom:fundamental_outstanding_market_cap` | 시가총액에 유통주식 비율을 적용한 시가총액 |
| `custom:fundamental_stock_issuance_available`, `custom:fundamental_outstanding_share_available` | 주식발행 공시 및 유통주식 지표 사용 가능 여부 |
| `custom:fundamental_net_share_supply_latest_fy_change` | 최근 완료 사업연도의 분할조정 유통주식수 전년 대비 변화율 |
| `custom:fundamental_net_share_supply_latest_fy_available` | 연속된 두 사업연도의 유통주식수 변화율 사용 가능 여부 |
| `custom:fundamental_cash_dividend_ttm_amount_proxy` | 주당배당금과 배당기준일 발행주식수로 추정한 최근 달력 12개월 보통주 현금배당 총액 |
| `custom:fundamental_cash_dividend_available` | 금융위 주식배당 원천에서 해당 보통주를 식별할 수 있는지 여부 |
| `custom:fundamental_cash_dividend_latest_fy_amount_proxy` | 기업별 최근 완료 사업연도에 속한 보통주 현금배당 총액 추정치 |
| `custom:fundamental_cash_dividend_latest_fy_available` | 최근 완료 사업연도 배당총액의 지급일·배당기준일 주식수 연결이 완전한지 여부 |

배당수익률은 `fundamental_cash_dividend_ttm_amount_proxy`를
`market_cap`으로 나누어 계산합니다. 주당배당금을 배당기준일의
발행주식수로 환산하므로 주식분할 전후의 주당 단위가 섞이지 않습니다.
사업연도 기준 배당수익률은
`fundamental_cash_dividend_latest_fy_amount_proxy`를 `market_cap`으로
나누어 계산합니다. 사업연도 배당총액은 해당 사업보고서와 그 사업연도의
모든 양수 현금배당 지급일이 확인된 뒤부터 사용합니다. 무배당 사업연도는
사업보고서 제출 다음 거래일부터 `0`으로 반영합니다.
주주환원수익률은 이 배당수익률에서
`fundamental_outstanding_market_cap / trade_price`의 12개월 변화율을
차감하여 구성할 수 있습니다. 배당 캐시는 실제 현금 지급일을 기준으로
하므로 배당 공시일을 추정하지 않습니다.
사업연도 기준 주주환원수익률은 사업연도 기준 배당수익률에서
`fundamental_net_share_supply_latest_fy_change`를 차감하여 구성합니다.
유통주식수가 감소하면 변화율이 음수이므로 주주환원수익률에는 양수로
기여하고, 신규 발행으로 증가하면 음수로 기여합니다. 사업연도
유통주식수는 결산일의 `market_cap / trade_price`에 해당 사업보고서의
유통주식 비율을 적용해 분할조정합니다. 따라서 결산 후 발생한 분할이나
합병을 이전 사업연도의 신규 발행으로 오인하지 않습니다.

## 주요 transform

| transform | 핵심 params | 의미 |
|---|---|---|
| `simple_return` | `window` | `price / price.shift(window) - 1` |
| `momentum` | `window` | 가격 모멘텀 계열 |
| `rolling_mean` | `window` | rolling 평균 |
| `rolling_sum` | `window` | rolling 합 |
| `rolling_std` | `window` | rolling 표준편차 |
| `rolling_percentile` | `window` | 현재 값의 rolling 분위 |
| `downside_vol` | `window`, `annualize` | 하락 수익률만 이용한 변동성 |
| `rolling_drawdown_min` | `window` | rolling 기간 내 최대 낙폭 계열 |
| `efficiency_ratio` | `window` | 추세 효율성 지표 |
| `cross_rank` | `descending` | 같은 날짜의 종목 간 랭크 |
| `cross_percentile` | `descending` | 같은 날짜의 종목 간 percentile |
| `group_demean` | `group_feature`, `min_group_size` | 같은 날짜·그룹의 평균을 각 종목 값에서 차감 |
| `group_rank` | `group_feature`, `descending`, `min_group_size` | 같은 날짜·그룹 안에서 종목별 랭크 계산 |
| `group_percentile` | `group_feature`, `descending`, `min_group_size` | 같은 날짜·그룹 안에서 최상위 0, 최하위 1의 percentile 계산 |
| `calendar_hold` | `freq`, `hold_periods`, `signal_timing`, `anchor`, `broadcast_sparse` | 기간 마지막 값을 N개 기간 동안 유지 |
| `calendar_mean` | `freq`, `signal_timing` | 기간 평균을 일별 frame에 매핑 |
| `calendar_rolling_robust_mean` | `freq`, `periods`, `skip_periods`, `signal_timing`, `mode`, `trim_count` | 기간별 마지막 값의 절사평균 또는 윈저평균 |
| `calendar_rolling_sum` | `freq`, `periods`, `skip_periods`, `signal_timing` | 기간별 마지막 값을 rolling 합산 |
| `period_simple_return` | `freq`, `periods`, `signal_timing` | 월/주/분기 단위 수익률 |
| `corr_greedy_filter` | `threshold`, `corr_window`, `liquidity_window`, `freq` | 상관이 높은 후보를 유동성 기준으로 제거 |
| `residualize_reference` | `reference`, `beta_window` | 기준 자산 베타를 제거한 잔차 |
| `subtract_reference` | `reference` | 기준 피처를 차감 |
| `ratio_to_reference` | `reference` | 기준 피처 대비 비율 |
| `mask_by_feature` | `feature` | 다른 피처가 켜진 곳만 남김 |
| `select_by_state` | `state_<값>_feature`, `default_feature` | 상태값에 따라 사용할 피처를 선택 |
| `clip` | `lower`, `upper` | 값 상하한 제한 |
| `target_vol_weight` | `target_annual_vol`, `periods_per_year`, `min_weight`, `max_weight` | 실현변동성을 목표 연환산 변동성에 맞추는 종목별 비중 배수 |
| `quantize_weight` | `step_size`, `rounding`, `min_weight`, `max_weight` | 연속형 비중 배수를 일정한 단계로 변환 |
| `shift` | `periods` | row 기준 lag |

주의:

- `calendar_mean(freq="M", signal_timing="next_period")`은 월평균을 다음 달 날짜들에 매핑한다. 그래서 결과적으로 한 달 고정처럼 보인다.
- `calendar_rolling_robust_mean`은 `periods - skip_periods`개의 값을 집계한다. `mode="trimmed"`는 양쪽에서 `trim_count`개씩 제외하고, `mode="winsorized"`는 양쪽 극단값을 각각 경계값으로 대체한다.
- `calendar_rolling_sum(freq="M", periods=9, skip_periods=1, signal_timing="next_period")`은 월별 값 9개 합에서 최근 1개월을 제외하고 다음 달에 적용한다.
- `calendar_hold(freq="M", hold_periods=2)`는 월별 마지막 값을 2개월 동안 유지한다. `anchor="calendar"`면 1-2월, 3-4월처럼 달력 기준으로 묶고, `anchor="first_valid"`면 첫 유효 기간부터 묶는다.
- `calendar_hold(..., broadcast_sparse=true)`는 기간 중 일부 날짜에만 값이 있는 피처의 마지막 유효값을 다음 보유 기간 전체에 매핑한다. 기본값은 `false`이며 기존처럼 원본 피처의 결측 마스크를 유지한다.
- `group_demean`, `group_rank`, `group_percentile`의 `group_feature`에는 먼저 정의한 피처 이름을 넣는다. 그룹 피처나 원본 값이 결측이면 결과도 결측이며, 유효 종목 수가 `min_group_size`보다 작은 그룹도 제외한다.
- `rebalance_frequency`는 주문/weight 출력 주기이고, 피처 점수 산출 주기가 아니다.

### 상태별 피처 선택

`select_by_state`는 시장 전체에 같은 상태값을 적용하면서 상태마다 다른 점수 산식을 사용할 때 쓴다.

```json
{
  "source": "regime_state",
  "steps": [
    {
      "kind": "select_by_state",
      "params": {
        "state_0_feature": "momentum_12m_skip1",
        "state_1_feature": "momentum_6m",
        "state_2_feature": "momentum_3m",
        "default_feature": "momentum_12m_skip1"
      }
    }
  ],
  "column_name": "active_momentum_score"
}
```

- 상태값은 숫자여야 하며 `state_<값>_feature`를 필요한 만큼 추가할 수 있다.
- 상태와 선택 대상 피처는 index와 market column을 맞춰 계산한다.
- 상태가 결측이면 결과도 결측이다.
- 매핑되지 않은 상태는 `default_feature`가 있으면 해당 피처를 사용하고, 없으면 결측이다.
- 상태 신호의 시점과 지연은 이 변환이 자동으로 바꾸지 않는다. 월말 정보는 `signal_timing="next_period"` 같은 기존 시점 규칙으로 먼저 확정해야 한다.

### 그룹 피처 예시

```json
[
  {
    "source": "custom:industry_id",
    "column_name": "industry_id"
  },
  {
    "source": "custom:fundamental_roe",
    "steps": [
      {
        "kind": "group_demean",
        "params": {
          "group_feature": "industry_id",
          "min_group_size": 3
        }
      }
    ],
    "column_name": "roe_industry_demeaned"
  },
  {
    "source": "market_cap",
    "steps": [
      {
        "kind": "group_rank",
        "params": {
          "group_feature": "industry_id",
          "descending": true,
          "min_group_size": 3
        }
      }
    ],
    "column_name": "rank_score_within_industry"
  },
  {
    "source": "custom:fundamental_roe",
    "steps": [
      {
        "kind": "group_percentile",
        "params": {
          "group_feature": "industry_id",
          "descending": true,
          "min_group_size": 3
        }
      }
    ],
    "column_name": "roe_percentile_within_industry"
  }
]
```

## Universe Spec

Universe는 “무엇을 후보로 고르고 어떤 필터를 적용할지” 정한다.

```json
{
  "name": "{run_name}",
  "feature_column": "rank_score_corr095_m9_skip1",
  "lag": 0,
  "signal_lag": 0,
  "mode": "top_n",
  "top_n": "{top_n}",
  "ascending": true,
  "allowed_markets_file": "data/kr_etfs/universe/kr_etf_cat24_structure_excluded_markets.json",
  "value_filters": [
    {
      "feature_column": "monthly_individual_rv12d_pct42d",
      "operator": "le",
      "value": 0.8,
      "lag": 0
    }
  ]
}
```

주요 키:

| 키 | 의미 |
|---|---|
| `feature_column` | 정렬/선택 기준 점수 |
| `sort_column` | 정렬 기준을 별도로 둘 때 사용. 없으면 `feature_column` 사용 |
| `lag` | 선택 기준 피처 lag |
| `signal_lag` | 신호 lag. 일반적으로 0을 많이 쓴다. |
| `mode` | `top_n`, `all`, quantile 계열 |
| `top_n` | 상위 N개 |
| `ascending` | 낮은 값이 좋은 점수면 `true`, 높은 값이 좋으면 `false` |
| `allowed_markets`, `allowed_markets_file` | 허용 종목 직접 지정 또는 파일 지정 |
| `excluded_markets`, `excluded_markets_file` | 제외 종목 직접 지정 또는 파일 지정 |
| `value_filters` | 절대 조건 필터 |
| `rank_filters`, `filter_stages` | 시총/거래대금 상위 N 같은 단계형 필터 |

## Weight Spec

Weight는 선택된 후보를 실제 목표 비중으로 바꾼다.

### 동일가중

```json
{
  "weighting": "equal",
  "gross_exposure": 1.0,
  "rebalance_frequency": "change_only",
  "universe_name": "{run_name}"
}
```

### rank-fixed

```json
{
  "weighting": "rank_fixed",
  "rank_weight_feature": "rank_score_corr095_m9_skip1",
  "rank_weight_ascending": true,
  "rank_weights": ["{w1}", "{w2}", "{w3}"],
  "gross_exposure": 1.0,
  "rebalance_frequency": "change_only",
  "universe_name": "{run_name}"
}
```

`rank_fixed`는 선택된 종목을 다시 랭크해서 앞에서부터 고정 비중을 준다. 예를 들어 top2에 `rank_weights=[0.8, 0.2, 0.0]`이면 1등 80%, 2등 20%다.

종목별 상태에 따라 고정 비중을 줄이려면 `position_score_feature`를 배수로 지정한다.

```json
{
  "weighting": "rank_fixed",
  "rank_weight_feature": "rank_score_corr095_m9_skip1",
  "rank_weight_ascending": true,
  "rank_weights": [0.8, 0.2],
  "position_score_feature": "individual_risk_weight",
  "position_score_lag": 0,
  "rebalance_frequency": "change_only"
}
```

최종 비중은 `랭크 고정 비중 × 종목별 배수 × 전체 노출 배수`로 계산한다. 종목별 배수는 기존 transform으로 0~1 범위의 피처를 만들어 사용하며, 결측값은 0으로 처리한다. 예를 들어 배수가 `[0.5, 1.0]`이면 top2의 `[0.8, 0.2]`는 `[0.4, 0.2]`가 되고 나머지 40%는 현금으로 남는다. 선택 종목 수보다 `rank_weights`가 짧으면 오류가 발생한다.

개별 종목의 목표 변동성 배수는 일간 수익률의 rolling 표준편차에 `target_vol_weight`를 적용해서 만든다. 아래 예시는 최근 20일 변동성을 기준으로 연환산 22%를 목표로 하며, 원래 비중의 40~100% 범위에서만 조절한다.

```json
{
  "source": "signal_price",
  "steps": [
    { "kind": "simple_return", "params": { "window": 1 } },
    { "kind": "rolling_std", "params": { "window": 20 } },
    {
      "kind": "target_vol_weight",
      "params": {
        "target_annual_vol": 0.22,
        "periods_per_year": 252,
        "min_weight": 0.4,
        "max_weight": 1.0
      }
    }
  ],
  "column_name": "individual_target_vol_weight"
}
```

`target_vol_weight`는 `target_annual_vol / (rolling_std × sqrt(periods_per_year))`를 계산한다. 입력 변동성이 0이면 `max_weight`, 결측이면 결측을 반환한다. 미래정보 사용을 막으려면 `position_score_lag: 1`처럼 비중 단계에서 지연을 명시한다. 이 기능은 종목별 위험만 조절하며 종목 간 상관관계를 반영한 포트폴리오 전체 변동성 타기팅은 아니다.

연속형 비중 배수의 잦은 미세 조정을 줄이려면 `quantize_weight`를 뒤에 연결한다.

```json
{
  "source": "individual_target_vol_weight",
  "steps": [
    {
      "kind": "quantize_weight",
      "params": {
        "step_size": 0.1,
        "rounding": "nearest",
        "min_weight": 0.5,
        "max_weight": 1.0
      }
    }
  ],
  "column_name": "individual_target_vol_quantized_weight"
}
```

`rounding`은 가장 가까운 단계인 `nearest`, 항상 낮은 단계인 `floor`, 항상 높은 단계인 `ceil`을 지원한다. `min_weight=0.5`, `step_size=0.1`이면 가능한 배수는 `0.5, 0.6, 0.7, 0.8, 0.9, 1.0`이다. 입력 결측은 유지하고 범위를 벗어난 값은 상하한으로 제한한다. 단계 경계를 자주 왕복하면 `change_only`에서도 거래가 발생하므로 이 변환 자체가 히스테리시스를 제공하지는 않는다.

### 포트폴리오 변동성 타기팅

선택된 종목들의 변동성과 상관관계를 함께 반영해 포트폴리오 전체 비중을 줄일 때 사용한다. 기존 `top_n`, `equal` 또는 `rank_weights`, 종목별 `position_score_feature`, `gross_exposure_feature`가 만든 기본 비중을 그대로 사용하므로 종목 수와 비중을 다시 지정하지 않는다.

```json
{
  "weighting": "rank_fixed",
  "rank_weight_feature": "rank_score",
  "rank_weight_ascending": true,
  "rank_weights": [0.8, 0.2],
  "rebalance_frequency": "change_only",
  "portfolio_vol_target": {
    "price_feature": "signal_price",
    "window": 40,
    "target_annual_vol": 0.30,
    "periods_per_year": 252,
    "min_multiplier": 0.5,
    "max_multiplier": 1.0,
    "step_size": 0.1,
    "rounding": "floor",
    "min_valid_ratio": 0.9,
    "min_pair_obs_ratio": 0.8
  }
}
```

각 시점의 기본 비중 벡터를 `w`, 선택 종목의 최근 수익률 공분산 행렬을 `Σ`라고 하면 예상 연환산 변동성은 `sqrt(w'Σw × periods_per_year)`로 계산한다. 최종 비중은 기본 비중에 `target_annual_vol / 예상 연환산 변동성`을 곱한 뒤 `min_multiplier`와 `max_multiplier` 범위로 제한한다. `max_multiplier`는 1 이하만 허용하므로 이 옵션은 기존 비중을 늘리지 않고 감액만 한다.

| 키 | 기능 |
|---|---|
| `price_feature` | 공분산 계산에 사용할 가격 피처 |
| `window` | 일간 수익률 공분산 계산 구간 |
| `target_annual_vol` | 목표 연환산 변동성 |
| `periods_per_year` | 연환산 기간 수 |
| `min_multiplier`, `max_multiplier` | 전체 비중에 적용할 배수의 하한과 상한 |
| `step_size` | 0이면 연속형, 양수이면 단계형 배수 |
| `rounding` | 단계형 배수의 `floor`, `nearest`, `ceil` 방식 |
| `min_valid_ratio` | 종목별 최소 수익률 관측 비율 |
| `min_pair_obs_ratio` | 종목 쌍별 최소 동시 관측 비율 |

계산 시점의 가격은 사용하지 않고 직전 행까지의 수익률만 사용한다. 필요한 관측치나 유효한 공분산을 확보하지 못하면 추가 감액을 하지 않고 `max_multiplier`를 적용한다. 배수는 매 행 다시 계산하며, `change_only`에서는 최종 비중이 달라진 행만 출력한다. `step_size`를 사용하면 작은 변동에 따른 잦은 비중 변경을 줄일 수 있다.

### gross exposure feature

시장 필터로 전체 노출을 0 또는 1로 조절할 때 쓴다.

```json
{
  "gross_exposure_feature": "risk_on_breadth_top200_pos3m_gt45",
  "gross_exposure_lag": 0,
  "gross_exposure_clip_min": 0.0,
  "gross_exposure_clip_max": 1.0
}
```

`gross_exposure_feature`가 0이면 선택 종목이 있어도 전체 목표 비중이 0으로 줄어든다.

### defensive_sleeve

위험 회피 시 현금이 아니라 방어자산으로 전환할 때 쓰는 옵션이다.

```json
{
  "defensive_sleeve": {
    "risk_on_feature": "risk_on_feature_name",
    "markets": ["TLT", "UGL"],
    "risk_on_lag": 0,
    "weighting": "equal",
    "gross_exposure": 1.0,
    "listed_feature": "trade_price"
  }
}
```

이 옵션은 방어자산이 같은 universe columns 안에 있어야 한다.

## VectorBT Spec

```json
{
  "price_column": "trade_price",
  "periods_per_year": 252,
  "fees": 0.0005,
  "slippage": 0.0,
  "benchmark_market": "PORTFOLIO_GROUP_REBALANCE",
  "benchmark_mode": "portfolio_group_rebalance",
  "benchmark_rebalance_frequency": "monthly",
  "benchmark_listed_normalize": true,
  "trim_start_mode": "timestamp",
  "trim_start_timestamp": "2008-03-03 00:00:00"
}
```

`periods_per_year`는 CAGR, Sharpe, Sortino, Calmar, Martin과 벤치마크
성과지표에 공통으로 적용할 연간 bar 수다. 생략하면 일봉은 `252`, 분봉은
`365 × 일중 bar 수`를 사용한다. 따라서 24시간 암호화폐 60분봉은
`8760`으로 계산된다. 다른 거래일·거래시간 체계는 실제 연간 관측 수를
명시한다. 전략과 벤치마크에는 항상 같은 값이 적용된다.

주요 키:

| 키 | 의미 |
|---|---|
| `price_column` | 백테스트 수익률 계산 가격 |
| `fees` | 거래 수수료 |
| `slippage` | 슬리피지 |
| `benchmark_market` | 단일 벤치마크 종목 또는 portfolio group benchmark 이름 |
| `benchmark_mode` | `single_market`, `portfolio_group_rebalance` 등 |
| `benchmark_rebalance_frequency` | 포트폴리오형 벤치마크 리밸런싱 주기 |
| `benchmark_listed_normalize` | 상장된 종목만으로 벤치마크 비중 정규화 |
| `benchmark_price_column` | 벤치마크 전용 가격 column을 따로 쓸 때 |
| `benchmark_source_cache_dir` | 단일 종목 벤치마크를 전략과 다른 wide cache에서 읽을 때. 생략하면 최상위 `source_cache_dir`를 사용한다. |
| `trim_start_mode` | 시작점 자르기 방식 |
| `trim_start_timestamp` | 고정 시작일 |

벤치마크 상대지표는 전략과 벤치마크에 실제 값이 모두 존재하는 공통 관측기간에서 계산합니다. 전략의 전체기간 CAGR, MDD 등 절대지표는 전략 자체의 전체기간을 유지하며, 요약에는 `Benchmark Comparison Start`, `Benchmark Comparison End`, `Benchmark Comparison Observations`가 기록됩니다.

전략 유니버스에 없는 지수나 ETF를 벤치마크로 사용할 때는 벤치마크 캐시를 별도로 지정한다.

```json
{
  "price_column": "trade_price",
  "benchmark_mode": "single_market",
  "benchmark_source_cache_dir": "data/stocks_cache/kr_stock_daily",
  "benchmark_price_column": "index_price",
  "benchmark_market": "KOSPI"
}
```

이 설정에서 전략 가격과 피처는 최상위 `source_cache_dir`에서 읽고, KOSPI 가격만 `kr_stock_daily/index_price.parquet`에서 읽는다. 벤치마크 캐시는 성과 비교에만 사용되며 전략의 투자 후보나 피처 계산에는 들어가지 않는다. 백테스트가 저장한 `benchmark_curve.csv`는 불확실성 분석 등 후속 검증에서도 그대로 재사용한다.

## Grid 작성 규칙

`grid`에는 단순값만 넣는 것이 안전하다.

좋은 예:

```json
{
  "grid": {
    "top_n": [1, 2, 3],
    "weight_label": ["top1", "top2_8020"],
    "w1": [1.0, 0.8],
    "w2": [0.0, 0.2]
  }
}
```

피해야 할 예:

```json
{
  "grid": {
    "rank_weights": [[0.8, 0.2], [0.7, 0.3]]
  }
}
```

list를 grid 값으로 넣으면 결과 요약의 groupby나 plateau 계산에서 `unhashable type: 'list'`가 날 수 있다. 비중은 `w1`, `w2`, `w3`처럼 분해하고 `constraints`로 올바른 조합만 남긴다.

```json
{
  "constraints": [
    "(weight_label == 'top1' and top_n == 1 and w1 == 1.0 and w2 == 0.0) or (weight_label == 'top2_8020' and top_n == 2 and w1 == 0.8 and w2 == 0.2)"
  ]
}
```

## 예시 1: 기본 월별 랭크모멘텀

```json
{
  "candle_dir": "data/stocks/daily",
  "source_cache_dir": "data/stocks_cache/kr_etf_daily",
  "out_dir": "data/grid/example_rank9_topn_v2",
  "run_name_template": "example_rank9_top{top_n}_v2",
  "shared_feature_spec_template": [
    {
      "source": "signal_price",
      "steps": [
        { "kind": "simple_return", "params": { "window": 1 } },
        { "kind": "cross_rank", "params": { "descending": true } },
        { "kind": "calendar_mean", "params": { "freq": "M", "signal_timing": "same_period" } },
        { "kind": "calendar_rolling_sum", "params": { "freq": "M", "periods": 9, "skip_periods": 1, "signal_timing": "next_period" } }
      ],
      "column_name": "rank_score_m9_skip1"
    }
  ],
  "universe_spec_template": {
    "name": "{run_name}",
    "feature_column": "rank_score_m9_skip1",
    "lag": 0,
    "mode": "top_n",
    "top_n": "{top_n}",
    "ascending": true
  },
  "weight_spec_template": {
    "weighting": "equal",
    "gross_exposure": 1.0,
    "rebalance_frequency": "change_only",
    "universe_name": "{run_name}"
  },
  "vectorbt_spec_template": {
    "price_column": "trade_price",
    "fees": 0.0005,
    "slippage": 0.0,
    "benchmark_market": "PORTFOLIO_GROUP_REBALANCE",
    "benchmark_mode": "portfolio_group_rebalance",
    "benchmark_rebalance_frequency": "monthly",
    "benchmark_listed_normalize": true
  },
  "grid": {
    "top_n": [1, 2, 5]
  }
}
```

## 예시 2: breadth로 전체 노출 조절

```json
{
  "shared_feature_spec_template": [
    {
      "source": "signal_price",
      "steps": [
        { "kind": "period_simple_return", "params": { "freq": "M", "periods": 3, "signal_timing": "next_period" } }
      ],
      "column_name": "return_3m"
    },
    {
      "compare": { "left_feature": "return_3m", "operator": "gt", "right_value": 0.0 },
      "column_name": "return_3m_pos"
    },
    {
      "source": "candle_acc_trade_price",
      "steps": [
        { "kind": "calendar_mean", "params": { "freq": "M", "signal_timing": "next_period" } }
      ],
      "column_name": "liquidity_prev_month_avg"
    },
    {
      "breadth": {
        "driver_feature": "liquidity_prev_month_avg",
        "signal_feature": "return_3m_pos",
        "mode": "top_n",
        "top_n": 200,
        "ascending": false
      },
      "column_name": "breadth_top200_pos3m_ratio"
    },
    {
      "compare": { "left_feature": "breadth_top200_pos3m_ratio", "operator": "gt", "right_value": 0.45 },
      "column_name": "risk_on_breadth45"
    }
  ],
  "weight_spec_template": {
    "weighting": "equal",
    "gross_exposure": 1.0,
    "gross_exposure_feature": "risk_on_breadth45",
    "gross_exposure_lag": 0,
    "gross_exposure_clip_min": 0.0,
    "gross_exposure_clip_max": 1.0,
    "rebalance_frequency": "change_only",
    "universe_name": "{run_name}"
  }
}
```

실제 config에서는 위 feature들을 기존 rank score feature와 함께 넣어야 한다. 이 예시는 breadth 블록만 보여준다.

## 예시 3: 월별 고정 개별 변동성 컷

```json
{
  "feature_spec_template": [
    {
      "source": "signal_price",
      "steps": [
        { "kind": "simple_return", "params": { "window": 1 } },
        { "kind": "rolling_std", "params": { "window": "{rv_window}" } },
        { "kind": "rolling_percentile", "params": { "window": "{rv_pct_window}" } },
        { "kind": "calendar_mean", "params": { "freq": "M", "signal_timing": "next_period" } }
      ],
      "column_name": "monthly_individual_rv{rv_window}d_pct{rv_pct_window}d"
    }
  ],
  "universe_spec_template": {
    "value_filters": [
      {
        "feature_column": "monthly_individual_rv{rv_window}d_pct{rv_pct_window}d",
        "operator": "le",
        "value": "{rv_cutoff}",
        "lag": 0
      }
    ]
  },
  "grid": {
    "rv_window": [12, 16],
    "rv_pct_window": [42, 63],
    "rv_cutoff_label": ["p80", "p84"],
    "rv_cutoff": [0.8, 0.84]
  },
  "constraints": [
    "(rv_cutoff_label == 'p80' and rv_cutoff == 0.8) or (rv_cutoff_label == 'p84' and rv_cutoff == 0.84)"
  ]
}
```

주의: 위 `universe_spec_template`는 부분 예시다. 실제 config에서는 `name`, `feature_column`, `mode`, `top_n` 등 기본 universe 키가 같이 있어야 한다.

## 예시 4: 월별 점수 2개월 유지

이미 월별로 계산된 점수를 2개월 동안 유지하려면 최종 점수 뒤에 `calendar_hold`를 붙인다.

```json
{
  "source": "rank_score_raw_m9_skip1",
  "steps": [
    {
      "kind": "calendar_hold",
      "params": {
        "freq": "M",
        "hold_periods": 2,
        "signal_timing": "same_period",
        "anchor": "calendar"
      }
    }
  ],
  "column_name": "rank_score_raw_m9_skip1_hold2m"
}
```

주의:

- 앞 단계에서 이미 `calendar_rolling_sum(..., signal_timing="next_period")`를 썼다면 `calendar_hold`는 보통 `signal_timing="same_period"`를 쓴다. 둘 다 `next_period`로 두면 한 달 더 늦어질 수 있다.
- 원천 일별 값을 바로 월별로 확정하고 다음 기간부터 N개월 유지하려면 `calendar_hold`에서 `signal_timing="next_period"`를 쓸 수 있다.
- 이 기능은 점수 자체를 덜 자주 바꾸는 기능이다. 주문만 덜 자주 내는 `rebalance_frequency="quarterly"`와 다르다.

## Live profile과 grid 조합

라이브 profile은 보통 연구 config 전체를 다시 쓰지 않고, 연구 config와 특정 grid context를 연결한다.

```json
{
  "name": "kr_etf_example_live",
  "strategy": {
    "config_json": "configs/stocks/grid_kr_etf_cat24_rank9_top_weight_breadth_validation_v2.json",
    "source_cache_dir": "data/stocks_cache/kr_etf_daily",
    "feature_tail_rows": 1000,
    "live_extend_to_as_of": true,
    "live_extend_max_stale_days": 7,
    "live_extend_weekdays_only": true,
    "context": {
      "weight_label": "top2_w8020",
      "top_n": 2,
      "w1": 0.8,
      "w2": 0.2,
      "w3": 0.0,
      "breadth_label": "breadth200_gt45",
      "risk_feature": "risk_on_breadth_top200_pos3m_gt45"
    }
  }
}
```

라이브로 연결하기 전에 확인할 것:

- `context`가 grid/constraints에서 실제 valid 조합인지 확인한다.
- 연구 config의 `source_cache_dir`와 live profile의 `source_cache_dir`가 일치하는지 확인한다.
- live에서 필요한 원천 column이 cache에 있는지 확인한다.
- `change_only` 전략은 수동매매 후에도 다음 실행에서 현재 보유와 목표 비중을 다시 비교하는지 preview로 확인한다.

## 작성 체크리스트

- 새 전략은 먼저 기존 transform/config로 표현 가능한지 확인한다.
- 전용 스크립트보다 범용 config 옵션을 우선한다.
- 새 transform이 필요하면 `features_v2.py`에 추가해야 하며, 기존 config가 그 transform을 쓰지 않으면 기존 동작은 바뀌지 않아야 한다.
- `calendar_mean`과 `rebalance_frequency`를 혼동하지 않는다.
- `grid`에는 list/dict 값을 직접 넣지 않는다.
- `rank_weights`는 `w1`, `w2`, `w3`처럼 나눠서 넣고 `constraints`로 묶는다.
- 일별 필터를 월별 전략에 붙이면 거래가 과도하게 늘 수 있다. 월별 전략에는 `calendar_mean(freq="M", signal_timing="next_period")` 같은 고정화가 필요한지 먼저 검토한다.
- 최종 후보는 `summary_results.csv`뿐 아니라 실제 `weights.csv`나 artifacts로 선택 종목과 거래 빈도를 확인한다.
