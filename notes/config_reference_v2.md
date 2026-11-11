# 설정 파일 레퍼런스

v2 config의 공통 옵션, 기본값, 지원 범위, 제약과 작성 예시를 실제 코드가 읽는 키 기준으로 정리했습니다.

기존 기능을 조합한 실험의 후보·기간·실행 명령·검증 결과는 해당 config의 연구 메모와 연구 문서에서 관리합니다. 이 문서에는 옵션 사용법에 필요한 짧은 예시를 둡니다.

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
  "artifact_format": "parquet",
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
| `save_run_artifacts` | `true`이면 후보별 설정·요약·자산곡선과 선물 거래/주문/계좌 기록을 저장한다. 기본 `false`. 연도별 수익률·집중도 등 작은 집계표는 이 값과 관계없이 저장한다. |
| `artifact_format` | `parquet`(기본) 또는 `csv`. 큰 자산곡선·계좌 기록의 저장 형식이며, Parquet는 Zstd로 압축한다. |
| `shared_feature_spec_template` | 모든 grid 조합이 공유하는 피처. 전략 핵심 점수, breadth, 공통 필터는 여기에 두는 편이 좋다. |
| `feature_spec_template` | grid 조합마다 달라지는 피처. 예: `rv_window`, `cutoff`가 바뀌는 볼컷. |
| `universe_spec_template` | 어떤 점수로 어떤 종목을 고를지 정한다. |
| `weight_spec_template` | 선택된 종목을 어떤 비중으로 들고 갈지 정한다. |
| `vectorbt_spec_template` | 기존 백테스트 설정 이름. 가격, 수수료, 벤치마크, 시작일을 정한다. |
| `backtest_spec_template` | 같은 설정의 엔진 공통 이름. `vectorbt_spec_template`와 동시에 지정하지 않는다. |
| `grid` | 치환할 후보값들. 값은 숫자/문자/불리언처럼 hash 가능한 단순값만 권장한다. |
| `constraints` | 잘못된 grid 조합을 제거하는 조건식. |

`backtest_spec_template.prune_inactive_price_columns`는 `true`, `false`, `"auto"`를 받는다.
기본 `"auto"`는 명시한 `allowed_markets`가 없을 때 켜진다. 켜면 기본 비중·헤지 계산을
마친 뒤 전 기간 비중이 0/결측인 종목을 목표 비중표와 가격 로딩 대상에서 함께 제외한다.
한 번이라도 비중이 있었던 종목은 이후 0 청산 지시까지 유지한다. WF는 해당 IS/OOS
평가 구간의 비중으로 판단하며, 시작일로 다시 자를 때도 제거한 열을 되살리지 않는다.
헤지 자산은 유지한다. 전부 현금인 경우에는 엔진 입력이 빈 종목 집합이 되지 않도록
원래 비중표의 한 종목을 0 비중으로 남긴다. 벤치마크용 가격 로딩은 별도로 유지하며,
이 처리가 캐시 파일이나 원래 종목 선택·배분을 변경하지는 않는다.

`artifact_format="parquet"`는 후보별 `equity_curve`, `benchmark_curve`, `excess_equity_curve`,
선택한 `rolling_information_ratio`와 선물 `futures_account`를 `.parquet`로 저장한다.
소수점 반올림이나 dtype 축소는 하지 않는다. `summary_results.csv`, 후보별 `summary.csv`,
설정 JSON, `futures_trades.json`, 작은 `futures_orders.csv`는 기존 형식을 유지한다.
`save_run_artifacts=false`인 그리드는 상세 파일을 생성하지 않는다.
새 탐색용 그리드 config도 이 기본값을 유지한다. 후보 전체는 요약 수치와 아래 집계표를 저장하고,
상세 분석이 필요한 후보를 다시 실행할 때 `true`를 명시한다. `false`로 바꿔도
이미 저장된 상세 결과를 자동 삭제하지는 않는다.

단일 v2 백테스트와 cross-section grid는 실행 종료 후 메모리에 있는 자산곡선과 거래 결과에서
다음 집계 파일을 자동 저장한다. grid에서는 후보별 `backtest` 폴더에 저장하며
`save_run_artifacts=false`여도 생성한다. 계산 엔진·비중·체결 결과를 바꾸거나 백테스트를
다시 실행하지 않는다. 큰 곡선·피처·주문·전체 거래 파일의 저장 여부는 기존 옵션이 담당한다.

- `annual_returns.csv`: UTC 연도별 수익률·시작/종료 자산·실제 관측 기간. 첫해는 초기자금,
  이후 해는 직전 연말 자산을 분모로 써서 연초 이전 보유 포지션의 평가 변동도 포함한다.
- `asset_contribution.csv`, `side_contribution.csv`: 전체 기간 종목별·롱숏별 순손익과
  전체 순이익 대비 기여 비중. 연도별 롱숏 평가손익을 분해한 결과와는 별개다.
- `top_trades.csv`: 이익이 난 거래 중 순손익 상위 최대 10건. 전체 거래 원본은 저장하지 않는다.
- `analysis_summary.json`: 상위 1·5·10종목/거래 집중도, 양의 이익 합계 대비 집중도,
  롱숏 기여, 거래 손익과 계좌 손익의 차이·일치 여부 및 산식 설명.

같은 집중도·기여 지표는 grid의 `summary_results.csv`와 단일 실행의 `summary.csv`에도 추가한다.
`Analysis Directory`에서 집계 폴더를 찾을 수 있다. 순이익 대비 비중은 손실 종목·거래도 분모에
포함하므로 100%를 넘을 수 있고, 전체 거래 순손익이 0 이하이면 비율을 결측(JSON `null`)으로 둔다.
별도 양의 이익 대비 비중은 종목별 순손익 또는 거래별 순손익 중 양수만 합산한 분모를 사용한다.
선물 엔진의 거래 순손익에 포함된 수수료·펀딩·청산비용·보험 보전을 다시 차감하지 않는다.
VectorBT는 엔진의 거래 기록과 아직 열린 거래의 평가손익을 사용하고 이 차이를 JSON에 기록한다.
집계 금액 단위는 해당 계좌 자산과 같다. 파일을 다시 실행하면 같은 이름으로 덮어쓴다.

단일 v2 백테스트도 기본값은 `parquet`이며, CSV가 필요하면 `--artifact-format csv`를 지정한다.
그리드는 `artifact_format="csv"`로 CSV를 선택할 수 있다.
`--save-excess-returns-csv` / `--save-rolling-ir-csv`로 켠 선택 출력도 지정한 형식을 따른다.
이 옵션은 target weight 출력 형식과 별개다.

후보별 `summary.csv`에는 `Artifact Format`을 기록하며, 모든 상세 파일을 쓴 뒤 요약을 저장한다.
불확실성·전략 혼합·국면 분석의 자산곡선 로더는 이 형식을
우선하여, 같은 폴더에 남은 이전 CSV를 새 Parquet 결과로 오인하지 않는다. 명시한 현재 형식의
파일이 없으면 오류로 중단하며 과거 파일로 조용히 대체하지 않는다. 이전 요약에 형식 기록이
없으면 CSV/Parquet 파일의 존재 여부로 찾는다.

전략 혼합 분석의 기존 `equity_csv` 키와 국면 분석의 `--strategy-equity-csv` 인자에도
`.parquet` 경로를 지정할 수 있다. 이 로더들은 입력 경로가 `.csv`여도 같은 폴더의
`summary.csv`에 기록된 현재 형식을 우선한다. 따라서 예전 CSV와 새 Parquet가 함께 있어도
현재 결과를 읽는다. 형식 기록이 없는 기존 결과·외부 파일은 지정한 파일을 우선하고,
없을 때만 같은 이름의 다른 형식을 찾는다. 기존 CSV 결과와 명령은 계속 지원한다.
저장 형식을 바꿔도 이전 파일을 자동 삭제하거나 기존 결과 전체를 변환하지는 않는다.

## 기존 grid의 랜덤·TPE 파라미터 탐색

`scripts/run_cross_section_grid_v2.py`에서 기존 `grid`, 전략 템플릿, `constraints`를 그대로 두고
아래 블록만 추가하면 일부 조합을 순차 탐색한다. 별도 실행 스크립트는 필요 없다.

```json
"search": {
  "sampler": "tpe",
  "n_trials": 600,
  "metric": "Sortino Ratio"
}
```

| 옵션 | 기본값 / 의미 |
|---|---|
| `search.sampler` | 생략 또는 `grid`: 기존 전체 그리드. `random`: 미평가 유효 조합 중 균등 선택. `tpe`: 기존 평가 결과를 사용하는 Optuna TPE. |
| `search.n_trials` | `100`. 기존 기록을 포함한 **서로 다른 조합의 유한 점수 평가 누적 목표**. 재개 시 늘릴 수 있다. |
| `search.metric` | `Sortino Ratio`. 기존 `summary_results.csv`의 숫자 지표 이름을 정확하게 지정한다. 예: `CAGR [%]`, `Sharpe Ratio`, `Annualized Information Ratio`. |
| `search.direction` | `maximize`. `minimize`도 지원한다. 낙폭 등은 기존 지표의 부호를 확인하고 선택한다. |
| `search.seed` | `42`. 후보 선택의 재현용 시드. |
| `search.startup_trials` | `20`. TPE가 무작위 초기 탐색에 사용하는 유한 점수 평가 수. |

- `search` 생략 및 `{"sampler":"grid"}`는 기존 전체 그리드 동작이다. 전체 그리드에
  탐색 예산/평가지표를 같이 지정하면 무시하지 않고 오류를 낸다.
- 숫자·문자·불리언·null 후보를 Optuna의 **각 파라미터별 범주형 선택**으로 전달한다.
  숫자 목록도 범주로 다루므로 숫자 간 거리·순서를 가정하지 않는다. 목록 밖의 보간값은 생성하지 않는다.
  동일 후보 중복은 제거하며, `1`과 `true`처럼 값이 같고 타입이 다른 모호한 후보는 거부한다.
- 기존 `constraints`로 유효 후보군을 구성한다. 중복 제안·제약 위반 제안은 FAIL로 기록하고
  백테스트·평가 예산에 포함하지 않는다. TPE가 32회 연속 미평가 유효 조합을 제안하지 못하면
  남은 유효 조합 하나를 무작위 선택하여 **원래 값 그대로** native enqueue/ask/tell로 평가한다.
- 파산 및 정의되지 않은 지표(NaN/inf)는 유효 평가에 포함하지 않는다. 해당 조합은 다시 평가하지 않는다.
  없는 지표 이름, 데이터·엔진 오류는 기록 후 중단하며, 가짜 벌점 점수를 TPE에 넣지 않는다.
  모든 유효 조합을 소진하면 목표 평가 수에 못 미쳐도 `exhausted`로 종료한다.
- 비용·체결·시작/종료 기간·워밍업은 **기존 백테스트 설정을 그대로 따른다**. 자동 학습/평가 분할이나
  공통 시작일 정렬을 추가하지 않는다. 기간이 다른 후보를 비교하지 않으려면 기존 시작일 옵션을 맞춘다.
- `summary_results.csv`의 기존 지표를 유지하고 `Search Trial`(0부터 시작), `Search Score`,
  `Search Status`를 추가한다. 전체 이웃이 없는 탐색에서 `Plateau AIR Mean`은 생성하지 않으며
  탐색 목표로도 지정할 수 없다. `search_best.json`, `search_summary.json`, `search.sqlite3`,
  `search_manifest.json`을 같은 `out_dir`에 저장한다. 최적 후보는 설정된 점수 기준이며 별도 위험 제한은 자동 적용하지 않는다.
- 같은 config와 출력 경로로 다시 실행하면 SQLite에서 이어간다. 실행 중 강제 종료된 미완료 후보는 재시도할 수 있다.
  `n_trials`만 늘릴 수 있으며 후보·전략·비용·기간·지표·시드·코드·원천 데이터·패키지가 달라지면 새 `out_dir`이 필요하다.
  기존 전체 그리드 결과 폴더에 탐색을 켜거나 반대로 바꿔 덮어쓰는 것은 거부한다.
  같은 폴더의 동시 실행도 차단한다.
- 현재는 기존 그리드와 같이 **전체 조합을 먼저 열거**한다. TPE가 줄이는 것은 백테스트 횟수이며,
  거대한 곱집합의 초기 메모리 사용을 없애는 구현은 아니다. 적응형 탐색에서는 조합별 완성 피처/유니버스 캐시를
  평가 후 해제한다. 공통 변환 지표는 아래의 용량 제한 캐시에서 재사용한다.
- Optuna 의존성은 `py -m pip install -r requirements-research.txt`로 설치한다. 옵션을 생략한 기존 전체 그리드는 Optuna를 불러오지 않는다.
- PBO·SPA·WF는 아직 `search` 설정의 탐색 과정/이력을 검증하지 않는다. 기존처럼 전체 `grid` 후보군을
  재구성한다. 해당 명령을 실행한 결과를 TPE 탐색 자체의 표본 외 검증으로 해석하지 않는다.

전체 그리드와 random/TPE는 후보의 다른 조건이 바뀌어도 같은 변환 지표를 재사용한다.
캐시 키에는 실제 입력값·시간 인덱스·종목·변환 설정·상위 의존성을 포함하여, 같은 이름의
지표라도 입력이나 상위 조건이 달라지면 다시 계산한다. 재사용 캐시는 한 번의 그리드 실행 안에서
최대 128 MiB 및 128개 항목을 유지하며 오래 사용하지 않은 항목부터 해제한다. 이 한도는
변환 결과 캐시에만 적용하며 원천 데이터·공유 피처·백테스트 전체의 메모리 한도는 아니다.
새 config 옵션은 필요하지 않다. EMA는 기존 SMA 초기값과 관측된 값 기준의 결측 처리 및
연산 순서를 유지하면서 배열로 계산한다. Freqtrade 연결부도 마크가격·목표비중을 시작 시
배열로 정렬하여 조회하며, 주문·펀딩·증거금·청산·검산 순서를 유지한다.

## 시계열 피처 예측력 분석 v2

`scripts/research/analyze_time_series_features_v2.py`는 기존 wide cache와 v2 피처 계산을 재사용하는 **연속형 상관·회귀 분석기**다. 종목 간 순위를 비교하는 `analyze_cross_section_features_v2.py`와 달리, 각 종목의 시간 흐름에서 피처 수치와 미래 수익률의 관계 및 검증 기간 예측오차를 측정한다. 기존 grid/백테스트/라이브 실행 경로는 변경하지 않는다. 반복 사용을 위한 범용 도구이며 특정 전략용 임시 스크립트가 아니다.

BTC 5분봉 기본 실행:

```powershell
py scripts/research/analyze_time_series_features_v2.py --source-cache-dir data/futures/binance/BTCUSDT/5m --out-dir data/research/time_series_features_v2/btc_5m_continuous
```

기본 분석:

- 특정 가격 이평 조합이나 상승/하락 추세 규칙을 기본 가정으로 두지 않고, 최근 등락률·변동성·상대 거래량으로 시장 상태를 측정한다.
- 측정 기간은 약 **15분·1시간·4시간·24시간**이다. 봉 간격으로 올림하고, 표준편차 계산을 위해 최소 2봉으로 제한하며 중복 기간은 제거한다. 5분봉에서는 3 / 12 / 48 / 288봉이다. 이는 사전에 정한 시간 규모이며 최적 기간을 자동 탐색하는 그리드가 아니다.
- `past_return_N`: `종가[t] / 종가[t-N] - 1`. 최근 N봉 등락률.
- `realized_vol_N`: 최근 N개의 한 봉 단순 수익률의 표준편차(`ddof=0`). 연율화하지 않은 한 봉 수익률 변동성이다.
- `relative_volume_N`: 현재 봉 거래량 / 직전 N개 봉 평균 거래량. **분모에서 현재 봉을 제외**한다. 1은 직전 평균과 같은 거래량이다.
- 피처를 분위·고정 임계값으로 나누지 않고 **수치 전체를 연속형 입력으로 사용**한다. 5분봉 기본값은 12개 피처 × 3개 예측 길이 = 36개 관계이며, 탐색/검증 각각 36행을 저장한다. 피처 조합을 전수 탐색하거나 매매 진입선을 최적화하지 않는다.
- 각 피처에 대해 Pearson·Spearman 상관계수를 표시하고, **절편이 있는 단변량 선형회귀**를 별도로 적합한다. 다른 피처의 영향을 통제하는 다변량 회귀나 비선형 곡선 모델은 아니다.
- 종가 시점 t까지 알려진 피처로 **다음 봉 시가 t+1부터 t+1+h 시가까지**의 단순 가격 수익률을 설명/예측한다. 기본 예측 길이는 약 15분·1시간·4시간이며, 봉 간격으로 올림하고 중복 길이는 제거한다. 5분봉은 3 / 12 / 48봉이다.

| CLI 옵션 | 기본값과 의미 |
|---|---|
| `--source-cache-dir` | 필수. `opening_price` 및 피처 원천 parquet가 있는 wide cache. |
| `--out-dir` | 필수. 결과 폴더. 같은 폴더로 다시 실행하면 같은 이름의 결과 파일을 갱신한다. 비교 실험은 다른 출력 폴더를 사용한다. |
| `--market-columns` | 생략 시 캐시의 모든 종목. 쉼표로 종목 선택 가능. |
| `--feature-spec-json` | 선택. 기존 v2 Feature Spec JSON list. 생략 시 각 측정 기간의 등락률·변동성·상대 거래량과 중간 계산용 피처를 구성한다. |
| `--features` | 분석할 resolved 피처 이름 목록. 기본값은 내장 시장 상태 피처 전체(중간 계산 제외) 또는 custom spec의 모든 출력. 예: `past_return_12,realized_vol_12,relative_volume_12`. |
| `--lookback-windows` | 쉼표로 구분한 측정 봉 수, 모두 >= 2. 생략 시 약 15분 / 1시간 / 4시간 / 24시간. 예: `3,12,48,288`. custom spec과 동시에 지정하지 않으며, custom 기간은 JSON에서 정한다. 기존 이평용 `--fast-window`, `--slow-window`, `--volume-window`는 제거했다. |
| `--forward-horizons` | 예: `3,12,48`. 봉 수이며, 생략 시 위의 시간 기준 기본값. |
| `--holdout-fraction` | `0.3`. 시간순 앞 70% 탐색, 뒤 30% 검증. |
| `--split-date` | 선택. 검증 시작 신호 시각 UTC. 지정하면 fraction 대신 적용하며 실제 첫 해당 봉으로 올림한다. |
| `--block-bars` | `0`: max(약 24시간 봉 수, 최장 예측 길이 × 2, 최장 내장 측정 기간 × 2). 5분봉 기본값은 576봉(48시간). 직접 지정 시 최장 예측 길이보다 커야 한다. 자동값은 휴리스틱이며 최적 길이 추정 결과가 아니다. custom spec 기간은 자동 추정하지 않으므로 의존성에 맞게 직접 지정할 수 있다. |
| `--bootstrap-reps` | `4999`. 최소 `199`. 결과 p값의 최소 해상도는 `1 / (reps + 1)`이므로 많은 가설을 보정할 때 검정력이 제한될 수 있다. |
| `--min-count` | `100`(최소 3). 탐색 회귀 적합 및 각 분석 기간에 필요한 유효 피처·라벨 쌍의 최소 수. |
| `--min-blocks` | `20`(최소 2). 유효 관측이 있는 시간 블록 수의 하한. |
| `--seed` | `20261030`. 부트스트랩 난수 시드. |

통계 처리와 해석:

- 탐색 표본은 최장 예측 길이에 대해 종료 시가가 검증 시작 봉보다 앞선 경우만 사용한다. 경계의 `최장 horizon + 1`개 신호를 제외하므로 미래 수익률이 검증 기간과 겹치지 않는다. 검증 피처의 과거 계산에는 탐색 구간 데이터를 사용할 수 있다.
- 내장 피처는 후행 계산이다. custom spec도 허용된 후행 transform만 사용할 수 있고, 음수 shift/periods, HMM 같은 전 구간 학습 transform, calendar transform, cross-market source는 거부한다.
- 피처·예측 길이별 유효 탐색 표본에서 피처 평균/표준편차(`ddof=0`)를 구하고 `z = (x - 탐색 평균) / 탐색 표준편차`로 표준화한다. 탐색에서 `예측 수익률 = 절편 + 기울기 × z`를 적합한 뒤, **평균·표준편차·절편·기울기를 모두 고정하여 검증 기간을 예측**한다. 검증에서 다시 학습하지 않는다.
- 각 기간의 `local_slope_pct_per_discovery_sd`는 해당 기간의 관계를 설명하기 위해 별도로 계산한 진단 기울기다. 검증의 local slope는 실제 예측 계수로 사용하지 않는다. `frozen_slope_pct_per_discovery_sd`가 고정한 탐색 계수다. 단위는 피처가 탐색 표준편차 1만큼 증가할 때 미래 가격 수익률의 퍼센트포인트 변화다.
- 예측 기준선은 **같은 탐색 표본의 평균 미래 수익률을 항상 예측**하는 모델이다. 검증 평균으로 기준선을 다시 맞추지 않으며, 회귀와 기준선 모두 같은 검증 관측에서 오차를 측정한다.
- `mse_gain_bps2 = (기준선 MSE - 고정 회귀 MSE) × 10^8`. 양수면 회귀의 평균제곱오차가 작다. 단위는 수익률 bp의 제곱이며, 매매 수익률 bp가 아니다. `r2_vs_discovery_mean = 1 - 회귀 SSE / 기준선 SSE`도 양수면 개선, 음수면 악화다. 일반적인 검증 평균 중심 R²와 기준선이 다르다.
- 시계열을 고정 길이의 연속 블록으로 나누고 **피처·미래 수익률·기준선/회귀 오차를 같은 블록으로 함께 재표집**한다. 마지막 짧은 블록도 포함한다. 이는 non-overlapping paired block bootstrap이며 기존 전략 수익률 uncertainty 스크립트의 stationary bootstrap과는 다르다.
- local 회귀 기울기는 0과 다른지 양측 검정한다. 고정 회귀의 검증 MSE 개선은 0보다 큰지 단측 검정한다. pointwise 95% percentile 신뢰구간과 centered bootstrap p값을 사용하며, 신뢰구간 자체는 다중검정 보정 구간이 아니다. 예측오차의 불확실성은 탐색 모델에 조건부이며 부트스트랩에서 예측 계수를 다시 맞추지 않는다. 탐색 구간 MSE 개선은 학습 성능이므로 유의성 검정을 하지 않는다.
- Pearson·Spearman은 기술통계로 제공한다. 별도의 IID 상관 p값을 사용하지 않고, Spearman도 순위 고정 부트스트랩을 정확한 검정인 것처럼 제공하지 않는다. 현재 통계 검정의 대상은 **선형 기울기와 검증 예측오차 개선**이다. 비선형 관계가 없다는 결론을 내리는 검정이 아니다.
- 모든 종목 × 피처 × 예측 길이를 묶어 Benjamini–Yekutieli(BY) FDR 보정한다. 탐색은 기울기만, 검증은 기울기와 MSE 개선을 합친 가설군으로 보정한다. 기본 탐색 36개/검증 72개 가설이며, 표본 부족 가설도 p=1로 가설군 크기에 포함한다. `qvalue_by < 0.05`가 다중검정 기준이다.
- 표본 수와 시간 블록 수 부족, 탐색/분석 기간의 상수 피처 또는 불안정한 재표집 결과는 `status`로 표시하고 유의확률을 비워 둔다. 겹친 관측 수를 독립 거래 수로 해석하지 않는다.
- 블록 방식도 충분히 안정적이고 약한 의존성을 가진 데이터라는 가정이 필요하다. 다른 블록 길이·연도별 결과를 함께 확인한다. 이미 연구에 사용한 과거 데이터의 시간 분리는 완전히 손대지 않은 신규 검증 데이터가 되는 것은 아니다.

출력:

| 파일 | 내용 |
|---|---|
| `summary.csv` | 피처·예측 길이·기간별 표본 수, Pearson/Spearman 상관, local/frozen 기울기, 기준선/회귀 MSE와 차이, 기준선 대비 R², 기울기·검증 MSE 개선의 신뢰구간/p값/BY q값, 시간 블록 수와 상태. 내장 분석은 `feature_family`, `lookback_bars`, `lookback_time`으로 측정 종류와 기간을 표시한다. |
| `yearly_summary.csv` | 신호 연도별 상관·local 기울기·고정 회귀의 오차에 대한 기술통계. 예측 모델의 재학습이나 연도별 유의성 검정을 하지 않는다. |
| `metadata.json` | `analysis_mode=continuous`, 내장/custom `feature_mode`, 실행 옵션, 피처 정의·spec, 캐시 SHA256, 분석 범위·분리 시각, 피처/예측 길이별 `discovery_models`(평균·표준편차·절편·기울기), 블록·검정 방법과 가설군 크기. timezone 없는 캐시는 UTC로 해석한 사실도 기록한다. |
| `report.md` | 탐색·검증 모두 같은 방향의 기울기로 BY q < 0.05를 통과한 관계와, 그중 고정 예측의 검증 MSE 개선도 보정 후 통과한 관계 수. 음의 기울기도 유효한 예측 방향으로 취급한다. |

분위·추세 분할을 대체했으므로 `--quantiles`, `--regime-feature`, `--regime-threshold`, `--min-event-blocks`, `--round-trip-cost-bps`는 제거했다. 출력에 거래 승률이나 수수료 차감 손익을 표시하지 않는다. 통계적 관계나 예측오차 개선은 수익 전략의 증거와 같지 않으며, 발견한 후보는 기존 v2 백테스트에서 진입·청산과 비용·펀딩·체결을 검증한다. 필수 입력의 누락, 불규칙 시간 간격, 중복 시각은 임의 보간하지 않고 중단한다.

## Feature Spec

피처는 크게 다섯 방식으로 만든다.

### 1. source + steps

가장 일반적인 형태다. 원천 frame 하나에서 transform을 순서대로 적용한다.

특정 종목의 값을 출력 대상 종목 전체에 복제하려면
`source="market:<종목>:<원천 또는 피처>"`를 사용한다.
`market:KRW-BTC:trade_price`, `market:BTC/USDT:trade_price`와 함께
선물 형식 `market:BTC/USDT:USDT:trade_price`도 지원한다. 선물 종목명의
정산통화 접미사 `:USDT`는 종목명에 포함된다. 별도 Parquet 원천은
`market:BTC/USDT:USDT:custom:funding_rate`처럼 지정하며
`funding_rate.parquet`의 `BTC/USDT:USDT` 열을 읽는다. 이 참조는 종목을
자동 수집하거나 신호를 지연하지 않는다. 같은 캐시 안에 원천과 참조 종목이 있어야 하며,
관측 시점·결측 처리·신호 지연은 기존 피처/비중 설정으로 따로 정한다.

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

보유 상태에 신호 기준 최대 보유기간을 설정할 수 있다. 예시는 다음과 같다.

```json
{
  "column_name": "short_holding",
  "state": {
    "entry_feature": "short_entry",
    "exit_feature": "short_exit",
    "cooldown_bars": 6,
    "max_holding_bars": 24
  }
}
```

- `max_holding_bars`는 생략/`null`이면 기존처럼 명시한 청산 조건까지 보유한다. 지정하면
  양의 정수만 허용한다. 0·음수·Boolean·실수·문자열은 오류다. 그리드에서는
  `"max_holding_bars": "{hold_bars}"`, `"hold_bars": [12, 24, 48]`처럼 기존 템플릿을 쓴다.
- `entry`가 참이 되어 보유 신호가 시작된 행을 k라 하면, N봉 제한은 k+N행에서 상태를 0으로
  전환한다. 진입 조건이 계속 참이어도 시계를 초기화하지 않는다. 일반 청산이 먼저 발생하면
  그 청산과 기존 재진입 대기가 우선한다. 이는 **실제 체결 시각이 아니라 신호상 진입 기준**이다.
- 만료는 같은 행에서 재진입하지 않으며 기존 `cooldown_bars` 대기를 적용한다. 추가로 진입
  조건이 한 번 거짓이 된 뒤에만 새 참 신호를 허용해, 지속된 동일 조건의 주기적 재진입을 막는다.
  만료 행에서 진입 조건이 거짓이면 초기화로 인정한다. 이는 시간 만료에만 적용하며 기존 일반
  청산의 재진입 동작을 바꾸지 않는다. `cooldown_bars`는 기존처럼 양쪽 신호가 유효한 행에서 센다.
- 단위는 입력 시계열의 물리 행이다. 연속 1시간봉 24행은 24시간, 연속 5분봉 24행은
  2시간이다. 불규칙/거래일 자료에서는 벽시계 시간이 아니다. 결측 행도 최대 보유기간에는
  포함하며 만료 행에는 입력 조건이 결측이어도 알려진 시간 만료로 0을 출력한다. 캐시의 거래
  가능 기간 마스크와 보유 중 필수 가격 검사는 그대로 적용한다.
- 출력 상태를 기존 `position_score_feature`로 사용하고 `position_score_lag: 1`을 적용하면
  다음 관측 봉 시가에 실행한다. 진입·청산에 동일한 지연이 적용된다. **시간청산을 매 봉 반영하려면**
  `rebalance_frequency: "change_only"` 또는 `every_bar`를 사용한다. `daily` 같은 주문 주기를
  지정하면 신호가 먼저 0이 되어도 실제 비중 갱신은 다음 주문 시점으로 늦어질 수 있다.
- 헤지는 기존처럼 최종 알트 목표 비중으로 계산한다. 두 알트가 서로 다른 시각에 만료되면
  해당 알트 목표를 0으로 만들고 BTC 헤지도 같은 갱신에서 부분 축소한다. BTC 자체에 별도의
  피처 보유 시계를 만들 필요가 없다. 모든 알트가 0이면 BTC 목표도 0이다. 실제 주문 거절이나
  시간 경과에 따른 실제 수량 노출까지 자동 보정하는 기능이라는 뜻은 아니다.
- 신호 기준 청산을 쓰는 전략은 `numba_futures.max_holding_hours`를 생략/`null`로 둔다.
  엔진 옵션은 실제 체결 기준의 별도 안전장치이며, 동시에 켜면 어느 쪽이 먼저 종료할 수 있다.
  이 기능은 `state`를 쓰는 기존 v2 계산 경로에 적용되며 엔진의 기본값을 바꾸지 않는다.

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

연간 재무값(`fundamental_roe`, `fundamental_net_income` 등)의 공개 규칙
(`scripts/kr_stocks/build_kr_stock_fundamental_cache.py`):

- 기본: 값은 data.go.kr 요약재무의 값이고, 해당 사업연도의 마지막 사업보고서(정정 포함)
  접수일 다음 날부터 사용합니다. 요약재무는 정정 후 값이므로 원공시일로 앞당기지
  않습니다. 몇 년 뒤 일괄정정이 나온 종목은 그 정정일까지 해당 연도 값이 없습니다.
- `--use-xbrl-versions`(빌드 옵션): 사업보고서의 원공시·정정공시를 각각 접수일 다음
  날부터 반영합니다. 마지막 공시는 요약재무 값을 쓰고, 그 이전 공시는 OpenDART
  사업보고서 XBRL(`fnlttXbrl`)에서 읽은 그 공시 자체의 값을 씁니다. 이전 공시의
  XBRL이 없거나 같은 재무제표 유형(연결/별도)이 없으면 그 버전은 건너뜁니다.
  XBRL에서 읽지 못한 항목과 자본금은 그 버전에서 결측입니다(최종값을 빌려 쓰지 않음).
  요약재무에서 결측·0인 항목은 마지막 공시의 XBRL 값으로 채웁니다. 기초자본은 해당
  이벤트 공개일에 알려진 직전 사업연도 버전의 자본입니다. 필요한 XBRL은
  `update_kr_stock_fundamentals.py --include-xbrl-versions`(또는 `--xbrl-versions-only`)로
  받으며 `data/kr_stocks/raw/fundamental_xbrl_values.parquet`와 압축 인스턴스 폴더
  `fundamental_xbrl_instances/`에 저장합니다. `--reparse-xbrl`은 API 호출 없이 저장된
  인스턴스를 다시 파싱합니다.
- `fundamental_pretax_income`은 법인세차감전순이익입니다(요약재무 `iclsPalClcAmt`, XBRL
  `ProfitLossBeforeTax`). 이전 이름 `fundamental_comprehensive_income`은 의미와 달라 바꿨습니다.
- 요약재무가 누락을 0으로 주는 경우가 있어 영업이익·순이익·세전이익·자산·부채·자본·자본금의
  정확한 0은 결측으로 봅니다. 매출 0은 실제 값일 수 있어 그대로 둡니다.
- 최신 사업연도 값이 결측이면 결측으로 남고 이전 사업연도 값을 이어 쓰지 않습니다.
- 이미 공개된 사업연도보다 오래된 사업연도의 정정공시는 최신 값을 덮어쓰지 않습니다.
  사업연도 기준 배당·유통주식 변화 프레임도 같습니다.
- 금융업(은행·보험·증권·금융지주)은 요약재무 원천에 행이 없어 연간 재무값이 없습니다.
- 재무 캐시(보통주 플래그 포함)는 빌드 시점의 주가 달력으로 만들어집니다. 주가 캐시만 갱신한 뒤
  `custom:` 원천의 마지막 날짜가 같은 캐시의 `trade_price`보다 이르면 피처 계산이 오류로 멈춥니다
  (늘어난 기간이 조용히 결측이 되어 종목이 빠지는 것을 막기 위함). 이때
  `fundamental_cache_report.json`의 `build_args`와 같은 옵션으로 재무 캐시를 다시 빌드합니다.

## 주요 transform

| transform | 핵심 params | 의미 |
|---|---|---|
| `simple_return` | `window`, `missing_policy` | `price / price.shift(window) - 1`. `skip`(기본)는 유효 관측 기준, `preserve`는 원본 행 기준으로 결측 보존 |
| `momentum` | `window` | 가격 모멘텀 계열 |
| `rolling_mean` | `window` | rolling 평균 |
| `ewma` | `window` | EMA. 첫 window개 유효값의 SMA로 시작하고 이후 alpha=2/(window+1)로 갱신 |
| `vwma` | `window`, `reference` | 거래량가중이평. reference는 거래량 원본 컬럼 또는 거래량 피처 이름 |
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
| `calendar_start_value` | `freq` | UTC 달력 기간의 정확한 시작 시각 값을 그 기간에 유지. 기본 `D`, 주간 `W`/`W-SUN`은 월요일 00시 시작 |
| `calendar_mean` | `freq`, `signal_timing` | 기간 평균을 일별 frame에 매핑 |
| `calendar_rolling_robust_mean` | `freq`, `periods`, `skip_periods`, `signal_timing`, `mode`, `trim_count` | 기간별 마지막 값의 절사평균 또는 윈저평균 |
| `calendar_rolling_median` | `freq`, `periods`, `aggregation`, `min_count`, `signal_timing` | UTC 달력 기간별 집계값의 연속 기간 중앙값. 완료된 기간만 다음 기간에 적용 |
| `calendar_rolling_sum` | `freq`, `periods`, `skip_periods`, `signal_timing` | 기간별 마지막 값을 rolling 합산 |
| `period_simple_return` | `freq`, `periods`, `signal_timing` | 월/주/분기 단위 수익률 |
| `corr_greedy_filter` | `threshold`, `corr_window`, `liquidity_window`, `freq` | 상관이 높은 후보를 유동성 기준으로 제거 |
| `residualize_reference` | `reference`, `beta_window` | 기준 자산 베타를 제거한 잔차 |
| `subtract_reference` | `reference` | 기준 피처를 차감 |
| `ratio_to_reference` | `reference` | 기준 피처 대비 비율 |
| `multiply_reference` | `reference` | 같은 날짜·종목의 기준 피처와 곱함. 한쪽이 결측이면 결측 유지. 거래대금 근사 등 범용 곱셈 |
| `mask_by_feature` | `feature` | 다른 피처가 켜진 곳만 남김 |
| `select_by_state` | `state_<값>_feature`, `default_feature` | 상태값에 따라 사용할 피처를 선택 |
| `clip` | `lower`, `upper` | 값 상하한 제한 |
| `target_vol_weight` | `target_annual_vol`, `periods_per_year`, `min_weight`, `max_weight` | 실현변동성을 목표 연환산 변동성에 맞추는 종목별 비중 배수 |
| `quantize_weight` | `step_size`, `rounding`, `min_weight`, `max_weight` | 연속형 비중 배수를 일정한 단계로 변환 |
| `rolling_beta` | `reference`, `window` | 기준 피처 대비 과거 `window`행의 롤링 베타. 완전한 유효 관측 구간 필요 |
| `shift` | `periods` | row 기준 lag |

`calendar_start_value`는 기간 시작 시각의 원래 행만 읽는다. 예를 들어 `opening_price`에
`{"kind":"calendar_start_value","params":{"freq":"W-SUN"}}`를 적용하면 UTC 월요일
00시 시가를 그 주 내내 유지한다. `M`은 매월 1일 00시다. timezone이 있는 입력은 UTC로
판정하고 원래 출력 인덱스는 보존하며, timezone이 없으면 UTC로 취급한다. 시작 행이 없거나
그 종목의 시작값이 결측/무한대면 기간 전체를 결측으로 두며 나중 첫 유효값으로 채우지 않는다.
따라서 이력 첫 주가 중간부터 시작하거나 주중 상장된 종목은 다음 정상 기간부터 기준값이 생긴다.
기간 시작값 자체는 그 시각부터만 사용한다. 시가에 알 수 있는 기준 가격에는 `opening_price`를
사용하고, 종가 조건의 다음 시가 실행 지연은 기존 `signal_lag` 등에서 별도로 지정한다.

주의:

- `vwma`는 현재 봉까지의 연속된 window개 행에 대해 `Σ(가격×거래량) / Σ거래량`을 계산한다.
  거래량은 음수일 수 없으며, 가격·거래량이 결측/비유한 값인 행이 창에 포함되거나 거래량 합이
  0이면 NaN이다. 빠진 행을 제거해 이전 관측값으로 창을 늘리거나 0으로 채우지 않는다.
  `reference` 의존성은 피처 그래프가 자동으로 읽고 정렬한다. 예: 가격 source에
  `{"kind":"vwma","params":{"window":20,"reference":"candle_acc_trade_volume"}}`를 적용한다.
- `calendar_mean(freq="M", signal_timing="next_period")`은 월평균을 다음 달 날짜들에 매핑한다. 그래서 결과적으로 한 달 고정처럼 보인다.
- `calendar_rolling_robust_mean`은 `periods - skip_periods`개의 값을 집계한다. `mode="trimmed"`는 양쪽에서 `trim_count`개씩 제외하고, `mode="winsorized"`는 양쪽 극단값을 각각 경계값으로 대체한다.
- `calendar_rolling_median`은 `freq="D"`, `aggregation="last"`, `min_count=1`,
  `signal_timing="next_period"`가 기본이며 `periods`는 양의 정수로 지정한다. `aggregation`은
  `last`, `sum`, `mean`이다. `min_count`개 이상의 유한 관측이 있는 기간만 인정한다.
  빠진 달력 기간은 결측으로 남기고, 연속 `periods`개 집계가 모두 유효해야 중앙값을 낸다.
  중앙 두 값이 있는 짝수 길이는 두 값의 평균을 쓴다. 순서가 뒤집히거나 중복/결측인 시각은 거부한다.
  timezone-aware 입력은 UTC로 집계하고 출력 인덱스의 원래 timezone은 보존한다.
  `same_period`는 미래 정보를 사용할 수 있어 지원하지 않는다. 현재 기간의 원시 관측이
  없어도 이미 알려진 이전 기간 통계는 유지하며, 거래 가능 여부는 기존 캐시 자격 마스크가 담당한다.
  1시간봉 거래대금에 `freq="D", periods=30, aggregation="sum", min_count=24`를 적용하면
  완전한 UTC 일별 거래대금 30개의 중앙값을 다음 날 00:00부터 사용할 수 있다.
  이 피처로 선별할 때 `signal_lag=0`으로 두면 추가 하루 지연이 생기지 않는다.
- `calendar_rolling_sum(freq="M", periods=9, skip_periods=1, signal_timing="next_period")`은 월별 값 9개 합에서 최근 1개월을 제외하고 다음 달에 적용한다.
- `calendar_hold(freq="M", hold_periods=2)`는 월별 마지막 값을 2개월 동안 유지한다. `anchor="calendar"`면 1-2월, 3-4월처럼 달력 기준으로 묶고, `anchor="first_valid"`면 첫 유효 기간부터 묶는다.
- `calendar_hold(..., broadcast_sparse=true)`는 기간 중 일부 날짜에만 값이 있는 피처의 마지막 유효값을 다음 보유 기간 전체에 매핑한다. 기본값은 `false`이며 기존처럼 원본 피처의 결측 마스크를 유지한다.
- `group_demean`, `group_rank`, `group_percentile`의 `group_feature`에는 먼저 정의한 피처 이름을 넣는다. 그룹 피처나 원본 값이 결측이면 결과도 결측이며, 유효 종목 수가 `min_group_size`보다 작은 그룹도 제외한다.
- `rebalance_frequency`는 주문/weight 출력 주기이고, 피처 점수 산출 주기가 아니다.
- `rebalance_frequency: "3d"`처럼 양의 정수와 `d`를 쓰면 N일마다 목표비중을 출력한다.
  UTC `1970-01-05 00:00`(월요일)를 고정 기준으로 N개의 달력 일을 묶는다. 시간대가 없는
  인덱스는 UTC로 해석하고, 시간대가 있으면 UTC로 환산하되 출력 인덱스는 보존한다.
  각 구간의 첫 관측 봉에서 출력하며 첫 입력 봉도 초기 배분 대상이다. 경계 봉이 없으면
  그 구간의 첫 관측 봉을 사용하고, 관측이 없는 구간은 건너뛴다. 시작일을 바꾸어도 이후
  구간의 경계는 이동하지 않지만, 새 입력의 첫 봉은 별도 초기 배분으로 출력될 수 있다.
  `7d`는 UTC/시간대 없는 입력에서 기존 월요일 시작 `weekly`와 같고 `1d`는 `daily`와 같다.
  기존 `daily`, `weekly`, `monthly`, `quarterly`, `semiannual`, `every_bar` 동작은 유지한다.
- `"Nd+K"`는 N일 구간의 시작을 K일 늦춘다(1 ≤ K < N). `7d+3`은 매주 목요일 00 UTC(또는 그
  구간의 첫 관측 봉), `14d+7`은 `14d`보다 한 주 늦은 2주 주기다. 위상은 `Nd`처럼 전역 UTC 달력에
  고정되어 워크포워드 구간 시작과 무관하다. `+0`은 받지 않으며 `Nd`를 그대로 쓴다(실행 이름 유지).
  `weekly` 등 달력 이름에는 오프셋을 붙일 수 없다. weight의 `rebalance_frequency`,
  `preselection.rebalance_frequency`, `position_exposure_cap.reset_frequency`에서 같은 의미이고
  `strategy_sleeves`에서는 sleeve마다 다른 오프셋을 줄 수 있다(예: 같은 전략을 `7d`와 `7d+3`
  두 sleeve로 나눔). 이때 공유 종목은 기존 sleeve 결합 근사를 따른다. 한 sleeve의 요청 시각에
  다른 sleeve 몫은 실제 보유 수량이 아니라 그 sleeve의 마지막 목표 비중으로 다시 맞춰지므로,
  같은 종목을 겹쳐 보유하는 요일 분산은 두 계좌를 따로 운용한 결과와 다를 수 있다.
  `partial_exit.reset_frequency`는 기존대로 `daily`, `weekly`, `monthly`만 지원한다.
  같은 주기 함수를 쓰는 `gross_exposure_update_frequency`와 `preselection.rebalance_frequency`에도
  사용할 수 있다. 체결 지연·`output_mode`와 거래 가능 마스크는 기존 규칙을 그대로 따른다.

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

### 선택 종목을 유지하고 빈자리만 채우기

`Universe Spec`의 선택 옵션 `holding`은 **목표 보유 종목**을 기억한다. 생략/`null`이면
기존처럼 각 시점의 조건과 순위로 다시 선택한다. 별도 실행기 없이 기존 단일 백테스트,
grid/TPE, PBO·SPA 재실행, WF 경로에서 사용한다.

```json
{
  "feature_column": "entry_priority",
  "mode": "top_n",
  "top_n": 4,
  "ascending": false,
  "lag": 0,
  "signal_lag": 1,
  "value_filters": [
    {"feature_column": "entry_allowed", "operator": "eq", "value": 1, "lag": 0}
  ],
  "holding": {
    "exit_feature": "short_exit",
    "exit_lag": 0,
    "max_holding_bars": 24,
    "cooldown_bars": 6
  }
}
```

위 피처 이름들은 예시다. `entry_priority`, `entry_allowed`, `short_exit`을 기존 피처
계산으로 정의해야 하며, 이 옵션 자체가 과열·식음의 정의를 만들지는 않는다.

- `mode: "top_n"`에서만 사용하며 `top_n`이 최대 목표 보유 수다. 기존 보유 중 청산 대상부터
  제거하고 남은 보유를 유지한 뒤, 빈자리만 현재 유효 후보 중 순위대로 채운다. 동점은
  종목명 오름차순으로 정한다. 못 뽑힌 후보를 저장하지 않으며, 나중에 빈자리가 생겼을 때도
  진입 조건이 유효하면 선택할 수 있다. 순위 밀림·진입 필터 탈락만으로는 청산하지 않는다.
- `exit_feature`의 0은 청산하지 않음, 유한한 비영 값은 청산이다. 청산 조건이 참인 종목은
  새로 선택하지 않는다. 선택 후 청산 신호가 결측/무한대면 오류로 알리며 거짓으로 바꾸지
  않는다. 아직 미보유인 종목의 청산 신호가 결측이면 진입을 보류한다.
- `exit_lag`와 각 필터의 lag에는 `signal_lag`를 한 번씩 더한다. 위 설정은 직전 봉까지의
  순위·진입·청산 조건을 현재 봉에 적용한다. `lag`는 정렬 피처의 지연이며 청산 지연을
  자동으로 바꾸지 않는다. 음수·실수·Boolean 지연은 이 모드에서 허용하지 않는다.
- `max_holding_bars`는 생략/`null`이면 조건 청산만 사용한다. 양의 정수를 지정하면 목표
  보유로 처음 선택된 행 k부터 k+N행에 청산한다. 후보였지만 미선택인 기간에는 시계를
  돌리지 않는다. 단위는 원본 행이며 연속 1시간봉 24행은 24시간이다.
- `cooldown_bars`는 기본 0인 음이 아닌 정수다. 청산 행에서 재진입하지 않고 추가로
  다음 N행 동안 해당 종목을 막는다. 결측 행도 센다. 대기 후에는 그때 진입 조건으로 다시
  판단하므로 동일 과열 사건당 한 번만 진입하는 보장은 아니다. 필요하면 진입 피처에서
  새 사건 여부를 정의한다.
- 허용 목록·캐시 거래 가능 기간은 보유에도 적용한다. 캐시 사용 기간을 벗어나면 목표를
  0으로 만들며 실제 정산은 기존 엔진 규칙을 따른다. `value_filters`, 순위 필터, 최소
  이력·경고 종목 필터와 주간/일간 `preselection`은 이 모드에서 **새 진입의 조건**이다.
  기존 보유는 명시한 청산·시간 제한까지 유지한다. 사전선별 블록 자체에는 `holding`을
  지정할 수 없다.
- 초기 지원은 `weighting: "fixed"`, `rebalance_frequency: "change_only"` 또는 `every_bar`다.
  `position_score_feature`와 별도 `gross_exposure_update_frequency`는 중복 보유 관리나
  청산 지연을 막기 위해 함께 쓰지 않는다. `numba_futures.max_holding_hours`도 동시에
  설정하면 오류다. 과열 감시용 피처 `state`는 사용할 수 있지만 포지션 보유와 시계는
  이 옵션에서 관리한다. 예를 들어 고정 숏 비중은 `fixed_weight: -0.25`로 설정한다.
- BTC 헤지는 이 선택 결과로 만든 최종 알트 비중을 기존 방식으로 받아 계산한다. BTC를
  전략 유니버스에서 제외하면 알트 최대 보유 수에 포함되지 않는다. `change_only`를 쓰면
  동일한 목표가 유지되는 동안 수량을 유지한다.
- WF의 각 IS/OOS 계좌는 평가 시작에 빈 보유 목록으로 출발한다. 지표·과열 감시 피처는
  과거 자료로 예열하지만 목표 보유 목록·보유시간·재진입 대기는 넘겨받지 않는다. timestamp
  시작을 쓰는 grid도 설정 시작일부터 목록을 만든다. 해당 시작일은 선택 결과 캐시 키에 포함한다.
- 실제 체결 결과·주문 거절·강제청산을 되돌려 받는 기능은 아니다. 최대 보유 수와 시계는
  목표 기준이며 실제 계좌 보유와 달라질 수 있다. 기존 주문·펀딩·증거금 계산은 변경하지 않는다.

## Weight Spec

Weight는 선택된 후보를 실제 목표 비중으로 바꾼다.

### change_only의 변경 범위

`rebalance_frequency: "change_only"` 또는 `output_mode: "change_only"`를 사용할 때
`change_only_scope`로 주문 요청의 범위를 선택한다.

- 생략 또는 `"portfolio"`: 기존 기본값. 한 종목의 목표라도 바뀌면 그 행의 전체 목표를
  다시 전달한다. 따라서 변경 없는 보유 종목도 당시 계좌 자산·가격에 맞춰 재조정될 수 있다.
- `"asset"`: 종목별 목표가 바뀐 칸만 전달한다. 목표가 같은 칸은 `NaN`으로 수량 유지,
  청산 목표는 명시적 `0`이다. 다른 종목의 진입·청산은 기존 종목의 수량이나 미체결
  요청 수량을 다시 산정하지 않는다. 자기 목표가 -0.20에서 -0.30으로 바뀌면 재조정한다.
- 이 옵션은 change_only 출력에서만 사용한다. 일/주간 같은 평가 주기는 기존 설정을 따른다.
  단일·그리드·그리드 TPE·PBO·SPA·WF는 같은 범위를 최종 엔진 입력까지 전달한다.
  새 계좌가 시작되는 평가 구간에서는 첫 행의 전체 목표를 전달해 첫 진입 누락을 방지한다.
- 헤지·노출 배율·동일가중 배분 등으로 해당 종목의 목표 자체가 바뀌면 `asset`에서도
  주문한다. 모든 전략을 ‘진입 후 수량 고정’으로 바꾸는 옵션은 아니다.
- 최초 목표 합계나 보유 종목 수는 실제 총노출 상한이 아니다. 보유 중 가격·계좌 자산이
  바뀌면 노출이 100%를 넘을 수 있다. 총노출 제한·자동 축소·체결 결과에 따른 빈자리
  관리 기능은 추가하지 않는다. 기존 주문 제한·재시도·강제청산 규칙도 유지한다.

```json
{
  "weighting": "fixed",
  "fixed_weight": -0.2,
  "rebalance_frequency": "change_only",
  "change_only_scope": "asset"
}
```

문제 기록(2026-11-04): 기존 `to_change_only_target_frame`은 행 전체의 변경을 판정했다.
이는 기존 portfolio 의미이며, 독립 진입·보유 전략의 종목별 수량 유지와는 달랐다.
옵션으로 asset 출력을 추가하고 최종 재변환·평가 구간 첫 행에도 같은 범위를 적용했다.
그리드 보유 수 요약은 `NaN` 유지 지시를 해석한 목표 목록을 세도록 연결했다.
새 옵션 미사용 시 portfolio 출력과 기존 주문 정책은 유지한다. 검증 결과는
`notes/numba_futures_v2.md`의 종목별 변경 범위 기록을 따른다.

### 동일가중

```json
{
  "weighting": "equal",
  "gross_exposure": 1.0,
  "rebalance_frequency": "change_only",
  "universe_name": "{run_name}"
}
```

### 롱·숏 방향별 동일가중

`weighting: "long_short_equal"`은 롱·숏을 각각 동일가중으로 배분한다.
`long_exposure`와 `short_exposure`는 각각 기본값 `0.5`인 독립적인 비음수 목표 노출이다.
예를 들어 `0.8`/`0.3`이면 총 목표 명목노출은 `1.1`이다. 각 방향의 종목 수로 해당
노출을 나누며 한쪽 후보가 없으면 그 몫은 현금으로 남긴다. 총노출을 다시 100%로
정규화하지 않는다. 혼동을 방지하기 위해 이 방식의 `gross_exposure`는 `1`만 허용한다.
`gross_exposure_feature`를 지정하면 기존 의미대로 양쪽 목표에 같은 배율을 적용한다.

`long_exposure_feature`, `short_exposure_feature`를 지정하면 각각 해당 방향에만 피처 배율을
곱한다. 기본값은 둘 다 없음(배율 1)이며 `long_short_equal`에서만 지원한다. 예를 들어
`long_exposure: 0.5`, `long_exposure_feature: "btc30_long_scale"`에서 피처가 `0.5`이면
롱 총목표는 25%가 되고 숏 목표와 선정 종목은 바뀌지 않는다. 해당 방향 후보끼리는 계속
동일가중이며, 줄인 몫을 다른 방향에 재배분하거나 총노출을 100%로 맞추지 않는다.

- 피처는 시장 전체에 공통인 한 열 또는 행별로 동일한 값이 방송된 여러 열이어야 한다.
  음수·무한값·종목별 상이한 값은 거부한다. 같은 값 판정은 절대 1e-12 또는 상대 1e-9
  이내다. `market:BTC/USDT:USDT:...`처럼 한 시장 값을 열마다 복사한 뒤 rolling 통계를 열별로
  계산하면 종목별 시작점 차이로 상대 1e-12 정도의 부동소수 차이가 생기는데 이를 허용한다.
  여러 열이면 행마다 첫 유효 열의 값을 쓴다(`defensive_sleeve.risk_on_feature`도 같은 판정).
  0은 해당 방향 청산, 1은 기존 배율,
  1보다 큰 값은 확대다. 방향 배율은 별도로 자동 clipping하지 않는다.
- `long_exposure_lag`, `short_exposure_lag`는 각각 기본 0인 음이 아닌 정수이며 입력 봉 수만큼
  피처를 지연한다. 양수 lag에는 해당 방향 피처가 필요하다. 피처 자체가 이미 지연됐다면
  같은 지연을 다시 넣지 않는다. 피처/lag를 생략하면 기존 결과와 식별 이름을 유지한다.
- 피처의 결측과 lag 초기 구간은 중립 배율 1로 처리한다. 미래 관측으로 채우지 않는다.
  피처 이름을 지정했는데 입력 프레임이 없으면 오류다. 기존 공통 `gross_exposure_feature`의
  결측 0·clip 규칙은 유지하며 방향 배율과 공통 배율은 곱해서 적용한다.
- 배율은 `rebalance_frequency`에 따른 목표 출력 시점에 반영한다. `weekly`에서는 주간
  리밸런싱 때만 반영되고 사이 봉의 수량 유지(`NaN`)는 그대로다. 기존 공통 배율의 별도
  갱신 주기를 지정해도 방향 배율 자체에 새로운 갱신 주기가 생기지는 않는다.

방향 입력은 다음 두 방식 중 하나다.

- 기존 `position_score_feature`: 양수는 롱, 음수는 숏, 0·결측은 제외한다.
  크기는 배분에 사용하지 않으며 `position_score_lag`를 적용한다. 무한값은 거부한다.
- `long_filter`와 `short_filter`: 기존 `universe.rank_filters`와 같은 필드의 순위 조건을
  양쪽에 각각 지정한다. `top_n`/`quantile`, `ascending`, `lag`, `scope`를 재사용한다.
  이 경우 `position_score_feature`를 함께 지정할 수 없고 `position_score_lag`는 `0`이어야 한다.
  `lag`는 각 필터에 직접 지정하며 universe의 `signal_lag`를 자동으로 더하지 않는다.

`long_filter`·`short_filter`는 단일 순위 조건 외에 `{"mode":"and","filters":[...]}` 또는
`{"mode":"sequential","filters":[...]}`도 받는다. 그룹의 기본 `mode`는 기존과 같은 `and`다.
`filters`는 비어 있지 않은 순위 조건 배열이다.

- `and`: 각 조건이 같은 후보 모집단에서 독립적으로 순위를 계산한 뒤 교차한다.
  `scope: "global"`이면 원래 적격 모집단, `filtered`이면 해당 방향을 나누기 전의
  최종 선택 모집단을 사용한다. 필터 순서를 바꿔도 결과가 같다.
- `sequential`: 배열 순서대로 후보를 줄인다. `scope: "global"`은 여전히 원래 적격
  모집단 기준이고, `filtered`는 앞 조건들을 통과한 현재 방향의 후보 안에서 순위를 계산한다.
  따라서 앞의 두 조건을 `global` 3분할로 지정하면 기존 독립 교차를 유지하면서,
  마지막 `filtered`·`top_n`으로 그 교집합 안에서 N개를 고를 수 있다. 필터 순서가 중요하다.

중첩 그룹·OR·빈 배열은 지원하지 않으며, 기존 단일 조건 형식은 그대로 사용할 수 있다.
거래대금 조건이 양쪽에 다르면 universe에서 한쪽 거래대금 그룹만 먼저 고르지 말고,
공통 적격 종목을 남긴 상태에서 각 방향의 AND 조건에 거래대금을 지정한다.

```json
{
  "weighting": "long_short_equal",
  "long_exposure": 0.5,
  "short_exposure": 0.5,
  "rebalance_frequency": "weekly",
  "long_filter": {"mode": "and", "filters": [
    {"feature_column": "turnover", "mode": "quantile", "quantiles": 3,
     "bucket_values": [3], "ascending": true, "scope": "global", "lag": 0},
    {"feature_column": "return7d", "mode": "quantile", "quantiles": 3,
     "bucket_values": [3], "ascending": true, "scope": "global", "lag": 0}
  ]},
  "short_filter": {"mode": "and", "filters": [
    {"feature_column": "turnover", "mode": "quantile", "quantiles": 3,
     "bucket_values": [1], "ascending": true, "scope": "global", "lag": 0},
    {"feature_column": "return7d", "mode": "quantile", "quantiles": 3,
     "bucket_values": [1], "ascending": true, "scope": "global", "lag": 0}
  ]}
}
```

`scope: "global"`은 종목선택에서 사용한 원래 적격 모집단을 기준으로 순위를 계산한 뒤
최종 선택 마스크와 교차한다. 따라서 거래대금 필터 후 그룹 안에서 수익률을 재분할하지 않는다.
`filtered`의 모집단은 위의 그룹 `mode`에 따른다. 동률은 기존 순위 필터의 열 순서
기준을 따른다. 한 종목이 같은 시각에 양쪽 조건을 만족하면 오류로 중단한다.
직접 Python 호출에서 global 필터를 쓰려면 `build_weight_frame_v2`에
`base_eligible_mask=universe_result.base_eligible_mask`를 전달해야 한다.
단일 v2 실행·grid·TPE 재평가·PBO·SPA·WF 경로는 이를 전달한다.

다음은 기존 전체 모집단에서 두 축을 독립 3분할한 뒤, 해당 롱 그룹 안에서
거래대금이 낮은 순으로 최대 10개를 고르는 예시다. 숏도 별도의 기준·방향·N을 지정할 수 있다.
`top_n`은 양의 정수여야 하며 `ascending: true`는 낮은 값 우선, `false`는 높은 값 우선이다.
한도보다 후보가 적으면 있는 종목만 동일가중하고, 해당 방향이 비면 그 몫은 현금으로 남긴다.
선택 한도는 목표 종목 수의 상한이며 미체결·청산을 반영한 실제 보유 수를 보장하지 않는다.

```json
{
  "mode": "sequential",
  "filters": [
    {"feature_column": "turnover", "mode": "quantile", "quantiles": 3,
     "bucket_values": [1], "ascending": true, "scope": "global"},
    {"feature_column": "return7d", "mode": "quantile", "quantiles": 3,
     "bucket_values": [1], "ascending": true, "scope": "global"},
    {"feature_column": "turnover", "mode": "top_n", "top_n": 10,
     "ascending": true, "scope": "filtered"}
  ]
}
```

```json
{
  "weighting": "long_short_equal",
  "long_exposure": 0.5,
  "short_exposure": 0.5,
  "rebalance_frequency": "weekly",
  "long_filter": {
    "feature_column": "return7d", "mode": "quantile", "quantiles": 3,
    "bucket_values": [3], "ascending": true, "scope": "global", "lag": 0
  },
  "short_filter": {
    "feature_column": "return7d", "mode": "quantile", "quantiles": 3,
    "bucket_values": [1], "ascending": true, "scope": "global", "lag": 0
  }
}
```

이 방식을 선택하지 않으면 기존 배분 동작을 변경하지 않는다.
리밸런싱 행에만 목표를 출력하고 그 사이의 `NaN`은 기존처럼 수량 유지를 뜻한다.
미체결·최소 주문 제한과 이후 가격 변동으로 실제 비중은 목표와 달라질 수 있다.

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

### hedge: 지정 자산으로 목표 노출 헤지

`weight_spec` 또는 그리드의 `weight_spec_template`에 선택적으로 추가한다. 기본값은
`null`(헤지 없음)이며 기존 배분·엔진 설정을 바꾸지 않는다. 기본 종목 선택·배분을 마친
**목표 비중**을 기준으로 헤지 비중을 만든다. 체결 후 실제 수량·가격 변동으로 생긴 노출을
계속 추적하여 상쇄하는 기능은 아니다.

```json
{
  "weighting": "fixed",
  "fixed_weight": 1.0,
  "position_score_feature": "strategy_position",
  "position_score_lag": 1,
  "hedge": {
    "market": "BTC/USDT:USDT",
    "mode": "beta",
    "beta_feature": "beta_to_btc",
    "beta_lag": 1,
    "ratio": 1.0
  },
  "rebalance_frequency": "change_only"
}
```

| 설정 | 의미·기본값 |
|---|---|
| `market` | 필수 헤지 자산. 일반 전략의 선택 대상에서 제외해야 한다. 비중 출력과 백테스트 가격 로딩에는 별도로 포함된다. |
| `mode` | `beta`(기본) 또는 `notional` |
| `beta_feature` | `beta` 모드에서 필수. 각 종목의 기준 자산 대비 베타를 담은 피처 |
| `beta_lag` | 베타를 지연할 행 수. 기본 1, 0 이상의 정수. `position_score_lag`와 별개로 해당 베타에 한 번 적용 |
| `ratio` | 헤지 강도. 기본 1.0, 유한한 0 이상의 값. 0.5면 절반 헤지 |

`beta` 모드의 헤지 비중은 `-ratio × Σ(기본 목표 비중 × 베타)`다. `notional`은
`-ratio × Σ(기본 목표 비중)`이며 베타 피처를 요구하지 않는다. 양수·음수 기본 비중과
음의 베타도 그대로 계산하므로 결과에 따라 헤지 자산이 롱 또는 숏이 될 수 있다.

예를 들어 A 비중 -0.4, 베타 1.0, B 비중 -0.6, 베타 1.5이면 BTC 비중은 +1.3이다.
A·B가 모두 0이 되는 갱신에서는 헤지도 0이다. 이후 공통 배율 0.5를 적용하면
A -0.2, B -0.3, BTC +0.65가 된다. 총 절대노출은 세 비중의 절댓값 합으로 별도 확인한다.
헤지 자체가 레버리지·증거금 제한을 자동으로 맞추지는 않는다.

계산 순서는 `기본 배분 → 헤지 추가 → 공통 노출 배율 → 포트폴리오 목표 변동성 → change_only`다.
`gross_exposure_update_frequency: "every_bar"`를 사용하면 기존 주기의 헤지 포함 비중을
유지하면서 공통 배율만 갱신한다. 일반 주기 설정은 그 주기의 갱신 시점에만 베타를 읽는다.
`change_only`에서는 기본 비중이 같더라도 베타가 변해 헤지 비중이 바뀌면 새 지시가 나간다.
변화폭 제한·주문 후 실제 포지션 피드백은 이 옵션에 포함하지 않는다.

거래 가능 기간 마스크가 있는 캐시에서는, 헤지가 최종 거래 금지용 `0`을 넣기 전의
기본 비중에서 주문 시점을 읽는다. 따라서 아직 사용할 수 없는 종목의 `0` 때문에
주문 없는 행을 리밸런싱으로 오인하지 않는다. 헤지를 합친 뒤의 공통 거래 금지 처리,
기존 비헤지 전략의 청산용 `0`, 활성 비중의 베타 결측 검사와 헤지 자산의 기간 검사는 유지한다.

헤지 자산을 일반 종목으로도 선택하면 오류로 거부한다. `allowed_markets`가 알트만 포함하면
그대로 사용할 수 있고, 헤지 자산도 포함한 전체 유니버스라면 `excluded_markets`에 지정한다.
`defensive_sleeve`와의 동시 사용은 지원하지 않는다. `portfolio_vol_target`을 함께 쓰면
그 `price_feature`에 헤지 자산 가격도 있어야 한다. 예를 들어 BTC 참조 소스가 로드된
`trade_price`를 지정하거나, 피처 출력 대상에 BTC를 포함하고 일반 선택에서는 제외한다.

활성 기본 포지션에 필요한 베타가 결측·무한대이면 계산을 중단한다. 베타를 0으로 채우거나
헤지 없이 진입하지 않는다. 워밍업에는 유니버스의 `value_filters` 등으로 베타가 유효한
종목만 선택해야 한다. 같은 `beta_feature`의 유효성을 검사할 때는 다음 실제 지연을 맞춘다.

`value_filters[].lag + universe_spec.signal_lag = hedge.beta_lag`

`signal_lag`는 종목 선택의 점수·값 필터·순위 필터에 공통으로 더해진다. 비중부의
`position_score_lag`나 `hedge.beta_lag`에 자동으로 더해지는 값은 아니다.
`signal_lag=1`, `beta_lag=1`이면 필터 자체 `lag=0`을 사용한다.
`signal_lag=0`, `beta_lag=1`이면 필터 `lag=1`이다. 모두 1로 맞추면 필터는 2봉 전,
헤지는 1봉 전 베타를 사용하므로, 베타가 중간에 결측으로 바뀔 때 필터를 통과한 뒤
활성 비중의 베타 검증에서 중단될 수 있다.

다음은 `universe_spec` 또는 그리드의 `universe_spec_template`에 넣는 관련 필드다.
위 비중 예시(`beta_lag=1`)와 같은 시점의 베타 결측을 제외한다.
유한한 베타 범위도 연구에 맞게 제한할 수 있다.

```json
{
  "signal_lag": 1,
  "value_filters": [
    {"feature_column": "beta_to_btc", "operator": "gt", "value": -1000000, "lag": 0}
  ]
}
```

#### rolling_beta 피처

베타는 가격 수준이 아니라 같은 주기의 수익률로 계산한다. 예시의 BTC 열은 모든 알트 열에
방송되며, 각 알트와 BTC의 공분산을 BTC 분산으로 나눈다.

양쪽 수익률에는 `missing_policy: "preserve"`를 지정한다. 이 옵션은 결측을 제거하지 않고
원본 행에서 `window`만큼 이동한 가격과 비교한다. `window=1`일 때 현재 또는 직전 가격이
결측이면 수익률도 결측이다. 따라서 누락 직후 여러 봉에 걸친 수익률이 한 봉 수익률로
베타에 섞이지 않는다. 0인 분모·비유한 입력·비유한 결과도 결측 처리한다.
원본 인덱스에 아예 없는 시각을 복원하지는 않으므로 캐시의 시간축은 먼저 맞춰져 있어야 한다.
`preserve`는 양의 `window`와 정렬된 고유 시간축을 요구한다.

기존 설정 호환성을 위해 `missing_policy`를 생략하면 `skip`이다. 이 경우 종목별 결측을
제거한 유효 관측 순서로 계산하므로, 결측이 있는 시계열의 기간 간 베타에는 사용하지 않는다.
새 옵션은 일반 `simple_return` 연산에 적용되며 전용 헤지 스크립트를 요구하지 않는다.

```json
[
  {
    "source": "trade_price", "column_name": "bar_return",
    "steps": [{"kind": "simple_return", "params": {"window": 1, "missing_policy": "preserve"}}]
  },
  {
    "source": "market:BTC/USDT:USDT:trade_price", "column_name": "btc_bar_return",
    "steps": [{"kind": "simple_return", "params": {"window": 1, "missing_policy": "preserve"}}]
  },
  {
    "source": "bar_return", "column_name": "beta_to_btc",
    "steps": [{"kind": "rolling_beta", "params": {"reference": "btc_bar_return", "window": 240}}]
  }
]
```

`window`는 2 이상의 정수이며, 현재 행까지의 과거 행을 사용한다. 입력·기준 양쪽 모두
유한한 관측이 `window`개 연속 있어야 하고, 기준 분산이 0이면 결측을 반환한다.
결측 행을 제거해 시간 구간을 압축하거나 미래 값으로 채우지 않는다. 상장 전·누락 이후에는
완전한 구간이 다시 쌓여야 한다. `beta_lag`는 이 계산 이후 비중부에서 적용한다.
피처가 이미 지연돼 있으면 중복 지연하지 않도록 0을 명시한다.

Numba의 float64 이동 공분산·분산 갱신으로 계산하며, 창마다 재계산하여 누적 반올림 오차를
제한한다. 계산량은 대략 행 수×종목 수에 비례하고 전체 종목 쌍의 공분산 행렬을 만들지 않는다.
기존 `residualize_reference`의 동작은 유지한다. 같은 입력·기준·창의 결과는 기존 피처 캐시로
재사용한다. 그리드에서는 `window`, `ratio`, 기준 피처명 등에 `{beta_window}`, `{hedge_ratio}`
같은 템플릿 변수를 넣어 기존 방식으로 탐색할 수 있다. 기준 자산을 바꾸면 참조 소스와
`hedge.market`도 함께 바꿔야 한다.

2026-11-02 로컬 검증: 실제 BTC·SOL 1시간봉 공통 구간 53,616행, `window=240`에서
Pandas 공분산/분산 방식과 비교한 최대 절대 차이는 약 `2.9e-13`이었다. 3회 반복 중앙값은
Pandas 12.1ms, Numba 1.21ms였으며 첫 호출(JIT 캐시 로딩 포함)은 259ms였다.
같은 실제 수익률을 32열로 복제한 처리량 비교에서는 371ms 대비 59.5ms였다.
전체 데이터 로딩·피처 그래프·백테스트 시간이 아닌 베타 함수 호출 시간이며,
실제 32개 종목이나 전체 유니버스의 성능·최대 메모리를 측정한 결과는 아니다.

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

### strategy_sleeves: 독립 전략을 한 계좌로 결합

서로 다른 universe·비중 규칙을 가진 전략(sleeve)을 각각 만든 뒤 하나의 계좌 목표 비중으로 합친다.
`weight_spec_template` 전체를 아래 형태로 쓴다. 다른 최상위 비중 키와 함께 쓸 수 없다.
지원: 그리드(`scripts/run_cross_section_grid_v2.py`), 워크포워드(`scripts/walkforward/run_cross_section_grid_walkforward_v2.py`),
PBO·SPA(`scripts/research/analyze_pbo.py`, `analyze_spa.py`, 워크포워드 구간 함수 공유), `engine="numba_futures"`만.
단일 백테스트(`scripts/run_cross_section_backtest_v2.py`)·strategy_search(TPE)·다른 엔진은 오류로 중단한다.
단일 실행은 후보 1개짜리 그리드로 한다.

```json
"universe_spec_template": { "...": "계좌 universe: 모든 sleeve 종목을 포함" },
"weight_spec_template": {
  "strategy_sleeves": [
    {"name": "basic33", "capital": "{alt_cap}",
     "universe_spec": { "...": "알트 universe" }, "weight_spec": { "...": "기존 비중 spec" }},
    {"name": "btc_trend", "capital": "{btc_cap}",
     "universe_spec": { "allowed_markets": ["BTC/USDT:USDT"], "...": "..." },
     "weight_spec": { "...": "fixed, change_only 등" }}
  ]
}
```

| 키 | 의미 |
|---|---|
| `name` | 고유 이름. 결과 메타데이터 `strategy_sleeves`에 기록 |
| `capital` | 계좌 자산 대비 배율(0 이상, 합이 1을 넘어도 됨). 0이면 해당 sleeve 제외 |
| `universe_spec` | 생략하면 최상위 계좌 universe 사용 |
| `weight_spec` | 기존 비중 spec. `hedge`·`defensive_sleeve`·중첩 `strategy_sleeves`는 거부 |

동작 규칙:

- 피처는 `shared_feature_spec_template`(또는 `feature_spec_template`)에서 한 번 계산해 모든 sleeve가 쓴다.
  피처·가격·경고 데이터의 종목 범위는 최상위 계좌 universe가 정한다. sleeve의 `allowed_markets`가
  그 범위 밖이면 오류다. sleeve가 `exclude_warnings`를 쓰려면 계좌 universe도 `exclude_warnings`를 켜야 한다.
- sleeve마다 기존 순서대로 universe → 비중을 계산한다. sleeve는 종목에 처음 0이 아닌 목표를 낸 시각부터
  그 종목을 소유한다. 그 이전의 요청(빈 포지션에 대한 0)은 요청 없음으로 바꿔, 이후 데이터가 과거 요청을
  바꾸지 못하게 한다. 실행 가격은 소유 종목의 합집합만 불러온다(`prune_inactive_price_columns`와 무관).
- 기간 자르기는 모든 sleeve에 같은 시작 행을 적용한다. `first_weight`이면 어느 sleeve든 처음 목표를 낸 행이다.
  그 뒤 sleeve별로 `change_only` 변환(범위 `portfolio`/`asset` 포함)을 단독 실행과 같게 적용하고 결합한다.
- 결합: 어떤 시각에 종목을 소유한 sleeve 중 하나라도 요청하면 `Σ capital × sleeve의 최신 목표 비중`을
  주문하고, 아무도 요청하지 않으면 `NaN`(수량 유지)이다. 첫 요청 전 sleeve는 0으로 더한다.
  같은 종목의 롱·숏은 합산되어 순포지션으로 상쇄된다.
- 근사: 종목을 공유하는 sleeve들의 요청 시각이 다르면, 요청하지 않은 sleeve의 몫은 가격 변동 drift가 아니라
  마지막 목표 비중으로 다시 맞춰진다. 종목이 겹치지 않거나 같은 시각에 함께 리밸런싱하면 근사가 없다.
- 각 sleeve 목표는 계좌 전체 자산 기준이다. sleeve별 가상 자산은 추적하지 않는다.
- 엔진 단계 옵션(`position_exposure_cap`, 수수료, 레버리지, `margin_mode`)은 결합된 순포지션에 계좌 전체로 적용된다.
- `preselection`과 함께 쓸 수 없다. sleeve universe 결과는 그리드 universe 캐시에 저장하지 않는다.
- 워크포워드·PBO·SPA 구간: sleeve마다 전체 기간 비중을 만든 뒤 구간 `[시작, 끝)`을 잘라 sleeve별 `change_only`를 적용하고
  결합한다(단일 전략 구간과 같은 규칙: 구간 시작 시 빈 계좌, 관측 데이터 정책은 구간 기준, holding은 구간 이후 청산 무시).
  구간 안에서 0이 아닌 요청이 없는 종목은 제외한다. 소유 시작은 처음 0이 아닌 목표 시각이라 구간 이후 데이터가
  구간 결과를 바꾸지 않는다.
- 메모리·속도: sleeve 비중은 요청 행만 압축해 보관한다(`change_only`는 전체 기간 변환 행을 보관하고 구간 시작 상태를
  복원). 공유 피처는 어느 sleeve·universe·백테스트 설정도 참조하지 않는 중간 피처를 정리한다. sleeve config는
  `save_run_artifacts`를 켜도 정리하며(일반 config는 기존대로 전체 보관), 워크포워드도 sleeve config일 때만 같은
  정리를 한다. 워크포워드는 같은 피처·경고 객체, sleeve 비중 설정, 구간 데이터 정책 적용 후 universe, holding 구간이
  같으면 sleeve 준비 결과를 구간 간에 재사용한다.

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
| `engine` | `vectorbt`(기본) 또는 선택 설치한 `freqtrade` 선물 백테스트 |
| `timeframe` | `daily` 또는 `60m` 같은 분 단위 주기. 생략하면 기존 가격 디렉터리 이름으로 추정한다. |
| `init_cash` | 초기 계좌 자산. 기본 `1000000`; USDT 선물에서는 단위가 USDT다. |
| `freqtrade` | 아래 선물 어댑터 설정. `engine="freqtrade"`일 때만 사용한다. |
| `execution_availability` | 거래정지·상장폐지 체결 처리. 아래 절 참조. `engine="vectorbt"` 전용, 생략하면 꺼짐 |

### 거래정지·상장폐지 체결 처리 (`execution_availability`)

```json
"execution_availability": {"volume_column": "candle_acc_trade_volume"}
```

`source_cache_dir`의 `<volume_column>.parquet`를 읽어 체결 단계에서만 적용한다. 캐시와 피처 값은 바꾸지 않는다.

- 거래 가능 bar = 실행 가격이 유한하고 거래량이 0보다 큰 bar. 거래량이 결측이면 거래 불가로 본다.
  가격은 있는데 거래량 관측이 전혀 없는 종목은 오류로 중단한다.
- 거래 불가 bar의 목표 비중 요청(매수·매도 모두)은 그 종목의 다음 거래 가능 bar에 같은 목표 비중으로 체결한다.
  그 전에 새 요청이 오면 새 요청으로 대체한다. vectorbt `targetpercent`이므로 미룬 매수 수량은 체결일 계좌 자산 기준이다.
  보유가 없고 대기 요청도 없을 때의 0 요청은 주문 없음으로 둔다.
- 보유 목표가 남은 종목의 가격이 이후 끝까지 나오지 않으면, 가격이 끊긴 첫 bar에 마지막 유효 가격으로 0까지 매도해 정산한다.
  수수료·슬리피지는 일반 매도와 같이 적용한다. 합병 신주 전환·실제 청산 대금은 재현하지 않는 근사다.
  "끝까지 나오지 않음" 판정에는 실행 구간의 이후 가격을 쓴다. 가격이 끊겼다 다시 나오면 위의 거래 불가 규칙을 따른다.
- 정산 시점까지 체결되지 못한 0이 아닌 대기 요청(신규 매수 또는 보유분 리밸런싱)은 취소로, 실행 구간 끝까지
  거래 불가인 대기 요청은 미체결로 집계한다. 보유분은 취소와 별개로 위 규칙대로 정산된다.
- 그리드·워크포워드·단일 백테스트 요약에 `Deferred Non-Tradable Requests`, `Superseded Deferred Requests`,
  `Unfilled Requests At End`, `Canceled Requests At Delisting`, `Delisting Settlements`를 기록한다.
  그리드는 산출물 저장 시 `execution_availability_audit.json`(정산 종목·날짜·가격 포함)을 남긴다.

### 선물 실행 엔진과 검증 연결

`backtest_spec_template`는 그리드, PBO, SPA, 워크포워드가 함께 읽는다.
기존 `vectorbt_spec_template`도 동일하게 지원한다. 엔진을 생략한 기존 config는
계속 vectorbt를 사용한다. 피처·유니버스·비중 계산은 기존 v2 함수를 재사용하며,
PBO 조합, SPA bootstrap, WF 후보 선정 방식은 바뀌지 않는다.

선택 의존성은 `python -m pip install -r requirements-futures.txt`로 설치한다.
검증 대상은 Freqtrade `2026.9`이다. 이 어댑터는 해당 버전의 내부 백테스트
호출부를 사용하므로 다른 버전은 자동으로 허용하지 않는다. 기존 환경에서
선물 엔진을 사용하지 않으면 추가 설치가 필요 없다.

```json
"backtest_spec_template": {
  "engine": "freqtrade",
  "timeframe": "60m",
  "price_column": "opening_price",
  "init_cash": 1000,
  "fees": 0.0005,
  "slippage": 0,
  "periods_per_year": 8760,
  "benchmark_market": "BTC/USDT:USDT",
  "benchmark_mode": "single_market",
  "freqtrade": {
    "exchange": "binance",
    "margin_mode": "isolated",
    "leverage": 2,
    "funding_interval_hours": 8
  }
}
```

`freqtrade`에서 지원하는 키:

| 키 | 기본값과 의미 |
|---|---|
| `exchange` | `binance`. Freqtrade에서 지원하는 거래소 이름. 거래소별 지원 범위는 Freqtrade를 따른다. |
| `margin_mode` | `isolated`; `cross`도 선택 가능. 교차증거금 백테스트의 계좌 상호 영향은 Freqtrade의 제한을 따른다. |
| `leverage` | `2`. 거래소 증거금 배수인 양의 정수. 계좌 대비 실제 노출은 목표비중으로 정한다. |
| `pair_map` | `{}`. 캐시 종목명을 Freqtrade 쌍으로 매핑. 예: `{"BTCUSDT":"BTC/USDT:USDT"}`. 원천 데이터 자체는 해당 선물의 데이터여야 한다. |
| `funding_interval_hours` | `8`. UTC 자정 기준 예상 정산 간격. 하루를 나누는 양수이며 실제 시장의 정산 일정에 맞춰야 한다. |
| `funding_schedule` | 없음. 기간별 `{"start": "UTC 시각", "interval_hours": 간격}` 배열. 있으면 고정 `funding_interval_hours` 대신 이 일정을 검증한다. |
| `rebalance_tolerance` | `0.01` USDT. 이 금액 이하의 수량 차이는 재조정하지 않는다. 거래소 수량 정밀도도 적용한다. |
| `liquidation_buffer` | `0`. Freqtrade가 청산가에 적용하는 완충 비율. |
| `metadata_dir` | `data/research/freqtrade_metadata_v2`. 공개 계약 정보·증거금 구간 캐시 위치. API 키를 받지 않는다. |

`cross`의 지갑잔액은 Freqtrade 실현손익에 열린 거래의 정산된 펀딩과 남은 포지션의
진입수수료를 반영한다. 부분청산 손익에 이미 포함된 비용은 다시 차감하지 않는다.
미실현손익은 지갑잔액에 넣지 않는다. 목표 주문 수량에는 별도로 계산한 마크 평가자산을 사용한다.
정산 후와 체결·부분청산이 완료된 후 지갑을 갱신하고 Freqtrade의 기존 청산 함수를
다시 호출한다. 매 봉의 지갑잔액 + 미실현손익을 기존 평가 장부와 대사하며 불일치는 오류다.
`futures_account.csv` 또는 `.parquet`의 `wallet_balance`는 이 보정된 cross 지갑잔액이다.
이 보정은 내부 클래스 상속을 사용하는 버전 고정 연동이며 공식 전략 콜백만의 구현은 아니다.
설치된 Freqtrade 파일은 수정하지 않는다. 격리증거금의 담보 배분과 여러 교차 포지션 간
가격·증거금 상호 영향은 기존 Freqtrade 모델의 범위와 제한을 유지한다.

피처에서 숏 신호를 만들 때도 기존 `custom:` 원천, `select_by_state`,
`position_score_feature` 등의 범용 기능을 사용할 수 있다. 어댑터 입력은
완성된 부호 있는 목표비중이다. `+1.1`은 계좌 자산 대비 롱 110%, `-0.5`는
숏 50%이고, `leverage=2`는 그 포지션의 증거금 배수다. 전략 지표에 펀딩을
사용하려면 기존 피처의 `source="custom:funding_rate"`처럼 지정할 수 있다.

`source_cache_dir`에는 같은 종목 column과 UTC timestamp index를 가진
다음 wide parquet가 필요하다. 기존 `scripts/build_price_only_source_cache.py`의
`--output-column-name`으로 각 원천 CSV를 변환할 수 있다. 바이낸스 USDT
무기한 선물 분봉·1시간봉은 아래 수집기로 wide cache를 직접 만들 수 있다.

- `opening_price`, `high_price`, `low_price`, `trade_price`: 선물 OHLC.
- `candle_acc_trade_volume`: 거래량.
- `mark_price`: 각 bar 시작 시점의 마크가격. 종가 마크를 같은 시각의 시가처럼 넣지 않는다.
- `funding_rate`: 실제 정산 시각의 비율. 그 외 시각은 NaN 또는 행 생략.

#### Numba 선물 엔진: `engine="numba_futures"`

기존 피처·유니버스·비중 입력을 그대로 사용하는 선택형 연구 엔진이다.
`backtest_spec_template.engine`을 `numba_futures`로 지정하고 같은 객체 아래에
`numba_futures` 설정을 넣는다. 단일 CLI는 `--engine numba_futures
--numba-futures-spec-json <설정 JSON>`을 사용한다. 기존 엔진의 기본값은 바뀌지 않는다.
설치는 공통 `requirements.txt`의 `numba==0.68.0`을 사용하며 Freqtrade 설치나
거래소 API 연결은 필요하지 않다. 최초 실행에는 Numba 컴파일 시간이 추가된다.

지원 범위는 USDT 선형 무기한·시장가·종목별 단일 순포지션이며 증거금은 격리(기본) 또는
계좌 전체 교차다. 격리에서는 여러 종목이 공통 USDT 현금을 공유하고 각 포지션의 담보는
분리한다. 교차는 아래 「교차증거금」 절을 따른다.
`NaN`은 수량 유지, `0`은 청산, 유한 양수·음수는 계좌 자산 대비 목표 명목노출이다.
`opening_price`, 공유 현금, `size_type=targetpercent`, `call_seq=auto`를 요구한다.
신호 지연은 기존 비중 계산에서 적용하며 엔진이 추가로 한 봉 밀지 않는다.

`portfolio_group` 벤치마크는 처음부터 대상 종목마다 동일한 초기 자금을 배정한다.
상장 전 배정분은 현금으로 유지하고 첫 유효 관측 가격에 매수한 뒤 보유한다.
기간 내 미상장 종목의 배정분도 현금으로 남는다. 상장 시 다른 종목의 자금을
재분배하지 않는다. 데이터 끝의 결측은 마지막 관측 가격으로 평가하며 별도
상장폐지 정산을 재현하지 않는다. 이 정책은 공통 선물 결과 객체에 적용되고
`single_market` 벤치마크 계산은 기존과 같다.

| `numba_futures` 항목 | 기본값·의미 |
|---|---|
| `margin_mode` | `isolated`. `cross`는 모든 대상 종목을 하나의 교차 지갑으로 운용하며 `liquidation_model=mark_open`만 허용. 종목별 혼합 모드는 미지원. 아래 「교차증거금」 참조. |
| `leverage` | `2`. 증거금 배수인 양의 정수, 또는 모든 대상 종목을 명시한 객체. 목표 노출과 별개다. |
| `contracts` / `contract_specs_json` | 둘 중 하나 필수. 종목별 계약 규칙 객체 또는 그 JSON 경로. 파일은 `{"contracts": {...}}` 형태도 허용. |
| `liquidation_model` | **명시 필수**. `mark_open` 또는 `mark_ohlc`. 아래 데이터 제약 참조. |
| `negative_margin_policy` | `insurance_cover`. 전량 종료한 포지션의 음수 반환액을 0으로 정산하고 부족액을 별도 기록하는 연구 가정. 강제청산뿐 아니라 일반·반전·최종 전량 종료에도 적용. 부분 종료는 남은 자기 담보에서 손실을 차감한다. `error`는 전량 종료의 실제 부족액 발생 시 중단하는 선택형 진단 정책. |
| `funding_interval_hours` | `8`. UTC 정산 달력. 모든 종목에 공통 기본값이며 종목별 계약 설정으로 재정의 가능. |
| `funding_schedule` | 없음. 기존 기간별 정산 일정 형식. 종목별 계약 설정으로 재정의 가능. |
| `rebalance_tolerance` | `0.01` USDT. 같은 방향의 목표 차이만 이 금액 이하일 때 생략. 0 청산·반전에는 적용하지 않음. |
| `max_holding_hours` | 생략 또는 `null`이면 비활성. 양의 유한 숫자로 최대 보유시간을 지정. 실제 첫 진입 체결부터 계산하고, 만료 이상이 되는 첫 봉 시가에 전량 청산. 부분 조정으로 시계를 초기화하지 않음. 아래 재진입 규칙 참조. |
| `partial_exit` | 생략 또는 `null`이면 비활성. 기존 0/1 피처 조건으로 실제 보유 수량의 일부를 청산. 종목별·UTC 기간별 1회 시도하고, 다음 초기화까지 증가·반전·재진입을 차단. 추가 축소·전량 청산은 허용. 아래 설정 참조. |
| `max_records` | `1000000`. 체결·펀딩·거부·취소 이벤트 수 상한. 넘으면 명시적 오류로 중단하며 기록을 버리지 않음. |
| `auto_add_margin` | `false`만 허용. |
| `save_events` | `true`. 이벤트 상세 저장. `false`여도 내부 회계 검산과 거래 집계는 수행. |
| `data_policy_preset` | `strict`. `research`는 아래 7개 데이터·종료 정책의 연구용 값을 묶어서 적용. 명시한 개별 항목이 묶음보다 우선. |
| `unverified_lifecycle_policy` | `error`. `observed_prices`는 계약 기간 미확인 종목의 실제 거래·마크 관측 구간을 연구용으로 허용. 가격·펀딩 품질 검사는 유지. 아래 설명 참조. |
| `funding_missing_policy` | `error`. `previous_observation`은 확정된 정산 시각의 누락 요율을 직전 원래 실측 요율로 메모리에서 대체. 이전 실측값이 없으면 중단. |
| `funding_transition_policy` | `error`. `observed_only`는 명시적으로 기록된 주기 변경 불확실 구간에서 실제 정산 행만 반영. 없는 정산은 만들지 않음. |
| `conflicting_price_policy` | `error`. `exclude_market`은 실행 구간과 가격 충돌 경고가 겹치는 종목 전체를 제외하고 사유를 기록. |
| `funding_start_policy` | `error`. `first_observation`은 직전 실측이 없는 초기 펀딩 누락을 첫 원래 정산 관측 시점까지 사용 구간에서 제외. 피처/상태도 그 경계부터 계산. |
| `missing_price_policy` | `error`. `exclude_market`은 거래 시가·종가·마크의 내부 가격 공백이 실행 구간과 겹치는 종목 전체를 제외. 앞/뒤의 정상 미관측 경계는 제외 사유가 아님. |
| `delisting_policy` | `error`. `last_available_open`은 종료 시각이 확인된 종목을 종료 직전 마지막 완전한 봉의 거래 시가로 정산하는 연구 옵션. 필요한 마지막 가격이 없으면 중단. |

시간청산은 기존 `numba_futures` 블록에 `"max_holding_hours": 24`처럼 추가한다. 별도 실행기나
피처가 필요 없으며, 같은 backtest 설정을 전달하는 단일 백테스트·grid·TPE·PBO·SPA·WF에서
적용한다. Freqtrade·현물 엔진이나 라이브에 자동으로 추가되는 기능은 아니다.

- 단위는 시간이다. 실행 봉 수로 올림하므로 1시간봉에 1.5를 지정하면 2시간 뒤 시가,
  5분봉에 0.1을 지정하면 10분 뒤 시가에 청산한다. 마지막 평가 시점이 먼저 오면 기존 최종
  정산을 적용한다. `0`, 음수, 문자열, Boolean, NaN/무한대는 허용하지 않는다.
- 주문 요청·거절이 아니라 **실제 첫 진입 체결**이 시작점이다. 추가 진입·부분 축소는 시계를
  유지하고, 전량 종료 후 신규 진입 또는 반전의 새 포지션은 새 시계를 시작한다.
- 기존 펀딩 정산·시가 강제청산·관측 이력/상폐 종료가 우선한다. 시간청산은 시가 주문 전
  공통 자산 스냅샷 이후, 일반 목표 주문보다 먼저 실행한다. 시간청산 반환 현금은 다른 종목의
  같은 시가 주문에 사용할 수 있지만 모든 목표 수량은 같은 청산 전 스냅샷으로 계산한다.
- 만료 시 기존 대기 주문을 취소한다. 청산 봉의 비중 증가·반전 요청으로 재진입하지 않는다.
  이후 해당 종목의 **명시적 목표 0을 한 번 관측하고, 다음 행 이후 새 비영 목표**가 와야 다시
  진입한다. 만료 봉의 0도 초기화로 인정하되 그 봉 재진입은 금지한다. NaN·같은 목표 재전송·
  반대 방향 비중만으로는 초기화되지 않는다. 다른 종목 변경 때문에 생긴 change_only 행도
  새로운 진입 사건으로 간주하지 않는다.
- 목표가 계속 비영인 전략은 시간청산 후 계속 현금으로 남을 수 있다. 시간청산이 상위 피처의
  보유 상태를 자동 변경하지는 않으며, 청산 후 대기시간만으로 반복 재진입을 허용하지 않는다.
- 체결은 기존 거래 시가·슬리피지·수수료·수량·격리담보 장부를 재사용한다. 주문/거래 종료
  사유는 `time_exit`, 차단된 비영 요청은 취소 이벤트 `time_exit_requires_flat_reset`로 남긴다.
  연속 차단은 한 번만 상세 기록하고 요청 취소 수에는 각각 반영한다.
- 사용 시 요약에 `Max Holding Hours`, `Max Holding Bars`, `Time Exits`, 메타데이터에
  `time_exit` 규칙을 기록한다. 종목별로 진입 봉 번호와 재진입 차단 여부만 추가 저장하며,
  전체 시간×종목 보유시간 행렬은 만들지 않는다. ATR 손절·익절이나 최대 4개 슬롯 관리는
  이 옵션에 포함되지 않는다.

실제 보유 포지션의 비중을 제한하려면 같은 `numba_futures` 블록에 선택 옵션을 넣는다.
이는 `weight_spec`의 목표비중 생성과 별개인 Numba 선물 엔진 전용 보유 중 위험 관리다.

```json
"position_exposure_cap": {
  "long": {"trigger": 0.20, "reduce_to": 0.15},
  "short": null,
  "reset_frequency": "weekly"
}
```

- 기본값은 미사용이다. 객체 생략·`null`·양쪽 방향 미설정/`null`은 검사·상태·주문을 추가하지
  않는다. `long`·`short`는 독립적으로 켜며 각 방향은 양의 유한 `trigger`,
  `0 <= reduce_to < trigger`가 필수다. 비율은 증거금 비율이 아닌
  `abs(실제 수량) × 현재 봉 마크 시가 / 공통 계좌 자산`이다. 1을 넘는 노출도 표현할 수 있다.
- 매 봉 펀딩·시가 강제청산·이력 종료 후의 기존 공통 자산 스냅샷을 사용한다. 시간청산·최종
  정산·기존 조건부 일부 청산이 우선하며, 명시적 목표 0은 비중 축소 대신 전량 청산한다.
  상한 검사는 일반 목표 주문 전에 실행하고, 같은 봉의 다른 종목 축소 비용으로 판단 기준을
  바꾸지 않는다. 현재/미래 봉의 고저가·종가는 사용하지 않는다.
- `trigger + 1e-12`를 넘으면 남길 수량을 `reduce_to × 공통 자산 / 마크 시가`로 계산하고
  수량 단위로 내림한다. 거래 시가·슬리피지·가격 단위·수수료·격리담보 처리는 기존 체결을
  재사용한다. 반환 담보와 실현손익에서 비용을 뺀 돈은 공통 현금으로 돌아간다. 현금이
  생겼다는 이유로 다른 종목의 새 목표를 생성하지 않으며, 기존 대기/신규 주문은 사용할 수 있다.
- 상한 초과 시 해당 종목의 이전 대기 주문을 취소하고 증액·직접 반전·같은 방향 재진입을
  초기화까지 막는다. 같은 방향의 추가 수량 축소와 목표 0은 허용한다. 전량 축소한 같은 봉의
  반대 방향 재진입도 금지한다. 이후 봉에서 새로 요청한 반대 방향은 그 방향 규칙을 따른다.
  `reset_frequency` 기본은 `weekly`이며 `daily`, `monthly`, 양의 정수 `Nd`, 위상 오프셋 `Nd+K`도 지원한다.
  UTC 기준 기존 리밸 캘린더를 재사용한다. 초기화가 새 주문을 만들지는 않는다.
- 최소 주문·수량 제한에 걸리면 사유와 거부 시도를 기록하고 다음 봉에도 초과하면 재시도한다.
  반복된 동일 거부는 상세 이벤트를 중복 저장하지 않는다. 전량 청산은 기존처럼 최소 주문
  제한을 면제한다. 수량 내림 때문에 잔여 수량이 0이면 전량 청산할 수 있다.
- 봉중 연속 감시나 비용 차감 후의 엄격한 상한 보장은 아니다. 신규 진입·증액 결과는 다음 봉에
  검사한다. 봉중 급등·갭·상태 변화로 상한을 넘을 수 있으며, 격리담보의 감소로 남은 포지션이
  청산될 가능성도 기존 장부·유지증거금 규칙으로 판정한다.
- 체결 이벤트는 `position_exposure_cap`, 차단 주문은
  `position_exposure_cap_requires_reset`이다. 켠 경우에만 요약에
  `Position Exposure Cap Exits`와 롱·숏 건수, 메타데이터에 판단·체결·초기화 정책을 저장한다.
  배치 후보 사이에는 읽기 전용 설정·초기화 배열만 공유하고 종목별 잠금 상태는 공유하지 않는다.

조건부 일부 청산은 기존 `numba_futures` 블록에 다음 객체를 추가한다. 조건은 기존
`feature_spec`/`feature_spec_template`의 `compare`, `logical`, 가격·수익률 계산으로 만든다.
`signal_feature`는 **비중이나 연속 점수가 아닌 0/1 피처 이름**이며 NaN은 신호 없음이다.

```json
"partial_exit": {
  "signal_feature": "surge_signal",
  "fraction": 0.5,
  "signal_lag": 1,
  "side": "long",
  "reset_frequency": "weekly"
}
```

롱·숏의 조건을 다르게 쓰려면 같은 위치에 다음 방향별 형식을 쓴다. 조건 피처는 기존
피처 그래프로 각각 계산하며, 아래 이름 자체가 상승·하락 기준을 정하지는 않는다.

```json
"partial_exit": {
  "long": {
    "signal_feature": "long_trim_signal",
    "fraction": 0.5,
    "signal_lag": 1,
    "reset_frequency": "weekly"
  },
  "short": {
    "signal_feature": "short_trim_signal",
    "fraction": 0.3,
    "signal_lag": 1,
    "reset_frequency": "daily"
  }
}
```

`long`·`short` 중 필요한 방향만 적거나 사용하지 않는 방향을 `null`로 둘 수 있다.
각 방향의 `signal_feature`·`fraction`은 필수이고, 지연·초기화 기본값은 각각 1·`weekly`다.
중첩된 규칙에는 `side`를 쓰지 않는다. 단일형의 `signal_feature`/`side` 등과 방향별 키를
같은 객체에 섞으면 오류다. 둘 다 `null`이면 일부 청산을 실행하지 않는다.

방향별 형식은 **종목·방향별로 발동과 초기화 상태를 독립 관리**한다. 발동한 방향의
증가·직접 반전·같은 방향 재진입은 해당 방향의 초기화까지 차단한다. 명시적 목표 0으로
전량 청산한 뒤에는 반대 방향이 잠겨 있지 않으면 그 방향으로 진입할 수 있다. `fraction=1`로
전량 청산해도 같은 봉의 직접 반전은 차단한다. 전량 청산 이후 봉에 새로 들어온 반대 방향
목표는 해당 방향의 잠금 상태를 따르며, 차단된 목표를 자동으로 재시도하지 않는다. 숏의 일별
초기화가 롱의 주간 발동 상태를 풀지는 않는다. 기존 단일형은 종전대로 종목 전체를 잠근다.
이는 한 종목에서 롱·숏을 동시에 보유하는 기능이 아니며, 기존 순포지션 구조를 유지한다.

- `fraction`은 필수이며 `0 < fraction <= 1`의 유한 숫자다. `0.5`는 당시 **실제 보유
  수량의 절반 청산**이며 목표비중 0.5배와 다르다. 수량 단위로 내림하고, 최소 수량·최소
  주문금액 미달은 거부 사유를 기록한다. 자동으로 전량 청산하거나 다음 봉에 재시도하지 않는다.
- `signal_lag`는 기본 1, 양의 정수다. 원래 피처의 UTC 시각에서 지정 봉 수를 지연해
  다음 시가에 주문하며, 평가 구간을 자르기 전 피처 이력도 읽는다. 0·음수는 거부한다.
  지연 시간은 초기화 주기보다 짧아야 한다(일 24시간, 주 168시간, 월은 최단 월 28일 기준).
  일봉·일별 초기화나 1시간봉·주간 초기화·지연 168 이상처럼 기간 안에 신호가 남을 수 없는
  조합은 시작 시 오류로 거부한다. 일봉은 주간/월간 초기화 등 더 긴 주기를 선택한다.
  피처 자체의 미래 정보 누출을 자동 판별하지는 않으므로 확정된 자료로 조건을 만들어야 한다.
- 단일형의 `side`는 기본 `long`, `short`와 `both`도 지원한다. 숏의 일부 청산은 매수로 수량을 줄인다.
  실제 포지션이 없거나 지정 방향과 다르면 주문하지 않는다. 피처에는 실행 종목 열이 모두
  있어야 하며, 일부 종목만 대상으로 삼으려면 다른 종목의 조건을 0으로 만든다.
- `reset_frequency`는 기본 `weekly`, `daily`·`monthly`도 지원한다. UTC 일·ISO 주(월요일
  00:00)·월 경계에서 종목별 발동 상태를 초기화한다. 이 주기는 기존 종목 선정·비중 리밸
  주기를 바꾸지 않는다. 이전 기간에서 지연되어 넘어온 신호는 새 기간에 사용하지 않는다.
  같은 기간에는 조건이 사라졌다가 다시 참이어도 재매도하지 않으며, 주문 거부도 1회 시도에
  포함한다. 별도 백테스트/WF 계좌의 시작에서는 발동 상태를 새로 시작한다.
- 펀딩·시가 강제청산·관측 종료·시간청산·마지막 봉 전량 정산이 우선한다. 같은 봉의 명시적
  목표 0도 전량 청산하므로 일부 청산을 생략한다. 일부 청산은 기존 시가·슬리피지·수수료·
  담보 반환을 재사용하고, 이후 유지증거금을 다시 검사한다. 반환 현금은 다른 종목의 같은
  시가 주문에 사용 가능하며 목표 수량 계산은 기존 공통 사전 자산 스냅샷을 유지한다.
- 단일형은 발동하면 해당 종목의 기존 대기 목표를 취소하고, 다음 초기화까지 **수량 증가·반전·청산 후
  재진입 요청을 차단**한다. 새 목표는 기존 공통 자산 스냅샷으로 수량을 계산해 현재 실제
  보유 수량과 비교한다. 같은 방향의 추가 축소와 동일 수량 유지, 명시적 0 전량 청산은 허용하며
  기존 최소 주문·수량 단위 규칙을 따른다. 목표비중 숫자만 비교하지 않는다. 따라서 다른
  종목 변경으로 목표가 재전송되어도 수량이 늘지 않는다. 거부한 증가 요청은 안전한 기존 축소
  대기 주문을 교체하지 않는다. 초기화 후에도 실제 새 목표 요청이 있어야 재조정하며, 축소 전
  수량을 자동 복원하지 않는다. 다른 종목은 그대로 처리한다.
- 체결 사유 `partial_exit`, 증가·반전·재진입 차단 사유 `partial_exit_requires_reset`, 요약
  `Partial Exits`·`Long Partial Exits`·`Short Partial Exits`, 메타데이터 `partial_exit`에 규칙과
  지연 후 신호 해시, `state_scope`(`per_market`/`per_market_and_side`)를 기록한다.
  `save_events=false`이면 상세 신호/거부 이력 파일은 저장하지 않지만 집계·메타데이터는 남긴다.
- Numba 선물의 단일·grid·grid TPE·PBO·SPA·WF 경로가 피처를 전달한다. Python API는
  `run_backtest_v2(..., feature_frames=frames)` 또는 Numba 단일/배치 API의 같은 인자를 쓴다.
  배치는 모든 후보가 같은 일부 청산 신호·설정을 공유할 때 사용한다.
  그리드는 백테스트 설정에서만 참조하는 공통 청산 피처도 보존하며,
  `save_run_artifacts`는 상세 산출물 저장을 조절한다. 전략 조립 탐색기는
  해당 후보에서 이미 생성하는 피처 이름을 사용해야 하며 조건을 별도로 자동 생성하지 않는다.
  Freqtrade·현물 엔진·실거래에는 적용하지 않는다. 헤지 목표를 실제 축소 수량에 맞춰 다시
  계산하는 기능도 포함하지 않는다.

예를 들어 아래 기존 피처 뒤에 `surge_signal` 비교를 붙이면 **직전 168개 시간봉 대비
수익률이 +200% 이상인 조건**을 사용한다. 주초 가격 기준이나 실제 체결가 기준 수익률과는
다르며, 어떤 기준으로 조건을 만들지는 피처에서 정한다. 수익률 조건은 아래처럼 별도로
작성해야 하고 `partial_exit` 옵션이 가격 기준이나 임계값을 암묵적으로 정하지 않는다.
실제 체결 진입가는 피처 계산부로 되돌려 받지 않으므로, 체결가 기준 수익률 조건을 이
설정만으로 생성하는 기능은 포함하지 않는다.

```json
[
  {"source": "trade_price", "column_name": "return168", "steps": [
    {"kind": "simple_return", "params": {"window": 168, "missing_policy": "preserve"}}
  ]},
  {"column_name": "surge_signal", "compare": {
    "left_feature": "return168", "operator": "ge", "right_value": 2.0
  }}
]
```

계약 기간 미확인 종목을 연구에 사용하려면 기존 `numba_futures` 객체에
`"unverified_lifecycle_policy": "observed_prices"`를 추가한다. 공용 관측 캐시는 실제 값을
보존하고 `instruments.json`에 `data_usage_basis="observed_prices"`, `data_start_inclusive`,
`data_end_exclusive`를 기록한다. 이 구간은 거래 시가·종가와 마크 시가가 함께 양수·유한한
첫 봉부터 마지막 봉까지이며, 공식 상장·상폐 기간으로 인증하지 않는다. 기존에 전체 열이
가려진 캐시는 종목별 체크포인트로 한 번 갱신해야 하며, 원본 재다운로드는 필요 없다.

구간 전후의 피처·상태·선택·목표비중을 제한하고 엔진에서는 같은 구간의 거래·마크가격만
사용한다. 마지막 사용 가능 봉에서는 펀딩·시가 위험 판정 후 남은 포지션을 거래 시가에
정산하며 수수료·슬리피지를 적용한다. 그 봉의 새 진입은 실행하지 않고 대기 주문도 취소한다.
이것은 데이터 종료에 따른 연구용 강제 정산으로, 실제 상폐 정산이나 사전에 알 수 있었던
청산 신호가 아니다. 이후 데이터를 추가하면 과거 데이터 종료 봉의 강제 정산은 바뀔 수 있다.
`futures_metadata.json.unverified_lifecycle_assumptions`에 종목·관측 경계·정산 가정을 남기며,
엔진에서 직접 배치 실행하거나 준비 데이터를 재사용해도 같은 정책을 적용한다.

이 옵션은 `unresolved_lifecycle`만 허용한다. 내부 가격 공백·가격 충돌·불완전한 펀딩·계약
주문/증거금 규칙 부족은 계속 오류다. 자동 펀딩 대체나 충돌 종목 자동 제외 옵션이 아니다.
원본의 `lifecycle_verified=false`, `onboard_time_utc=null`, `tradable=false`는 그대로 보존한다.
따라서 전략이 원본 `custom:tradable`을 필수 조건으로 쓰면 해당 종목은 여전히 선택하지 않는다.
기본 실행과 Freqtrade/vectorbt 전략 실행은 미확인 계약을 계속 거부한다. 넘바 실행에서 계산한
벤치마크도 같은 사용 정책과 관측 경계를 따르며, 미확인 구간을 공식 거래기간으로 바꾸지 않는다.

데이터 연구 정책을 함께 사용하려면 기존 `numba_futures` 객체에 아래 항목 하나를 추가한다.
계약 규칙·레버리지·체결 설정은 기존 값을 유지한다. 새 실행기나 대체 캐시를 만들지 않는다.

```json
{
  "data_policy_preset": "research"
}
```

`research`는 `unverified_lifecycle_policy=observed_prices`,
`funding_missing_policy=previous_observation`, `funding_transition_policy=observed_only`,
`conflicting_price_policy=exclude_market`, `funding_start_policy=first_observation`,
`missing_price_policy=exclude_market`, `delisting_policy=last_available_open`을 기본으로 선택한다.
예를 들어 같은 객체에 `"delisting_policy": "error"`를 명시하면 나머지는 연구용으로
처리하되 확인된 계약 종료의 자동 정산은 허용하지 않는다. 설정을 생략하거나 `strict`를
선택하면 종전처럼 7개 모두 `error`가 기본이며, 기존 개별 옵션만 쓰는 config도 유지한다.
잘못된 preset/개별 값은 오류로 처리한다. 프리셋이 피처·유니버스·그리드·WF·탐색·엔진에서
같게 해석되며, 원래 입력 config를 덮어쓰지 않는다. 선택한 묶음과 실제 적용한 7개 값은
결과의 `data_policy_audit.policy_preset`, `effective_policies` 및 저장 metadata에 남는다.
개별 처리의 가정과 차단 조건은 아래 설명을 따른다. 라이브·다른 엔진에는 적용하지 않는다.

펀딩 정책은 원본 Parquet을 수정하지 않는다. 확정된 정산 시각에만 요율을 보충하고,
매 봉에 요율을 전파해 비용을 부과하지 않는다. 실행 시작 전의 실측 요율도 같은 사용 이력
안에 있으면 사용할 수 있으며, 연구 시작 경계 이전의 구계약 관측은 빌려오지 않는다.
연속 누락 두 건을 채워도 두 번째의 출처는 첫 대체값이 아니라 원래 실측 행이다.
직전 실측값이 없으면 미래 실측값·0으로 대체하지 않는다. 초기 구간 제외 옵션을 생략하면
그대로 중단한다. 이 방식이 항상 비용을
크게 잡는 것은 아니며 추정한 정산 요율이라는 가정이 결과에 남는다.

`observed_only`는 캐시의 `funding_transition_uncertainties` 또는
`quality_intervals.unverified_funding_transition`으로 명시된 경계만 처리한다. 그 안의
실제 정산은 유지하고 없는 정산을 추가하지 않는다. 경계 밖 누락은 별도의
`funding_missing_policy`가 처리한다. 주기 자체가 미확인인 `unverified_funding_calendar`,
경계 밖의 예상하지 못한 정산·잘못된 숫자는 여전히 오류다.

펀딩 연구 정책 중 하나를 켜면 명시한 계약/엔진의 정산 설정을 우선한다. 명시한 설정이
없으면 같은 캐시 `instruments.json`의 종목별 일정/주기를 재사용하고, 그것도 없으면 기존
8시간 기본값을 사용한다. 사용한 일정과 출처를 audit에 기록한다. 정책을 모두 생략하면
기존 엄격 정산 검증 및 기본 일정 동작을 유지한다.

충돌 제외는 정상 봉만 골라 같은 종목을 계속 매매하는 방식이 아니라 해당 종목 전체를
그 실행에서 제외하는 방식이다. 기존 단일 실행·그리드·WF는 순위 선정 전에 제외하므로
남은 유니버스로 기존 비중 규칙을 계산한다. 이미 생성된 목표비중을 엔진에 직접 넘겼다면
제외 종목 비중만 제거하고 다른 비중을 임의로 키우지 않는다. WF는 각 평가 구간의 경고를
사용한다. grid의 timestamp 시작일은 반영하며, 첫 비중으로 시작을 정하는 모드에서는
선택 전의 잠재 실행 구간에 대해 보수적으로 제외한다. 단일 종목 탐색에서 그 종목이
제외되거나 실행 대상 전체가 제외되면 성공한 현금 전략으로 꾸미지 않고 명시적으로 중단한다.
직접 선택한 단일 벤치마크 또는 고정 벤치마크 자산이 충돌하면 임의로 다른 기준선을
고르지 않으며, 해당 벤치마크 설정/구간을 바꿔야 한다. 전략에 지정한 필수 헤지 자산이
충돌하거나 내부 가격이 누락될 때도 헤지를 조용히 생략하지 않고 중단한다.

종목별 대체 시각·요율·직전 원래 관측 시각, 실기록 정산 가정의 구간·생략 시각,
충돌 제외 종목·원인·구간은 `futures_metadata.json.data_policy_audit`에 기록한다.
요약의 `Replaced Funding Observations`, `Observed-Only Funding Intervals`,
`Excluded Conflicting Markets`는 건수이며 `Data Policy Audit`는 같은 내역의 JSON 문자열이다.
따라서 `save_run_artifacts=false`인 grid도 `summary_results.csv`에, WF도 구간 결과 CSV에
내역이 남는다. 자동 전략 탐색은 후보 metrics의 `data_policy_audit`를 trial JSON에 저장한다.
준비 데이터 재사용과 탐색 재개 지문에는 캐시의 instruments/manifest 변경도 반영한다.

초기 펀딩 누락을 제외하려면 `funding_start_policy="first_observation"`을 사용한다.
캐시의 초기 `missing_funding` 경고 전에 같은 사용 이력의 실측이 없을 때 해당 종목의 첫
원래 정산 관측부터 사용한다. 계약 기간 미확인 종목은 관측 가격 경계와 캐시의 정산 달력을
기준으로 정산이 확인되지 않은 앞부분도 제외할 수 있다. 이는 공식 개장/첫 정산의 인증이
아니며 audit에 근거를 기록한다. 원래 펀딩을 대체하거나 가격을 채우지 않는다.

단일/grid/WF는 피처 입력과 중간 상태부터 경계를 적용하므로 이전에 만들어진 보유 상태를
이어받지 않고 이동평균도 새 구간에서 워밍업한다. 공유 피처/후보별 피처 및 유니버스 캐시
키에도 이 경계를 반영한다. 자동 전략 탐색은 먼저 공통 소스의 앞부분을 잘라낸 뒤 기존
분할/워밍업을 계산한다. 완성 비중을 엔진에 직접 넘기면 경계 전 목표를 0으로 처리하지만
외부에서 이미 계산한 피처/상태를 되돌려 계산하지는 않는다. 포트폴리오 전체 평가 시작일은
임의로 변경하지 않으므로, 여러 자산의 미편입 기간은 기존처럼 현금으로 남을 수 있다.
관측이 전혀 없거나 첫 정산 이후에도 누락이 남으면 각각 명시적으로 중단하거나 기존
`funding_missing_policy`로 처리한다. 중간 누락을 건너뛰어 가장 좋은 시작일을 고르지 않는다.

`missing_price_policy="exclude_market"`은 원래 관측 가격에서 앞뒤로 공동 가격이 있는
내부 구멍을 찾는다. `quality_intervals`에 누락 표시가 없는 계약 기간 미확인 종목도
검사하며, 세 필드 중 일부만 NaN이거나 요청 구간 전체가 구멍 안에 있어도 제외한다.
초기 펀딩 사용 경계 이전의 구멍은 실행 대상이 아니므로 제외하지 않는다. 정규 상장 전·
관측 종료 뒤의 NaN만으로는 종목을 버리지 않는다. NaN 가격을 만들거나 보간하지 않으며,
음수/무한대 같은 잘못된 가격을 정상값으로 바꾸는 옵션도 아니다.

시작 변경은 `data_policy_audit.funding_start_adjustments`에 이전/새 경계와 누락 근거를,
가격 공백 제외는 `excluded_markets`에 필드·UTC 구간·봉 수를 기록한다. 요약의
`Funding Start Adjusted Markets`, `Excluded Missing-Price Markets`와 기존 JSON audit에도
남는다. 필수 헤지/선택한 벤치마크 또는 전체 자산이 제외되면 임의의 대체 전략을 만들지 않는다.

종료 시각이 확인된 종목을 정산하려면 기존 `numba_futures` 객체에
`"delisting_policy": "last_available_open"`을 추가한다. `instruments.json`에서
`lifecycle_verified=true`이고 `known_end_exclusive_utc`가 기록된 종목만 해당한다.
피처·유니버스·목표와 관측 캐시 벤치마크는 기록된 계약 종료 경계를 넘겨 사용하지 않는다.
이 공통 데이터 사용 경계는 정산 옵션이 꺼져 있어도 유효하며, 종료 뒤 값이 남아 있다는
이유로 거래 가능 기간을 늘리지 않는다. 자동 정산 허용 여부만 위 넘바 옵션으로 선택한다.
엔진은 수집기와 같이 종료 시각을 봉 간격으로 내림한 뒤, 바로 앞 완전한 봉의
시가에서 펀딩·시가 위험 판정을 먼저 수행하고 남은 포지션을 닫는다. 수수료·슬리피지와
기존 격리담보 정산 규칙을 적용하고, 대기 주문을 취소하며 그 봉과 이후의 신규 진입을
막는다. 다른 종목은 기존 시계열을 그대로 사용한다.

요청 구간이 그 종료 직전 봉을 포함할 때만 적용하며, 더 이른 평가 종료는 기존 최종
정산으로 처리한다. 필수 거래 시가 또는 마크 시가가 없으면 오류를 내고, 데이터 꼬리의
임의의 더 오래된 가격으로 정산하지 않는다. 종료 시각 미확인 종목에는 이 옵션만으로
관측 종료 정산을 허용하지 않는다. 해당 경우는 기존 `unverified_lifecycle_policy`가 담당한다.

청산 주문 사유는 `delisting_last_open`, 적용 경계는
`futures_metadata.json.data_policy_audit.delisting_settlements`와 summary의
`Data Policy Audit`에 남으며 `Delisting Boundary Markets`는 적용한 종목 수다
(실제 보유·청산 횟수는 주문 기록으로 확인). 후보별 상세 저장을 끈 grid/WF에도 audit가
남는다. 공식 상폐 정산가격이나 과거 시점의 공지 이력을 복원한 것이 아니라, 알려진
계약 종료 시각을 사용하는 연구 가정이다. 캐시·원본·공식 상폐 정산 완료 표시는 바꾸지 않는다.

이 정책들은 넘바 엔진 전용이며 기본은 모두 `error`다. 실행 계약 규칙 부족은 그대로
중단하며, 옵션을 켠 것만으로 전체 종목의 데이터가 완전해지는 것은 아니다. 실거래 경로는 사용하지 않는다.

각 종목 계약에는 `amount_step`, `price_tick`(양수), `min_qty`, `min_notional`,
`liquidation_fee`(비율), `maintenance_brackets`가 필요하다. 유지증거금 구간의
필수 항목은 `notional_cap`, `maintenance_margin_rate`, `maintenance_amount`,
`max_leverage`다. 명목 상한은 증가하고 유지증거금 함수는 구간 경계에서 연속이어야 한다.
`source`, `as_of`, `funding_interval_hours`, `funding_schedule`도 종목별로 기록할 수 있다.
거래소의 실제 조회값을 공급해야 하며 오늘의 계약 조건을 과거 전체 이력으로 간주하지 않는다.
공급한 구간을 벗어난 진입은 거부한다. 보유 명목금액이 최종 구간을 넘으면
시가 위험 판정에서 청산을 확정할 수 있는지 먼저 확인하며, 확정할 수 없으면 중단한다.

```json
{
  "engine": "numba_futures",
  "price_column": "opening_price",
  "timeframe": "5m",
  "init_cash": 10000,
  "fees": 0.0005,
  "slippage": 0,
  "numba_futures": {
    "margin_mode": "isolated",
    "leverage": 2,
    "contract_specs_json": "data/research/contracts/binance_usdt_snapshot.json",
    "liquidation_model": "mark_open",
    "negative_margin_policy": "insurance_cover"
  }
}
```

위 경로는 사용자가 실제 계약 스냅샷으로 준비할 입력의 예시이며 자동 생성되지 않는다.
스냅샷에 필요한 숫자 형식은 `tests/test_numba_futures_v2.py`의 합성 계약 예제로
확인할 수 있지만 그 합성값을 실제 거래소 규칙으로 사용하지 않는다.
기존 그리드 템플릿과 동일하게 `contract_specs_json`에 `{version}` 같은 축 변수나
`{run_name}`을 사용할 수 있다. 그리드 TPE·랜덤 탐색의 재개 검사는 제약을 통과한
모든 후보의 실제 엔진과 치환된 계약 경로를 확인한다. 엔진명은 중앙 실행과 같이
소문자로 정규화하고, Numba 코드·버전 및 실제 계약파일 내용을 해시한다.
아직 평가하지 않은 후보의 계약이 바뀌어도 재개를 거부하며, 사용되는 계약파일이
없으면 탐색 전에 오류로 중단한다. 경로 기준은 실행 시 작업 디렉터리와 같다.

필수 가격 캐시는 `opening_price.parquet`, `mark_price.parquet`이며 정산값은
`funding_rate.parquet`다. 기존 피처 계산에 필요한 OHLC·거래량은 별도로 유지한다.
종목별 상장 전·데이터 종료 뒤의 비거래 구간은 허용하지만, 중간 가격 누락과
필수 펀딩 누락·추가 정산은 오류다. 보유 중 가격이 사라지면 중단하며
미체결 진입 대기는 데이터 종료 시 취소한다. 펀딩 시각은 실행 봉에 있어야 한다.

`mark_open`은 관측된 마크 시가에서만 청산을 검사하므로 봉중 청산을 놓칠 수 있다.
`mark_ohlc`는 **추가** `mark_high_price.parquet`, `mark_low_price.parquet`를 요구하며
시가 주문 후 불리한 극값을 검사한다. 이 모형의 봉중 청산 체결은 계산한 마크
청산가격에 슬리피지를 적용하는 근사다. 거래가격 고저가로 마크 고저가를 대체하지 않는다.
숏의 봉 고가가 마지막 계약 구간을 넘더라도 제공된 구간 안의 청산가격을 먼저
통과하면 해당 가격에서 청산한다. 봉중 청산가격 자체가 미제공 구간에 있어
계산할 수 없으면 오류로 중단한다. 고가에서 담보가 소진됐다는 사실만으로
임의의 봉중 체결가격을 만들지는 않는다.

시가 위험 판정에는 관측된 거래 시가가 있으므로, 보유 명목금액이 표 밖이어도
자기 포지션 자산이0 이하이면 청산한다. 자산이 양수인 숏도 제공된 구간의 상한
가격에서 이미 유지증거금을 위반했다면 현재 시가에 청산한다. 이는 유지증거금이
음수가 아니고 구간에 따라 감소하지 않는 지원 모형의 성질을 사용하며,
미제공 구간의 증거금률을 추정하지 않는다. 그 외 위험 판정에 모르는 규칙이
필요하면 중단한다. 축소 후 시가 위험 재검사에도 같은 규칙을 적용한다.
메타데이터의 `out_of_bracket_open_risk_policy=liquidate_if_proven_else_error`에 기록한다.

청산가격 계산에서 인접한 두 구간의 후보 명목금액이 공통 경계 양쪽으로 각각
4 ULP 이내 엇갈릴 때만 공통 경계가격을 사용한다. 유효한 구간 안의 계산값과
최종 제공 구간 상한은 그대로 유지하며, 상대오차 비교로 계약 범위를 넓히지 않는다.
현재 수집 캐시의 `mark_price`는 마크 시가이며 추가 고저가 파일은 만들지 않는다.
두 모형 모두 거래소의 부분 강제청산·ADL·호가 경로를 재현하지 않는다.

슬리피지 0은 역사적 체결 시가를 보존한다. 양의 슬리피지는 비례 적용 후 불리한
틱으로 정규화하며, 정수 틱 경계의 계산 오차는 2 ULP 이내에서만 정리한다.
틱 개수가 float64의 정확한 정수 범위를 넘거나 반올림한 체결가가 0 이하이면
명시적 오류로 중단한다. 현재 계약 틱과 역사적 가격이 양립하는지 확인해야 한다.

펀딩 수입을 먼저 반영하고 출금은 공통 현금→해당 격리담보 순으로 차감한다.
동시 주문은 모든 종목의 축소 후 잔여 포지션의 유지증거금을 재검사하고,
종목 이름 순서로 증가를 처리한다. 자금 부족 시
목표를 임의로 줄이지 않고 고정 수량을 재시도한다. 모든 목표 수량은 같은 계좌
스냅샷에서 산정한다. 봉중 반환 현금은 그 봉 시가 주문에 소급 사용하지 않는다.
청산 시 대기 요청을 취소하고 같은 봉 재진입을 막는다. 마지막 봉은 신규 주문 없이
거래 시가에서 정산한다. 자산곡선은 시가 주문 전 마크 평가이며 마지막 행만 정산 후다.

감소·종료 주문의 반환액은 감소 물량에 비례 배정된 격리담보에 실현손익을 더하고
종료 수수료·강제청산 수수료를 뺀 금액이다. 부분 종료에서 이 금액이 음수이면
그 부족액을 같은 포지션의 남은 담보에서 차감하고 현금은 반환하지 않는다.
부분 종료만으로 손실을 보전하지 않으며 공통 현금이나 다른 포지션 담보도 쓰지 않는다.
축소 후 현재 마크 기준 유지증거금을 위반하면 증가 주문 전에 잔여 포지션을 전량
청산하고 `phase=post_reduction`으로 기록한다. 봉중 미래 고저가는 사용하지 않는다.

기본 `insurance_cover`는 **전량 종료** 시 남는 음수 반환액을 0으로 정산한다.
원래 실현손익·비용은 그대로 두고 부족액만 `insurance_credit`으로 별도 기록한다.
일반 종료에 강제청산 수수료를 임의 부과하지 않는다. 명시적 `error` 진단에서는
float64 계산 오차 상한 `8 × eps × (배정 담보 절댓값 + 진입 명목금액 + 종료
명목금액 + 종료 수수료 + 강제청산 수수료)`를 넘는 부족액이면 중단한다.
오차 이내 음수 반환액도 별도 보전액으로 남겨 현금을 음수로 만들지 않는다.
메타데이터에 `insurance_cover_scope=bankrupt_full_closes_only`와 부분 종료 정책을 기록한다.

이 즉시 보전은 보험기금·ADL·조건부 음수 잔고 정리를 단순화한 연구 모형이다.
거래소가 모든 종료의 초과 손실을 즉시 보장한다는 뜻이 아니며, 특히 일반 전량
종료까지 같은 정산 규칙을 확장한 가정이다. [Binance 청산 절차](https://www.binance.com/en/support/faq/detail/360033525271),
[보험기금 설명](https://www.binance.com/en/support/faq/detail/360033525371)을 참고한다.

#### 교차증거금 (`margin_mode: "cross"`)

```json
"numba_futures": {"margin_mode": "cross", "leverage": 2, "liquidation_model": "mark_open", ...}
```

- 포지션별 담보가 없다. 진입은 지갑에서 수수료만 빼고, 축소·종료는 실현손익에서 종료
  수수료·강제청산 수수료를 뺀 금액을 지갑에 반영한다. 펀딩 수입·지급도 지갑에만 반영한다.
  지갑(`free_cash`)은 포지션이 남아 있는 동안 음수일 수 있고 `isolated_collateral`은 항상 0이다.
  계좌 자산 = 지갑 + 미실현손익이며 매 봉 기존 손익 누적 검산을 그대로 적용한다.
- 강제청산: 펀딩 직후 시가와 축소 주문 직후(`phase=post_reduction`)에
  `지갑 + Σ미실현손익(마크 시가) ≤ Σ유지증거금`이면 모든 교차 포지션을 그 봉 거래 시가로
  전량 청산하고 모든 대기 요청을 취소한다. 그 봉 시가에는 교차 종목의 신규 주문을 받지 않는다.
  유지증거금 구간을 넘는 명목은 마지막 구간의 유지증거금을 하한으로 써서 청산이 증명될 때만
  청산하고, 아니면 오류로 중단한다. 봉중 고저가 판정은 지원하지 않는다(종목별 마크 고저가로는
  여러 포지션의 동시 경로를 알 수 없음). Binance의 부분·단계 청산, IOC 체결, ADL은 재현하지 않는다.
- 진입·증가: 사용 가능 잔고 = `max(0, 지갑 + Σ미실현손익 − Σ(마크 명목 / 레버리지))`.
  미실현이익을 포함한다. 주문의 `체결 명목 / 레버리지 + 진입 수수료`가 이를 넘으면
  `insufficient_margin`으로 거부하고, 체결 후 계좌가 유지증거금 이하가 되면
  `entry_below_maintenance`로 거부한다. 구간·구간 최대 레버리지 검사는 격리와 같다.
  같은 봉의 증가는 기존처럼 종목 이름 순서로 처리하며 앞선 체결의 초기증거금을 다음 판정에 반영한다.
- 부족액: 모든 교차 포지션이 닫힌 뒤 지갑이 음수이면 `insurance_cover`는 지갑을 0으로 맞추고
  부족액을 `insurance_credit`으로 기록한다. 거래별 배분은 직전 점검 이후 전량 종료들의 순손실
  비율(손실이 없으면 명목 비율)이며 열 순서에 의존하지 않는다. `error`는 계산 오차
  `8 × eps × (종료 명목 합 + 부족액)`를 넘으면 중단한다. 보전 후 자산 0은 기존처럼 파산 오류다.
- 거래별 `stake_amount`·`profit_ratio`의 분모는 체결 시점 `|수량| × 진입가 / 레버리지`의 최대값이다.
- 메타데이터에 `margin_mode=cross`, 청산·사용 가능 잔고·보험 배분·펀딩 모형과 한계를 기록한다.
- 근거: [청산 프로토콜](https://www.binance.com/en/support/faq/binance-futures-liquidation-protocols-360033525271)
  (교차 포지션은 Margin Balance를 공유하고 Margin Balance ≤ 유지증거금이면 청산),
  [청산가 계산](https://www.binance.com/en/support/faq/how-to-calculate-liquidation-price-of-usd%E2%93%A2-m-futures-contracts-b3c689c1f50a44cabb3a84e663b81d93)
  (교차 WB=crossWalletBalance, 다른 계약의 유지증거금·미실현손익 포함),
  [잔고 정의](https://www.binance.com/en/blog/futures/what-is-the-available-balance-margin-balance-and-total-balance-on-binance-futures-457299340443288694)
  (Available = Wallet − Initial Margin + Unrealized PNL). 2026-11-08 조회한 현재 문서 기준이다.
- Freqtrade 대조는 단일 종목만 의미가 있다. Freqtrade 2026.9 백테스트는 교차 청산가 계산에서
  다른 교차 거래를 진입가로 평가한다(`binance.py` `dry_run_liquidation_price`).

거래·주문·계좌 결과는 기존 `BacktestResultV2` 저장 경로를 쓴다.
`futures_metadata.json`에 계약 원본·해시·모형 한계를 보존하며
`futures_events.parquet`(CSV 선택 시 `.csv`)에 상세 사건을 저장한다.
같은 디렉터리에 다시 저장하면 현재 결과에 없는 메타데이터·이벤트와 이전 형식의
계좌·이벤트 파일을 제거한다. `save_events=false`이면 이전 CSV·Parquet 이벤트
파일도 남기지 않는다. 정리는 이 저장기가 소유한 정확한 파일명에만 적용하며
사용자 파일이나 다른 디렉터리를 삭제하지 않는다.
그리드·기존 그리드 TPE·PBO·SPA·WF는 동일한 중앙 엔진 호출을 사용한다.
전략 조립 탐색기도 `backtest.engine=numba_futures`를 선택할 수 있다.
엔진·계약·코드가 바뀌면 기존 탐색 DB를 이어 쓰지 않고 새 출력 디렉터리를 사용한다.

Numba 그리드와 기존 그리드 TPE는 한 번의 실행 안에서 동일한 가격·마크·펀딩의
준비 결과를 자동 재사용한다. 별도 config 옵션은 없으며 가장 최근 한 세트만 보관한다.
평가 기간·종목·비용·계약·엔진 설정, 입력 가격 또는 원본 파일 정보가 바뀌면 다시
준비한다. 계약 JSON 파일은 내용 해시로 확인한다. 비중과 계좌·주문 상태는 후보별로
분리한다. 전략 조립 탐색기와 PBO·SPA·WF 호출에는 이 순차 캐시를 연결하지 않는다.

독립 후보 병렬 평가용 Python API는 `iter_numba_futures_backtests_v2`다.
공통 가격·펀딩을 한 번 검증해 읽기 전용으로 공유하고 후보별 계좌는 분리한다.
후보 비중은 생성기에서 받을 때와 작업자에게 제출하기 전에 복사해 고정한다.
같은 DataFrame을 덮어쓰며 생성하는 입력도 후보별로 독립 평가한다.
`workers=1`(1~32), `memory_budget_mb=1024`이며 예상 작업 공간이 예산보다 크면 거부한다.
추정 이벤트 수는 `max_records`와 봉수·종목수로 계산한 보수적 상한 중 작은 값을
사용하며 초기 기록 배열 공간도 포함한다. 작은 작업에서 발생할 수 없는 백만 개
이벤트 공간을 무조건 예약하지 않는다. `max_records=1000000` 기본 상한 자체는 유지한다.
큰 작업은 여전히 `workers`, `max_records`, 예산을 명시적으로 조정해야 할 수 있다.
예산은 보수적 추정이고 프로세스 RAM의 강제 상한은 아니다.
결과는 입력 순서대로 하나씩 반환하므로 소비자는 무제한 누적하지 않아야 한다.
기존 그리드·TPE CLI의 후보 실행은 순차이며 이 API 때문에 자동 병렬화되지는 않는다.
자세한 회계 순서·검증 결과는 `notes/numba_futures_v2.md`를 따른다.

#### 바이낸스 선물 데이터 수집

```powershell
py scripts/binance_futures_collector_v2.py --start 2019-12-24

# BTC 무기한 선물 5분봉
py scripts/binance_futures_collector_v2.py --interval 5m --start 2020-12-18 --mark-gap-policy 1m-open
```

기본 종목은 `BTCUSDT`, 저장 위치는 `data/futures/binance/BTCUSDT/1h`다.

USDT-M 봉의 `candle_acc_trade_volume`은 기초자산 수량이고,
`candle_acc_trade_price`는 바이낸스 원본 `klines[7]`의 **USDT 거래대금**이다.
REST·월별/일별 ZIP·REST 봉 복구에서 동일하게 보존한다. 마크가격 봉의 거래량은
거래대금으로 사용하지 않으며, 종가×수량으로 실제 거래대금을 대체하지 않는다.
[바이낸스 공식 아카이브 필드 정의](https://github.com/binance/binance-public-data#futures)를 따른다.

기존 아카이브 관측 체크포인트에 이 필드가 없으면 같은 입력으로 `--normalize-only
--cache-incomplete`를 재실행할 때 로컬 거래가격 ZIP과 이미 기록된 일별/REST 복구 원본에서
거래대금만 추가한다. 기존 가격·마크·펀딩 파일을 재생성하지 않는다. 원본 체크섬과
저장된 OHLCV가 일치하는 관측만 연결하며, 원본을 다시 다운로드하거나 결측값을 추정하지 않는다.
`report.json.quote_turnover_backfilled_symbols`와 종목별 `manifest.json.detail.quote_turnover`에
보완 종목·원본 해시·관측 수를 남긴다. 이후 같은 입력은 체크포인트를 재사용한다.
공용 `cache_observed/wide`는 기존 게시 절차로 재조립한다.

거래대금이 없거나 숫자가 아니거나 음수/무한대이면 엄격 수집은 게시하지 않는다.
관측 캐시에서는 해당 거래대금만 `NaN`으로 두고 `missing_or_invalid_quote_volume`을 기록한다.
이는 가격·펀딩만 요구하는 기존 백테스트를 차단하지 않는다. 거래대금을 사용하는 조건은
결측을 제외해야 하며, 0은 실제 관측값으로 보존한다.
월별/복구 원본의 OHLCV는 같고 거래대금만 충돌하는 경우에도 가격 결측 복구는 진행한다.
충돌 거래대금만 결측으로 기록하고 복구 상세의 `quote_volume_conflict_bars`와 시각 목록을 남긴다.

예를 들어 1시간봉의 최근 20일 실제 거래대금 합은 기존 피처 설정으로 작성한다.
이 예시는 설정 문법이며, 기존 연구 config의 기간·선별 기준은 자동으로 변경하지 않는다.

```json
{
  "source": "candle_acc_trade_price",
  "column_name": "turnover_20d",
  "steps": [
    {"kind": "rolling_sum", "params": {"window": 480, "missing_policy": "preserve"}}
  ]
}
```

`rolling_sum.missing_policy`의 기본값 `skip`은 기존처럼 유효 관측 개수를 센다.
`preserve`는 정렬된 고유 타임스탬프의 연속 `window`행을 사용하고, 창 안의 결측이 있으면
결과도 결측으로 둔다. 따라서 일정한 1시간봉 캐시에서 480행은 20일이며,
중간 결측 때문에 합산 구간이 과거로 늘어나지 않는다. 기간은 봉 수 단위이고 5분봉의
20일은 5,760행이다. 피처에 포함된 현재 봉의 거래대금은 그 봉이 끝나야 알 수 있으므로
선별의 `filter_lag`와 이후 체결 시점은 기존의 미래 정보 누출 방지 규칙을 유지한다.

다종목·거래기간 수집은 같은 CLI의 `--symbols`로 선택한다. 아래 종목 목록은
명령 예시이며, 전략의 숏 후보나 과거 전체 유니버스를 고정하는 설정은 아니다.

```powershell
py scripts/binance_futures_collector_v2.py --symbols BTCUSDT,SOLUSDT,LINKUSDT --interval 1h --start 2024-01-01 --end 2024-02-01 --with-metrics --out-dir data/futures/binance/research_universe/1h
```

| 다종목 옵션 | 의미 |
|---|---|
| `--symbols` | 중복 없는 USDT 계약 목록. `--symbol`과 동시에 쓸 수 없다. 현재 거래 중인 계약을 자동으로 과거 유니버스로 삼지 않는다. |
| `--out-dir` | 다종목 모드에서는 필수. 단일 종목 캐시나 다른 종목/주기/metrics 모드의 결과는 덮어쓰지 않는다. |
| `--with-metrics` | 공식 일별 ZIP에서 OI·포지셔닝 자료를 수집한다. 생략 시 기존 가격·마크·펀딩과 거래기간 정보만 저장한다. |
| `--instruments-json` | `exchangeInfo` 형식의 `symbols` 배열에 `source`, `as_of`를 추가한 출처 있는 계약 스냅샷. 생략 시 공개 `exchangeInfo`를 조회한다. |
| `--funding-schedules-json` | 종목별 펀딩 설정 JSON. 각 키는 요청한 대문자 심볼이고 값에는 `funding_interval_hours`, `funding_schedule`을 넣는다. 공통 `--funding-schedule-json`과 동시 사용은 거부한다. |

기존 가격·마크·펀딩 수집·검증 함수를 종목마다 재사용한다. 요청한 공통 시간축에서
각 종목을 별도 열로 저장하고, 거래기간 밖의 가격·거래량·지표는 NaN으로 둔다.
현재 구현의 가격·마크·펀딩 소스는 REST이고, metrics만 공식 아카이브를 사용한다.
REST나 계약 스냅샷에서 사라진 과거 계약을 자동으로 복원하지는 않는다.

`instruments.json`에는 출처·조회 시점, 종목별 상장 시각·알려진 종료 시각·관측 당시
상태·적용한 펀딩 일정이 기록된다. `tradable.parquet`는 이 경계 안의 완전한 봉에
대한 불리언이다. 상장 시각은 봉 경계로 올림, 종료 시각은 내림하며 부분 봉은 제외한다.
다종목 REST·아카이브 수집은 `exchangeInfo.underlyingSubType`도 종목별
`underlying_subtypes` 목록으로 보존한다. 중복 태그는 제거하고 원래 대소문자는 유지한다.
`classification_source`, `classification_as_of`, `classification_basis=snapshot_not_historical`에
출처·조회 시점과 현재 스냅샷 분류임을 기록한다. `classification_status`는 `tagged`,
`empty_tags`(명시적 빈 목록), `field_missing`, `symbol_not_in_snapshot`, `invalid_tags`를
구분하며, 미확인·잘못된 태그를 임의 섹터로 채우거나 계약 생애 오류로 바꾸지 않는다.
하나의 코인에 여러 태그가 있을 수 있고, 현재 태그를 과거 당시 알려진 분류로 간주하면 안 된다.
분류 필드는 거래 가능 여부·피처·주문에 자동 적용되지 않는다. 관측 체크포인트 재사용 시에는
태그·출처 정보만 갱신하며, 분류 변경만으로 가격 체크포인트의 입력 서명을 바꾸지 않는다.
`SETTLING` 종목도 요청 구간에서 거래 중이었다면 포함한다. 현재 상태가 종료인데
종료 시각이 불명확하거나 계약 정보가 없으면, 파일 유무·거래량·마지막 가격으로
추측하지 않고 오류로 중단한다. 가격을0으로 만들거나 마지막 값을 이후로 채우지 않는다.
정상 거래기간 안의 거래량0 봉 자체를 상폐로 간주하지도 않는다.

이 정보는 관측한 계약 스냅샷 기준이며 완전한 재상장·일시 거래중단 이력이 아니다.
종료 시각과 현재 상태를 과거에 미리 알았던 매매 신호로 사용하지 않는다.
정산가격은 확보한 것으로 가정하지 않고 `settlement_price=null`,
`backtest_settlement_implemented=false`로 명시한다. 백테스트가 이 JSON을 자동으로
읽어 상폐 정산하거나 BTC 헤지를 조정하는 기능은 아직 없다. 현재 엔진은 보유 중
가격이 사라지면 오류로 중단하므로, 이 캐시 생성만으로 상폐 정산 재현이 완성되지는 않는다.

metrics 파일의 출력 이름은 `open_interest`, `open_interest_value`,
`long_short_account_ratio`, `top_long_short_account_ratio`,
`top_long_short_position_ratio`, `taker_long_short_volume_ratio`이며 모두 wide Parquet다.
`custom:open_interest` 등 기존 피처 입력으로 읽을 수 있다. 계정 수 비율과 상위
트레이더 포지션 비율은 서로 다른 지표이며 OI 자체가 롱 방향의 수량을 뜻하지 않는다.
5분 원본 스냅샷은 한 원본 주기(5분) 뒤부터만 사용하고, 다음 스냅샷이 누락되면
기존 값을5분 이상 연장하지 않는다. 원래 값이 NaN이면 그대로 유지한다.
동일한 중복 행은 제거·집계하고 같은 시각의 값이 충돌하면 저장 전에 중단한다.
404인 일별 파일은 결측으로 기록하고, 그 밖의 HTTP 오류는 중단한다.
`manifest.json.symbols_detail.<심볼>.metrics`에 누락 날짜, 중복 제거 수, 필드별
유효 봉 수를 기록하므로 파일이 생성됐다는 이유로 지표 전체 기간이 확보됐다고 보지 않는다.

종목별 펀딩 설정 파일의 예시는 다음과 같다. 생략한 종목은 공통 고정 간격을 사용하며
정산 공백에서 간격을 자동 추측하지 않는다. 다른 주기의 정산이 관측되면 명시한 일정과
다르므로 저장하지 않는다.

```json
{
  "BTCUSDT": {"funding_interval_hours": 8},
  "SOLUSDT": {
    "funding_interval_hours": 8,
    "funding_schedule": [
      {"start": "2020-09-15T00:00:00Z", "interval_hours": 8},
      {"start": "2022-11-09T20:00:00Z", "interval_hours": 4}
    ]
  }
}
```

위 예시는 형식 설명이며 실제 요청 기간 전체의 검증된 일정으로 교체해야 한다.
종목별 일정은 `instruments.json.records.<시장>`에 보관한다. Numba 백테스트의
`numba_futures.contracts.<시장>.funding_schedule` 등에 전달하는 연결은 별도다.
원본 REST 응답과 metrics ZIP은 `raw/<심볼>/`에, 계약 응답은 `raw/exchangeInfo.json`에
저장하고 SHA256을 manifest에 기록한다. 요청 목록은 명시한 종목에 한정되므로
자동으로 생존편향이 제거됐다는 의미가 아니다.

모든 종목의 검증·스테이징을 마친 뒤 파일별로 교체하고 manifest를 마지막에 게시한다.
다운로드·검증 실패 시 기존 결과는 유지되지만, 게시 중 전체 폴더가 하나의 트랜잭션은
아니므로 수집과 백테스트를 동시에 실행하지 않는다. 증분 수집·중단 후 재개 기능은 없고,
다시 지정한 구간으로 변환 캐시를 교체한다. 이전 구간의 원본 ZIP이 남을 수 있다.
기존 `--symbol` 단일 모드는 거래기간 조회와 metrics를 추가하지 않고 기존 명령을 유지한다.

공식 아카이브 일괄 수집은 같은 CLI의 `--source archive`로 실행한다. 기존 REST 모드의
기본값은 바뀌지 않는다. 목록·ZIP·체크섬 처리는 공용 `lib/binance_archive_v2.py`를 사용하며
전략별 실행기를 추가하지 않는다.

```powershell
# USDT 심볼 전체, 기본 1시간봉. 먼저 목록과 원본 ZIP 용량만 확인한다.
py scripts/binance_futures_collector_v2.py --source archive --all-symbols --start 2020-01-01 --with-metrics --out-dir data/futures/binance/archive_universe/1h --plan-only

# 같은 옵션에서 --plan-only를 빼면 다운로드와 검증·캐시 정리를 실행한다.
py scripts/binance_futures_collector_v2.py --source archive --all-symbols --start 2020-01-01 --with-metrics --out-dir data/futures/binance/archive_universe/1h
```

| 아카이브 옵션 | 기본값·동작 |
|---|---|
| `--source archive` | 공식 ZIP 일괄 모드. `--out-dir`는 캐시 파일을 직접 담는 폴더가 아니라 수집 전체를 관리하는 폴더다. |
| `--cache-end` | 아카이브 캐시만 조기에 끝낼 배타적 UTC 종료일. 생략 시 다운로드 종료일과 같다. 봉 경계에 맞고 `start < cache-end <= end`여야 한다. 인벤토리·다운로드 기간·원본 ZIP·고정 snapshot은 유지하며 정규화·검증·공통 캐시 시간축만 제한한다. REST에서는 사용할 수 없다. |
| `--exclude-incomplete-prefix` | 기본 비활성. `--source archive --cache-incomplete`에서 종목별 가격·마크의 앞부분 공백과 출처가 있는 연구 시작일 이전을 제외한 연구용 공통 캐시를 게시한다. 원본·종목별 관측 캐시는 보존하고, 시작 후 누락은 계속 검증한다. 정책을 유지하려면 재게시 때도 명시한다. |
| `--normalize-only` | 저장한 인벤토리를 자동 재사용하여 캐시만 재생성한다. 기본은 오프라인이며 복구 옵션을 명시한 경우 해당 보완 요청만 허용한다. 종목 생애·캐시 기간에 필요한 ZIP만 크기와 로컬 공식 `.CHECKSUM`의 SHA256으로 검사한 뒤 정규화한다. 원본·인벤토리·snapshot·`downloads.json`을 보존한다. 계획·다운로드 전용·원격 갱신·metrics 전용과 병용할 수 없다. |
| `--repair-candle-gaps` | 아카이브 정규화에서 공식 일별 ZIP을 먼저 사용하고, 남은 거래가격·마크가격 공백은 동일 봉 주기의 공식 REST 실측 OHLCV로 보충한다. `--normalize-only`와 함께 명시하면 이 보충에 한해서 네트워크를 허용한다. 가격 보간·펀딩 대체·자동 기간 축소는 하지 않는다. 생략하면 보관한 보충 ZIP·REST 응답만 오프라인 검증 후 재사용한다. |
| `--repair-funding-gaps` | 정산 달력에서 빠진 이벤트가 있는 달의 공식 REST 펀딩을 보완한다. 원본 응답과 SHA256을 저장해 중단·오프라인 재변환 때 재사용한다. 기존 관측과의 충돌은 덮어쓰지 않으며 API에도 없는 공백은 남긴다. `--normalize-only`와 병용 가능. 기본 비활성. |
| `--cache-incomplete` | 읽을 수 있는 가격·마크가격·펀딩·선택한 metrics를 `cache_observed`에 종목별로 저장하고 결측·오류를 보고한다. 일부 필드·종목·다운로드가 실패해도 나머지 관측을 저장한다. 재실행은 같은 입력의 종목별 체크포인트를 재사용한다. 기본 엄격 모드 및 기존 `cache_verified`는 유지한다. |
| `--all-symbols` | 월별·일별 `klines` 폴더 목록을 모두 페이지 순회해 USDT 심볼을 합친다. 상폐 심볼·일별 자료만 있는 신규 심볼도 탐색하며 Unicode 심볼을 허용한다. `--symbol`, `--symbols`와 상호 배타적이다. |
| `--asset-filter` | 아카이브 전용, 기본 `all`로 기존 동작 유지. `crypto`는 snapshot의 `quoteAsset=USDT`, `contractType=PERPETUAL`, `underlyingType=COIN`과 명확하게 불일치하는 상품을 다운로드·캐시 변환 전에 제외한다. snapshot에 없는 과거 심볼이나 분류 필드가 빠진 심볼은 미분류로 유지한다. `TRADING` 여부로 거르지 않는다. 비어 있는 snapshot으로는 필터를 실행하지 않으며 필요하면 출처가 있는 `--instruments-json`을 지정한다. |
| `--data-group` | 아카이브 전용. `core`는 가격·마크가격·펀딩만 다운로드하고 metrics 없는 캐시를 생성한다. `metrics`는 metrics ZIP만 받고 캐시는 생성·변경하지 않는다. `all`은 둘 다 검증하고 metrics 포함 캐시를 생성한다. 명시하면 `--with-metrics`보다 우선한다. 생략하면 기존대로 `--with-metrics` 지정 시 전체, 미지정 시 core를 처리한다. |
| `--symbol`, `--symbols` | 전체 대신 특정 종목만 수집할 때 기존 선택 옵션을 재사용한다. |
| `--interval` | 기본 `1h`. 원하는 주기 파일을 직접 받으며 1분봉 전체를 먼저 받을 필요가 없다. 다른 주기는 다른 수집 폴더를 사용한다. |
| `--end` | 생략하면 BTC의 가장 최근 공개 월별 펀딩 파일 다음 달 1일을 종료 경계로 사용한다. 실시간 자료까지 받은 것으로 가정하지 않는다. |
| `--plan-only` | 원본 ZIP을 받지 않고 `inventory.json`, `report.json`에 목록·ZIP 총 바이트를 기록한다. 기존 파일을 포함한 전체 원본 크기이며 풀린 CSV·캐시·임시 공간은 별도다. |
| `--download-only` | 체크섬을 통과한 원본 ZIP까지만 저장하며 백테스트 캐시는 게시하지 않는다. `--plan-only`와 동시에 사용할 수 없다. |
| `--workers` | 목록 조회·다운로드 및 공식 일별 봉 복구의 동시 작업 수. 기본 4, 범위 1~32. 종목별 데이터 병합·검증·Parquet 저장은 순차 처리한다. |
| `--pause-seconds` | REST 기본 0.15초, 아카이브 기본 0초. 명시한 대기는 유지한다. 아카이브에서도 HTTP 429/5xx 재시도·백오프는 계속 적용한다. |
| `--refresh-archives` | 생략 시 보관한 CHECKSUM·파일 크기·SHA256이 일치하면 네트워크 다운로드를 생략한다. 지정하면 원격 CHECKSUM을 다시 조회해 수정된 아카이브도 확인한다. |
| `--reuse-inventory` | 저장한 종목·봉 주기·기간의 목록을 재사용하고 원격 목록 조회를 생략한다. `--data-group` 생략 시 기존 metrics 모드 일치를 요구한다. 명시하면 저장된 목록에서 해당 그룹만 처리할 수 있다. `metrics`·`all`은 metrics 포함 목록이 필요하다. `--end` 생략 시 저장한 종료일을 고정하므로 새 기간·새 심볼을 자동 추가하지 않는다. |
| `--max-cache-memory-mb` | 공통 wide 캐시 조립의 추정 메모리 상한. 기본 2048 MiB. 시간축 길이 × 검증 가능 종목 수 × 8바이트 × 4로 보수적으로 추정하며 초과 시 원본을 보존하고 캐시를 게시하지 않는다. 실제 최대 메모리를 보장하는 측정값은 아니다. |

원본은 `<out-dir>/archives/data/futures/um/...`에 공식 경로를 유지해 ZIP과 `.CHECKSUM`으로
보존한다. 월별 가격·마크가격 파일이 있으면 같은 달의 일별 파일과 중복 수집하지 않고,
월별 파일이 없는 달은 일별 파일을 사용한다. 펀딩은 월별, metrics는 일별 파일을 받는다.
공식 목록에서 확인된 객체만 요청하며 다운로드 중인 임시 파일을 완료 파일로 취급하지 않는다.
같은 명령을 반복하면 검증된 원본을 재사용하고 변환을 다시 수행한다. 프로세스 강제 종료로
남은 `.download-*.part` 파일은 완료 ZIP으로 인식하지 않는다.
목록 조사까지 반복하지 않고 같은 범위를 재개하려면 원래 명령 뒤에 `--reuse-inventory`를
붙인다. 최초 인벤토리가 없거나 이전 목록 조사가 실패했다면 이 옵션은 사용할 수 없다.
구 버전 인벤토리에 `with_metrics`가 없으면 실제 metrics 객체 포함 여부로 해당 모드를 확인한다.
신규 `inventory.json`(schema v2)은 파일 목록과 `instrument_snapshot`, 정규화 JSON의
`instrument_snapshot_sha256`을 한 번의 원자적 교체로 함께 저장한다. 재개 시 snapshot 해시를
검사하고 목록 안의 계약 정보를 사용한다. 별도 `instruments_snapshot.json`은 목록 저장 후 쓰는
조회용 사본이므로, 이 사본 저장 전에 중단되어도 이전 계약 정보를 새 목록에 섞어 사용하지 않는다.
구 버전 목록은 기존처럼 별도 snapshot 파일을 사용하며, 이미 과거에 혼합된 상태인지는 자동으로
판별할 수 없다. 목록 조회가 실패하면 `report.json`에 실패 종목을 기록하고 이전 목록·계약
정보는 덮어쓰지 않는다. 이전에 성공한 목록이 있으면 이를 재개할 수 있고, 최초 목록 조사부터
실패했다면 `--reuse-inventory` 없이 다시 조사해야 한다.
`--reuse-inventory`는 저장한 계약 정보를 재사용해, 나중에 거래소 목록에서 사라진 종목 때문에
분리 수집 전후의 과거 계약 구간이 달라지지 않게 한다. 저장한 snapshot이
없거나 유효하지 않으면 현재 API 정보로 조용히 교체하지 않는다. 캐시 생성 또는 `crypto`
분류가 필요한 실행은 중단한다. 자산 필터가 `all`인 원본 전용 실행(`--download-only`,
`--data-group metrics`)과 목록 확인(`--plan-only`)은 계약 정보 없이도 재개할 수 있으며,
보고서의 `instrument_snapshot_error`에 사유를 기록한다. 출처가 있는
`--instruments-json`으로 이번 실행의 계약 정보를 명시적으로 보완할 수 있다. 이 override는
저장한 snapshot을 덮어쓰지 않으므로 수동 보완을 계속 사용할 때는 같은 옵션을 유지한다.
새 범위에서 기존 캐시의 종목이 빠지는 경우 목록·계약 정보 저장 및 다운로드 전에
`failed_universe_change`로 거부한다. `core`·`metrics`·`all`, `--plan-only`, `--download-only`에도
같은 사전 검사를 적용해, 거절된 실행이 재개 범위를 좁혀 덮어쓰지 않게 한다. 계약 정보를
확인할 수 있는 경우 캐시 생성 가능 종목을 비교하고, 계약 정보 없는 원본 전용 실행에서는
선택한 종목 범위를 비교한다. 보고서에 `failure_phase=preflight`와 기존 목록·계약 정보·캐시
보존 여부를 기록하며, 기존 명령으로 재개할 수 있다. 캐시 게시 직전에도 실제 변환 결과의
종목 누락을 다시 확인한다. 의도적으로 더 좁은 종목 집합을 만들 때는 별도 수집 폴더를 사용한다.
가격 자료를 먼저 받고 metrics를 미루려면 `--data-group core`를 사용한다. 명시적인 그룹으로
신규 목록을 만들 때는 metrics 파일 목록까지 조사해 `inventory.json`에 보존하지만, 선택하지 않은
그룹의 ZIP은 다운로드하지 않는다. 나중에 같은 명령의 그룹을 `all`로 바꾸고
`--reuse-inventory`를 붙이면 기존 가격 ZIP을 검증·재사용하고 빠진 metrics만 추가 다운로드한 뒤
전체 캐시를 재생성한다. metrics 원본만 따로 받고 싶으면 `--data-group metrics`를 사용한다.
metrics 단독 실행은 정상 완료 시 `downloaded`이며, 기존 캐시·manifest를 그대로 유지한다.
그 후 캐시에 통합할 때 `--data-group all --reuse-inventory`를 실행한다.
`--download-only`는 어떤 그룹에서도 캐시 게시를 생략한다.
예전 명령으로 만든 목록에 metrics 자체가 없다면 metrics 포함 그룹으로 재사용할 수 없다.
같은 시작·종료일로 `--reuse-inventory` 없이 `--data-group all`을 실행해 목록을 다시 조사해야 하며,
이 경우에도 검증된 기존 원본 ZIP은 재사용한다. 종료일은 이전 `inventory.json`의
`end_exclusive`를 `--end`로 지정해 기간이 의도치 않게 늘어나지 않도록 한다.
원본 수집 범위를 유지하면서 최근 누락 구간 이전까지만 캐시를 만들려면
`--reuse-inventory --cache-end 2026-10-28`처럼 지정한다. 이 예시는 UTC 2026-10-27까지 포함한다.
`report.json`과 게시된 캐시의 `manifest.json`에는 `download_start_inclusive`,
`download_end_exclusive`와 `cache_start_inclusive`, `cache_end_exclusive`를 별도로 기록한다.
캐시 종료 이전의 가격·마크가격·펀딩 누락은 여전히 검증 실패이며 자동 보간하거나 제외하지 않는다.
기존 캐시의 종목이 사라지는 종료일은 기존 유니버스 축소 방지 규칙에 따라 거부한다.
`--plan-only`, `--download-only`, `--data-group metrics`는 기존처럼 캐시를 게시하지 않는다.
이미 다운로드를 마쳤다면 다음처럼 다운로드 단계를 생략할 수 있다. `--reuse-inventory`는 자동 적용된다.

```powershell
py scripts/binance_futures_collector_v2.py --source archive --all-symbols --asset-filter crypto --data-group core --interval 1h --start 2020-01-01 --out-dir data/futures/binance/archive_universe/1h --normalize-only --cache-end 2026-10-28
```

저장한 인벤토리와 동일한 원본 종목·시작일·종료일을 사용해야 하며 종료일을 생략하면 저장된 값을 사용한다.
원본 ZIP 및 공식 `.CHECKSUM`이 모두 있어야 한다. 필요한 파일이 없거나 손상되면 해당 종목의
`conversion_failures`에 기록하고 새 캐시를 게시하지 않는다. 복구하려면 `--normalize-only`를 빼고
`--reuse-inventory`를 붙여 일반 다운로드·검증 경로를 실행한다.
metrics를 포함하면 첫 봉에 필요한 직전 5분 관측의 전날 파일도 검사한다.
캐시 밖의 기간·비활성 종목·제외된 데이터 그룹은 검사하지 않는다. 목록 JSON 읽기,
필요한 ZIP의 해시 검사·CSV 변환·가격 및 펀딩 누락 검증·Parquet 저장 시간은 여전히 든다.
보고서의 `download_skipped=true`, `verified_files`로 이 모드와 검사한 ZIP 수를 확인한다.
게시 manifest의 `archive_sha256`에는 이 실행에서 실제 검증·사용한 ZIP만 기록한다.

#### 미완성 관측 캐시 저장 및 나중에 복구

전체 종목 검증을 기다리지 않고 읽을 수 있는 데이터를 먼저 저장하려면:

```powershell
py scripts/binance_futures_collector_v2.py --source archive --all-symbols --asset-filter crypto --data-group core --start 2020-01-01 --out-dir data/futures/binance/archive_universe/1h --normalize-only --cache-end 2026-10-28 --cache-incomplete
```

나중에 같은 명령에 `--repair-candle-gaps`를 추가하면 체크포인트의 가격·마크 공백에 대해서만
공식 일별 ZIP을 조회한다. 이미 복구한 봉은 남고 복구하지 못한 봉도 결측으로 저장된다.
기존 보충 ZIP에도 남아 있는 공백을 다시 시도할 때는 원격 목록·CHECKSUM을 갱신하므로
나중에 공식 일별 파일이 수정된 경우에도 잔여 공백을 복구할 수 있다.
펀딩 누락·잘못된 정산 달력·미확인 계약 생애를 가격 보충으로 해결한 것으로 처리하지 않는다.
펀딩 원본이나 계약정보를 수정한 뒤 재실행하면 입력이 바뀐 종목만 원본에서 다시 변환한다.

출력은 다음과 같다.

- `cache_observed/symbols/<심볼>/`: 원자적으로 게시하는 종목별 Parquet·체크포인트 manifest.
  읽은 필드와 오류가 종목마다 남으므로 중간 중단 후에도 완료된 종목을 다시 변환하지 않는다.
  이 폴더는 재개용이며 백테스트 입력으로 직접 지정하지 않는다.
- `cache_observed/wide/`: 기존 v2 형식의 필드별 wide Parquet·`instruments.json`·`manifest.json`.
  `source_cache_dir`에는 이 폴더를 지정한다. 종목별 자료를 모아 만든 별도 사본이므로 디스크 공간은
  체크포인트와 wide 출력에 모두 필요하다. 한 번에 한 필드만 조립한다.
- `wide/manifest.json.quality_intervals`: 시장별 필드·원인·시작(포함)·종료(제외)·누락 수.
  `symbols_detail`에는 ZIP 읽기 오류·검증한 원본 해시·일별 복구 출처도 저장한다.
- `report.json`: `cached_complete` 또는 `cached_incomplete`, 저장 종목·재사용 종목·재변환 종목과
  미해결 품질 구간이 있는 종목을 기록한다. `cached_incomplete`는 캐시 저장 성공 상태이며 종료코드는 0이다.

미완성 관측 모드의 `quality_summary`는 report와 wide manifest에 함께 저장한다.
`stored_symbols`, `complete_symbols`, `data_issue_symbols`, `lifecycle_unverified_symbols`는
저장 전체·경고 없음·생애 확인 후 자료 경고 있음·계약 생애 미확인 종목 수다. 뒤의 세 집단은
서로 겹치지 않는다. `reasons`는 원인별 종목 수·구간 수·관측 수를 집계하며 원인별 종목 수는
서로 겹칠 수 있다. 이 모드의 `conversion_failures`에는 생애가 확인된 자료 경고 종목만 넣고,
계약정보가 확인되지 않은 경우에는 기존 `unresolved_instruments`와 별도로 집계한다.

생애 미확인 종목은 어떤 날짜에 관측이 있어야 하는지 확정할 수 없으므로 요청 전 기간을
`missing_candles`·`missing_funding`·`missing_metrics` 또는 `unexpected_funding`으로 세지 않는다.
관측값·읽기 오류·잘못된 숫자 등의 검증은 보존하고 `tradable=false`와 전 기간의
`unresolved_lifecycle` 표시를 유지한다. 기본값은 계속 차단이며, 위 넘바 전용 연구 옵션으로만
관측 구간 사용을 허용한다. 경고 감소가 공식 거래 가능 판정으로 바뀌는 것은 아니다.
구 버전 체크포인트는 원본 ZIP을 다시 읽지 않고 manifest만 원자적으로 갱신할 수 있으며,
이 경우 기존 Parquet 바이트와 복구값을 그대로 보존한다.

정상 데이터가 없으면 해당 필드는 NaN으로 저장하고 원인을 남긴다. 날짜를 삭제하거나 펀딩을
0으로 채우지 않는다. 계약정보 미확인 종목도 관측값을 저장하지만 `tradable=false` 및
`unresolved_lifecycle`로 표시하고 기본 백테스트 사용을 막는다. 계약 경계상 요청 기간에 비활성인
종목은 기존처럼 제외한다. 중복 타임스탬프는 임의로 하나를 선택하지 않고 해당 봉을 비운 뒤
읽기 오류를 기록한다. 잘못된 숫자·OHLCV 관계도 품질 오류로 남는다.

재개 시 원본 목록의 key·크기, ZIP/CHECKSUM의 로컬 크기·수정시각, 계약정보·펀딩 설정·기간을
지문으로 비교하고 체크포인트 Parquet의 SHA256을 검사한다. 같으면 원본 ZIP의 전체 해시 검사와
CSV 재파싱을 생략한다. 이 경로는 원본 내용이 크기·수정시각을 유지한 채 외부에서 변조되지 않았다는
전제로 재사용하며, 원본은 최초 변환 때 공식 체크섬으로 검증한다. wide 파일은 종목 체크포인트에서
다시 조립하므로 이 I/O 시간은 남는다. 캐시 기간·입력 설정이 바뀌거나 체크포인트가 손상되면 재변환한다.
`--max-cache-memory-mb`를 넘으면 종목별 저장은 보존하고 `cached_symbols_only`로 종료한다.
이때 기존 wide 캐시는 유지되므로 최신 결과로 혼동하지 말고 보고서 상태를 확인해야 한다.

공통 `run_backtest_v2` 실행 경로는 선택한 종목·실행 기간에 필요한 품질 구간만 검사한다.
Numba/Freqtrade 선물은 가격·마크·펀딩, VectorBT는 가격을 검사하며 다른 종목이나 실행 기간
밖의 공백 때문에 전체 실행을 막지 않는다. 재개용 종목 체크포인트를 직접 지정하면 거부한다.
단일 실행·그리드·WF의 벤치마크는 공통 `resolve_benchmark_curve_v2`를 거쳐 실제 벤치마크
캐시·종목·실행 기간·가격 필드의 품질도 검사한다. 가격만 쓰는 벤치마크는 마크·펀딩 오류 때문에
막지 않으며 별도 `benchmark_source_cache_dir` 및 `benchmark_price_column`을 반영한다.
미완성 관측 캐시의 벤치마크 가격에 내부 결측·잘못된 값이 있으면 비교 지표를 계산하지 않는다.
계약 경계에 따른 상장 전·종료 후 구간의 기존 공통기간 정렬 동작은 유지한다.
이 검사는 실행 필드에 대한 것으로, 피처 워밍업 기간이나 추가 metrics의 품질은 보고서를 별도로
확인해야 한다. 엔진 내부 함수를 직접 호출하는 경로는 이 공통 사전 검사를 거치지 않는다.

기존 `cache_verified`는 이 옵션으로 변경하지 않는다. 옵션을 생략한 엄격 실행은 기존처럼 모든
정규화 대상이 검증을 통과해야 `cache_verified`를 게시한다.

월별 ZIP이 존재하더라도 내부 날짜가 누락될 수 있다. 공식 일별 자료와 같은 주기의 REST로 누락 봉을 보충하려면:

```powershell
py scripts/binance_futures_collector_v2.py --source archive --all-symbols --asset-filter crypto --data-group core --interval 1h --start 2020-01-01 --out-dir data/futures/binance/archive_universe/1h --normalize-only --cache-end 2026-10-28 --repair-candle-gaps
```

보충 ZIP은 같은 `archives/data/futures/um/daily/...`에 보관하고, 크기와 공식 CHECKSUM으로
검증한다. 원래 월별 ZIP 및 고정 `inventory.json`·snapshot·`downloads.json`은 바꾸지 않으며,
별도 `candle_repairs.json`에 일별 보충 파일의 크기·해시를 원자적으로 기록한다.
보충 기록은 캐시 게시가 실패하거나 이후 작업이 중단돼도 완료된 다운로드를 재사용하게 한다.
이 기록은 수집 메타데이터이며 새 전략·실행 스크립트가 아니다. 일별 파일과 월별 파일의
중복 봉 값이 다르면 오류이고, 일별 파일에서는 원래 없던 타임스탬프만 추가한다.
일별 파일 보충 후에도 공백이 있으면 같은 봉 주기의 공식 REST 관측을 아래 방식으로 보완한다.
그 후에도 누락·중복·잘못된 값이 있으면 기존처럼 해당 종목이 실패하며
새 공통 캐시를 게시하지 않는다. `symbols_detail.<심볼>.candle_repairs`에는 복구한 봉의
시각·수·원본 key·SHA256이 남고, `archive_sha256`에도 사용한 일별 원본을 포함한다.
이 옵션은 정규화 때만 동작하므로 `--plan-only`·`--download-only`·metrics 전용 단계에서는
일별 보충을 수행하지 않는다. 첫 실행은 원격 요청과 다운로드로 시간이 추가될 수 있다.
`--cache-incomplete`에서는 날짜별로 복구를 격리한다. 어떤 날짜의 기존 봉이 잘못됐거나 일별
자료와 충돌하면 그 날짜의 오류를 `candle_repairs`에 기록하고 다른 날짜는 계속 처리한다.
같은 날의 기존 값을 조용히 덮어쓰지 않으며 복구하지 못한 품질 구간은 그대로 남는다.
옵션을 생략한 엄격 모드의 전체 검증·게시 규칙은 유지한다.

일별 복구의 목록 조회·CHECKSUM 확인·ZIP 다운로드에도 기존 `--workers`를 적용한다.
`--workers 1`은 순차 실행이며 기본은 4, 최대는 32다. 미완성 캐시에서는 현재 종목의 거래가격·
마크가격 복구 요청을 함께 준비하고, 정상 봉 검사를 통과한 날짜만 병렬 요청한다.
실제 동시 수는 해당 종목에 필요한 파일 수에 따라 설정값보다 적을 수 있다. 여러 종목의
월별 원본 파싱·DataFrame 병합·캐시 저장을 동시에 실행하는 방식은 아니다.
워커마다 HTTP 세션을 따로 만들고 재사용하며, 등록된 future는 최대 `workers × 2`개로 제한한다.
같은 key의 중복 요청은 하나로 합치고, 완료된 보충 파일의 메타데이터는 메인 스레드만
`candle_repairs.json`에 원자적으로 기록한다. 종료 시 세션을 닫는다.
중단하면 아직 시작하지 않은 요청은 취소하고, 진행 중인 HTTP 요청을 정리한 뒤 완료된 ZIP의
보충 이력을 저장한다. 네트워크 타임아웃·재시도 중이면 종료에 시간이 걸릴 수 있다.
재개는 기존 명령에 원하는 `--workers 4` 또는 `--workers 8`을 붙여 실행한다.

앞부분의 자료가 없는 기간을 연구에서 제외하려면 기존 정규화 명령에
`--cache-incomplete --exclude-incomplete-prefix`를 지정한다. 새 전용 실행기는 없다.
기존 복구 기록에 원본 간 가격 충돌이 남아 있으면 `conflicting_candles`로 계속 차단한다.
충돌한 시각이 없는 과거 기록은 해당 UTC 날짜의 사용 가능 구간을 보수적으로 차단한다.
종목마다 가격 OHLCV와 마크가격이 함께 관측되는 첫 구간부터 사용하며, 두 종류의 앞부분
공백이 겹치면 둘 다 끝나는 시각을 사용 시작일로 기록한다. 이후에 나타나는 공백을 건너뛰어
가장 좋은 연속 구간을 고르는 방식은 아니다. 잘못된 값은 시작 후에도 오류로 남는다.

재상장·거래 재개 이후부터 연구하려면 기존 `--instruments-json` snapshot의 해당 종목에
`research_start_time_utc`, `research_start_source`를 추가한다. 예:

```json
{"research_start_time_utc": "2022-09-27T02:30:00Z", "research_start_source": "https://www.binance.com/en/support/announcement/detail/adabdfbc53344094808a7bea464f101b"}
```

이 값은 공식 상장일 override인 `trading_start_time_utc`와 별개다. 출처가 필요하고 기록된
거래 시작보다 앞설 수 없다. 봉 주기에 맞춰 올림하므로 위 예시의 1시간봉은 03:00부터 사용한다.
해당 시작일 이전의 구계약과 관측값을 같은 연구 포지션으로 이어붙이지 않는다. 데이터가 있다는
것만으로 거래 재개를 추측하지 않으며, 계약 기간 미확인 종목을 이 옵션으로 정상 판정하지 않는다.

`cache_observed/symbols`의 Parquet·품질 기록은 원래 관측 그대로 보존한다. 공통 `wide` 캐시만
사용 시작일 이전을 `NaN`, `tradable`을 false로 게시하고, 원래 상장 정보와 함께 별도의
`instruments.json.records.<시장>.data_start_inclusive`를 저장한다. 사용 가능한 관측이 없으면
null이고 해당 종목은 편입되지 않는다. `excluded_prefixes`에 제외 범위·봉 수를 남기고,
`observation_quality_summary`는 제외 전 품질, `quality_summary`는 연구용 뷰에서 남은 품질이다.
원래 품질 구간은 종목별 manifest에서 다시 확인할 수 있다. 옵션을 생략하면 원본 관측으로
공통 캐시를 다시 만들 수 있다. 연구 시작 정보만 바뀌어도 월별 ZIP을 다시 파싱하지 않는다.

계약 기간 미확인 종목은 예외적으로 공용 캐시에서도 실제 관측값 전체를 보존한다. 해당 종목을
통째로 `NaN`으로 가리지 않고, 공동 가격 관측 범위를 별도 기록해 메모리상의 사용 구간을
제한한다. 원래 계약 기간 미확인 표시는 남기므로 기본 백테스트의 거부 정책은 유지된다.

v2 피처 계산은 사용 시작 전 입력·중간 조건을 가리고, 보유 상태가 그 이전에 만들어져 이어지지
않게 한다. 유니버스와 비중 생성도 같은 사용 구간을 적용하며, `__cache_eligible__`는 이를 위한
예약 내부 이름이다. Numba 선물 엔진은 기존의 미편입 구간 현금 유지 처리를 재사용한다.
이 정책은 포트폴리오 전체의 평가 시작일을 변경하지 않는다. 수익률 평가 구간은 기존
`trim_start_mode`·`trim_start_timestamp` 설정을 사용한다. Freqtrade의 모든 실행 종목과 직전
워밍업 봉에 완전한 가격이 있어야 하는 기존 제약은 유지하므로, 서로 다른 시작일을 포함한
다종목 전체 기간은 Numba 경로를 사용하거나 Freqtrade의 공통 유효 구간을 선택한다.

남은 거래가격 공백은 `/fapi/v1/klines`, 마크가격 공백은 `/fapi/v1/markPriceKlines`로 조회한다.
요청은 UTC 고정 경계의 최대 499봉 구간으로 묶고, 확인된 거래 기간과 캐시 종료일 안으로 제한한다.
응답이 가득 차도 그 구간의 최대 봉 수를 넘지 않으므로, 공백을 건너뛰는 커서 추측이나
봉마다 한 번씩 호출하는 방식이 필요 없다. `--interval`과 같은 주기의 OHLCV만 사용하고,
종가 시각까지 검사해 미완성·중복·역순·범위 밖·잘못된 가격을 거부한다.
거래가격 거래량은 실측값이며 마크 응답의 volume 필드는 거래가격 거래량으로 사용하지 않는다.
REST 요청은 메인 스레드에서 순차 처리하며 시작 사이를 최소 0.15초(`--pause-seconds`가 더 크면 그 값)로 둔다.
`--workers`를 늘려 REST 요청을 동시에 늘리지는 않는다. 기존 HTTP 재시도·백오프는 재사용한다.

원본 응답은 `candle_rest_repairs/<심볼>/<데이터종류>/response-<SHA256>.json`에 보존하고,
조회 구간별 JSON 영수증에 심볼·봉 주기·endpoint·실제 요청·조회 시각·응답 SHA256을 기록한다.
응답마다 저장하므로 종목 캐시 게시 전에 중단돼도 완료된 응답을 재사용한다. 영수증이 갱신돼도
이전 캐시에서 참조하는 원본 응답 파일은 유지한다. 완전한 응답은 명시적 복구 실행에서도 재사용하고,
미완성·빈 응답 또는 기존 값과 충돌하는 응답은 명시적 복구 때 재조회할 수 있다.
옵션이 없으면 원격 조회를 하지 않으며, 저장된 응답의 식별 정보와 SHA256이 잘못되면 오류다.

기존 봉과 겹치는 실제 OHLCV를 대조하고 값이 다르면 덮어쓰지 않는다. 미완성 캐시에서는
오류 날짜를 남기고 다른 날짜의 보완을 계속하며, 일별 자료에서 충돌한 날짜를 REST로 덮어
경고를 숨기지 않는다. API가 반환하지 않는 시각은 그대로 누락이다. 계약 기간 미확인 종목에는
추측한 거래 기간으로 REST 요청을 보내지 않는다. 이 보충은 `candle_repairs`의 `source_file`,
`request_file`, `sha256`, `dataset`, `interval`, `restored_bars`, `timestamps_utc`로 추적한다.
`report.json.candle_rest_repair_files`는 이번 실행에서 사용한 REST 응답 파일 수다. 일별 ZIP 파일 수는 포함하지 않는다.

펀딩 원본이 부족하면 같은 수집기의 **`--repair-funding-gaps`**로 공식 REST 정산 관측을
보완한다. 아카이브 모드 전용이며 `--normalize-only`와 함께 쓸 수 있다. 기본값은 비활성이다.

```powershell
py scripts/binance_futures_collector_v2.py --source archive --all-symbols --asset-filter crypto --data-group core --start 2020-01-01 --out-dir data/futures/binance/archive_universe/1h --normalize-only --cache-end 2026-10-28 --cache-incomplete --repair-funding-gaps
```

정산 달력상 빠진 이벤트가 있는 달만 `/fapi/v1/fundingRate`로 조회한다. 쿼리는 종목의 유효
구간과 한 UTC 월 안으로 제한하고, 시간 정렬의 기존 60초 미만 허용오차를 위해 양쪽 경계에
60초를 더해 요청한다. 결과는 원래 구간으로 자른다. 한 달의 시간별 정산은 1,000건 미만이므로
응답이 1,000건이면 완전성을 확인할 수 없어 거부한다. 여러 달의 보완은 순차 처리하고,
요청 시작 사이를 최소 0.65초(`--pause-seconds`가 더 크면 그 값)로 둔다. 펀딩 조회의 공식
500회/5분/IP 한도를 고려한 것으로, 다른 프로세스와 공유하는 전체 IP 한도를 예약하는 것은 아니다.
`--workers`는 기존 ZIP 목록·다운로드·캔들 복구용이며 펀딩 REST 요청 수를 늘리지 않는다.

`funding_repairs/<심볼>/`에 쿼리 범위·출처·조회 시각·원본 응답·응답 SHA256을 담은 JSON
영수증과, 응답 SHA256으로 이름 붙인 원본 JSON을 저장한다. 영수증을 원자적으로 게시하므로
중단 후 이미 필요한 이벤트가 있는 응답을 재사용한다. 원본 JSON은 재조회 후에도 보존한다.
월별 ZIP·고정 inventory·snapshot·`downloads.json`은 바꾸지 않는다. 이미 받은 응답은 옵션을
생략한 오프라인 재변환에서도 재사용한다. 미완성/빈 응답이나 기존 관측과 충돌하는 응답을
원격에서 다시 확인하려면 `--repair-funding-gaps`를 명시해 재실행한다.

같은 정산 시각의 기존 값과 REST 값이 다르면 그 달의 보완을 적용하지 않는다. 중복·잘못된
숫자·다른 종목·범위 밖·시간 정렬 불가 응답도 거부한다. 관측된 시각만 추가하고 기존 값을
덮어쓰거나 빈 정산을 0으로 만들지 않는다. API도 반환하지 않는 공백은 경고로 남는다.
`--cache-incomplete`는 다른 달을 계속 보완하고, 엄격 모드는 미해결 정산이 있으면 기존
캐시를 유지한다. 계약 생애나 정산 달력이 미확인인 경우 임의의 과거 일정으로 REST를 요청하지 않는다.
복구한 시각·수·원본 파일·SHA256과 실패 이유는 `symbols_detail.<심볼>.funding_repairs`,
이번 실행에서 사용한 응답 파일 수는 `report.json.funding_repair_files`에 기록한다.

명시한 공통/종목별 정산 달력은 CSV 행의 간격 표기보다 우선한다. 간격 변경 경계의 행에
이전 간격이 적혀 있어도, 실제 정산 시각과 값이 명시한 달력에 맞으면 통과한다. 예상 밖 정산과
실제 누락 검증은 유지한다. 기존 `--funding-schedules-json`의 종목별 `funding_schedule`을
사용할 수 있으며, SOL의 기존 연구 설정 `configs/grid_sol_futures_sma_long_60m_20_v2.json`에는
확인된 2022년 11월 일정이 있다. 다종목 전체에 SOL 일정을 공통 적용하지 않는다.
설정을 생략하면 CSV의 간격 표기를 바탕으로 일정을 생성하되, 아래 공식 규칙으로 확인할 수 있는
1→4시간 전환만 유효 경계를 보정한다. API의 빈 응답만 보고 달력을 추측하지 않는다.

Binance는 [2026-01-02 12:00 UTC부터 적용한 공식 규칙](https://www.binance.com/en/support/announcement/detail/e4445d0389ce4defa6009021fcf6ee46)에서
1시간 정산의 펀딩률 절댓값이 16회 연속 0.025% 이하이면 4시간 정산으로 복귀하도록 정했다.
수집기는 연속된 실제 16회 관측과 직전 기준 초과 관측(또는 관측으로 확인된 1시간 구간의 시작)을
확인하고, 마지막 1시간 정산 다음 시각부터 4시간 달력을 적용한다. 규칙 시행 시각에 충족한
경우도 허용한다. 예를 들어 마지막 시간별 정산이 04:00이면 경계는 05:00이고 다음 정산은
08:00이다. 첫 CSV 행이나 캐시 시작을 임의의 카운터 초기화로 보지 않는다. 새 달력에서 빠진
08:00 등은 계속 실제 누락으로 남기며, 다른 시기의 정책이나 다른 간격 변경에 이 규칙을 확장하지 않는다.

근거가 부족한 간격 증가 경계의 공백은 `unverified_funding_transition`으로 기록한다.
이것은 정산이 없었다는 확인이나 정상 판정이 아니며, 해당 구간의 선물 백테스트는 차단된다.
예상 정산 자체가 불확실하므로 이 경계만을 대상으로 REST 복구 요청을 반복하지 않는다.
확실한 달력 안의 `missing_funding`은 기존 REST 복구 대상이다.
`funding_archive_row_schedule`은 원래 CSV 표기 일정을, `funding_calendar_evidence`는 적용한 규칙·출처·
관측/전환 시각을, `funding_transition_uncertainties`는 미확정 범위를 보존한다.
`funding_calendar_version: 2`로 처리 버전을 남긴다. 이전 관측 캐시는 저장된 일정과 실제 펀딩값으로
재검증하므로 월별 ZIP을 다시 파싱하지 않는다. 일정 메타데이터 갱신은 펀딩 관측값을 변경하지 않는다.
REST로 이전에 빠진 실제 관측을 보완하면 전환 근거도 다시 평가한다. 이미 확인한 전환 근거는
캐시 범위가 짧아져 이전 관측을 읽을 수 없더라도 보존한다. 관측값을 덮어써 근거를 만드는 방식은 사용하지 않는다.

확인된 다종목 수집용 일정은 `configs/binance_futures_funding_schedules_v2.json`에 둔다.
현재는 기존 SOL 연구에서 검증한 8→4→2→8시간 일정만 포함하며, 첫 8시간 구간은 아카이브의
SOL 시작일인 2020-09-14를 포함한다. 다른 종목의 일정을 SOL과 같다고 가정하지 않는다.
변경 근거는 [바이낸스 SOL 정산 빈도 공지](https://www.binance.com/en/support/announcement/detail/e8be17e1e544418490e86723d84759f0),
기존 연구 config와 공식 `/fapi/v1/fundingRate`의 실제 정산 이력이다. 2시간·8시간 전환 시각은
실제 API 이력과 일치하는 기존 명시 일정을 사용하며, 해당 공지가 모든 전환을 명시한 것은 아니다.

현재 수집 폴더의 `instruments_reviewed_v2.json`은 고정 `instruments_snapshot.json`을 복사해
출처가 확인된 `trading_start_time_utc`·`trading_start_source`만 보정한 입력 사본이다.
`review`에 원본 스냅샷 파일·SHA256·검토 시각·적용한 보정을 기록한다. 공식 개장 시각은
[SKL 2020-12-08 07:00 UTC](https://www.binance.com/en/support/announcement/detail/61a763cd2fba47e7b5fc912057fb58f9),
[GTC 2021-06-11 03:30 UTC](https://www.binance.com/en/support/announcement/detail/c1a900363bd04ffd94032322f10de8ab),
[1000PEPE 2023-05-05 16:30 UTC](https://www.binance.com/en/support/announcement/detail/41993e3389654713946bcb6b9b032eaf),
[1000FLOKI 2023-05-06 16:30 UTC](https://www.binance.com/en/support/announcement/detail/cb89820730db4065b1d406350f6c0d9d)다.
기존 규칙대로 개장 후 첫 완전한 봉부터 포함한다. 원본 `onboardDate`·고정 inventory·snapshot을
덮어쓰지 않으며 첫 관측값만 보고 다른 종목의 개장일을 자동으로 바꾸지 않는다.

아래 명령은 저장한 보완 자료를 오프라인으로 재사용해 보정과 분류를 적용한다.

```powershell
$d = 'data/futures/binance/archive_universe/1h'
py scripts/binance_futures_collector_v2.py --source archive --all-symbols --asset-filter crypto --data-group core --start 2020-01-01 --out-dir $d --normalize-only --cache-end 2026-10-28 --cache-incomplete --instruments-json "$d/instruments_reviewed_v2.json" --funding-schedules-json configs/binance_futures_funding_schedules_v2.json
```

이 메타데이터를 계속 사용하려면 재개 시에도 두 입력 옵션을 유지한다. 사용한 명시 메타데이터
입력의 절대 경로·SHA256은 `report.json.metadata_inputs`와 wide manifest에 기록한다.
생애가 확인된 다른 종목의 실제 가격·펀딩 공백은 이 보정으로 숨기지 않는다.

보고서의 `data_group`, `files`, `zip_bytes`는 이번 실행 대상을 표시하며,
`deferred_files`, `deferred_zip_bytes`는 자산 필터를 통과했지만 이번 그룹에서는 미룬 자료다.
이미 수집한 metrics ZIP도 core 실행에서 삭제하지 않는다. core 캐시에는 metrics 필드가 없으므로
그 필드를 사용하는 전략은 metrics를 통합한 뒤 실행해야 한다.
`--asset-filter crypto`와 `--reuse-inventory`를 함께 쓰면 저장된 전체 목록에서 이번 실행의
다운로드·변환 대상만 좁힌다. `inventory.json`은 필터 전 목록을 유지하며, 신규 조사에서도
원격 파일 목록은 전체를 보존한다. 따라서 목록 조회 횟수를 줄이는 옵션은 아니다.
이미 받은 제외 상품의 ZIP도 삭제하지 않는다. 재개할 때도 같은 필터 옵션을 지정해야 한다.
`report.json`에는 필터 적용 후 `files`, `zip_bytes`, `selected_symbols`, 제외 사유가 있는
`excluded_symbols`, `excluded_files`, `excluded_zip_bytes`, 유지한 `unclassified_symbols`를 기록한다.
분류에 쓰는 현재 snapshot은 완전한 과거 상품 변경 이력이 아니므로, 심볼이 다른 유형으로
재사용된 경우 과거 코인 계약까지 제외할 수 있다. 이런 경우에는 해당 기간의 출처 있는
snapshot을 제공하거나 `all`로 원본을 수집해야 한다. 미확인 과거 계약은 원본 수집을 유지해도
계약 생애 정보가 보완되기 전까지 검증 캐시에서는 제외되고 최종 상태가 `partial`일 수 있다.
`--refresh-archives`로 원본을 교체하면 이전 캐시 manifest가 기록한 원본 해시와 달라질 수 있다.
이때 새 변환이 실패하면 이전 변환 캐시는 유지되지만 원본 버전까지 되돌리는 기능은 아니다.

다운로드 워커는 각자 독립 HTTP 세션을 만들고 여러 파일에 걸쳐 연결을 재사용한다.
ZIP 본문 전송 중 연결 끊김·시간 초과·청크/디코딩 오류는 불완전 임시 파일을 제거한 뒤
같은 파일을 처음부터 최대 3회 시도한다. 재시도 대기는 0.5초, 1초이며, 매번 전체 크기·SHA256을
검증한 후에만 완료 파일로 교체한다. 체크섬 불일치·HTTP 404·디스크 오류는 이 본문 재시도에
포함하지 않는다. 기존 HTTP 상태/연결 재시도와는 별도이고, 계속 실패하면 기존처럼 파일별
실패를 기록하며 새 캐시는 게시하지 않는다.
대기 중 작업은 최대 `workers × 2`개만 등록하고, 완료할 때 다음 작업을 공급한다.
체크섬 확인·스트리밍 SHA256·완료 파일 교체 규칙은 유지한다. 진행 개수는 증분 집계하며,
`report.json`의 `downloading` 상태에는 완료 개수·신규/재사용 개수·마지막 갱신 시각을
약 15초마다 저장한다. 콘솔의 `processed/s`는 재사용 파일을 포함한 처리 속도이므로
처음 받은 구간을 건너뛰는 동안 실제 신규 다운로드 속도보다 높게 나타날 수 있다.
보고서·목록·CHECKSUM의 원자적 파일 교체가 일시적인 접근 거부/파일 잠금으로 실패하면
최대 6회 시도하며 총 대기는 최대 2.5초다. 계속 잠겨 있으면 오류를 알리고, 디스크 부족 같은
다른 I/O 오류를 재시도로 숨기지 않는다. 기존 메타데이터는 교체 성공 전까지 보존한다.

저장 경로는 실제 링크를 해석한 뒤 수집 폴더 내부인지 검사한다. Windows에서 병렬로
부모 폴더가 생성되는 순간 `resolve()`가 자식에만 `\\?\` 접두사를 남기는 경우에는,
비교할 때만 일반 드라이브·UNC 경로의 동등한 표기를 맞춘다. 실제 파일 경로는 바꾸지 않고,
다른 드라이브·공유·형제 폴더·외부로 향하는 디렉터리 junction은 계속 거부한다.

현재 `exchangeInfo`만으로 원본 대상을 줄이지 않는다. 같은 심볼이 재상장되거나 다른
기초자산에 재사용된 경우에도 과거 원본 수집 자체가 빠지지 않도록 요청 기간의 아카이브를
유지한다. 다만 캐시 변환에는 현재 스냅샷 또는 출처 있는 `--instruments-json`의 유효 거래기간이
필요하다. 심볼 폴더는 완전한 계약 ID/상장 이력이 아니며, USDT 이름만으로 암호화폐인지
주식·지수 등 다른 기초자산인지 확정하지 않는다. `underlying_type`을 포함한 계약 정보와
시점별 유니버스 검증이 별도로 필요하다.

확인되지 않은 계약은 `report.json.unresolved_instruments`에 사유를 남기고 원본만 보존한다.
실제 개장 시각을 공식 공지 등으로 확인했다면 기존 `--instruments-json`의 해당 심볼 레코드에
`trading_start_time_utc`와 `trading_start_source`를 함께 지정할 수 있다. 예를 들어:

```json
{"trading_start_time_utc": "2023-05-05T16:30:00Z", "trading_start_source": "https://www.binance.com/en/square/post/492070"}
```

위 필드는 기존 exchangeInfo 형식 심볼 레코드에 추가하는 항목이며 독립 snapshot 전체를
대체하는 JSON이 아니다. 실제 개장 시각은 봉 경계로 올림하므로 1시간봉은 위 예시에서
17:00 UTC부터 거래 가능하다. 출력의 `exchange_onboard_time_utc`는 원래 `onboardDate`를,
`onboard_time_utc`·`trading_start_source`는 적용한 개장 시각·출처를 보존한다.
재사용하는 고정 snapshot은 자동 수정하지 않으므로 이후 실행에도 같은 sourced snapshot을
`--instruments-json`으로 지정해야 한다. 출처가 없으면 override를 거부하고, 첫 관측 봉만 보고
거래 시작을 추측해 앞부분을 자동 삭제하지 않는다. 이 경계 처리는 기존 REST 다종목에도 적용된다.
스냅샷 기준 요청 기간과 겹치지 않는 계약은 `inactive_symbols`로 기록하고 정규화에서 제외한다.
이 경우도 이전 계약 이력이 복원됐다는 뜻이 아니다. 유효 거래기간 안의 가격·마크·펀딩 누락,
중복·잘못된 값은 `conversion_failures`로 기록한다. 정규화 대상 중 하나라도 실패하면
이전 공통 캐시를 유지한다. 계약 정보가 미확인인 종목은 검증된 부분집합 캐시에 포함하지 않으며,
부분집합임을 manifest에 명시한다. 자동으로 생존편향이 제거됐다고 간주하지 않는다.

검증된 공통 캐시는 **`<out-dir>/cache_verified`**다. 기존 피처·전략·백테스트의
`source_cache_dir`에는 이 하위 폴더를 지정한다. 필드별 wide Parquet, sparse `funding_rate`,
불리언 `tradable`, `instruments.json`, `manifest.json`을 저장한다. 실제 상폐 자동 정산은
여전히 미구현이다. `--mark-gap-policy 1m-open`은 아카이브 모드에서 거부한다.
명시적인 `--repair-candle-gaps`만 같은 주기의 공식 REST 실측 OHLC를 보충할 수 있고,
1분봉 시가 대체나 보간으로 마크 공백을 숨기지 않는다.

펀딩 일정은 명시한 공통/종목별 설정이 우선한다. 설정이 없으면 공식 펀딩 CSV의
`funding_interval_hours` 필드가 바뀌는 시각을 보존하고, 위 공식 규칙으로 입증되는 전환만
유효 경계를 조정한다. 기존 정산 달력 검증을 통과해야 엄격 캐시에 포함한다. 정산 공백의
길이로 간격을 추측하지 않는다. 입증되지 않은 경계나 정산 누락은 변환 실패로 기록하며,
관측 캐시에서는 구간별 품질 경고와 백테스트 차단을 유지한다. 생성한 종목별 일정은
`instruments.json.records.<시장>.funding_schedule`에 저장한다. 백테스트 contract 설정으로
자동 전달되지는 않으므로 앞서 설명한 엔진별 설정 연결이 필요하다.

`inventory.json`은 계획, `downloads.json`은 다운로드 결과·원본 SHA256,
`report.json`은 게시 결과다. `complete`는 검증 대상 변환 성공, `partial`은 미확인 계약을
제외한 캐시, `downloaded`는 원본 전용 완료다. 계획/다운로드 실패, 정규화 실패로 인한
`no_cache`, 메모리 예산 초과, `partial` 상태에서는 CLI 종료 코드 2를 반환한다. 이전 캐시가 남았다면
그 캐시를 이번 요청의 새 결과로 사용하지 않는다. `--end` 생략 때 실제 적용된 기간은
`inventory.json`에서 확인한다.

수집 폴더의 `.collector.lock`으로 같은 폴더의 중복 실행을 막는다. 강제 종료로 잠금이
남으면 수집 프로세스가 없는지 확인한 뒤 잠금 파일만 제거한다. 새 아카이브 캐시는
폴더 단위로 게시하고 교체 실패 시 이전 폴더를 복원한다. 전원/프로세스가 교체 순간 종료되면
`.cache_verified.previous-*` 복구 폴더가 남을 수 있다. 이것은 동시 읽기를 지원하는
트랜잭션은 아니므로 수집과 백테스트는 동시에 실행하지 않는다. 기존 REST 저장 경로의
파일별 교체 방식은 이 옵션 추가로 바뀌지 않는다.

`--interval`은 `1m`, `3m`, `5m`, `15m`, `30m`, `1h`를 지원하며 기본값은 `1h`다.
`--interval 5m`이면 `data/futures/binance/BTCUSDT/5m`에 별도로 저장한다.
시작·종료 시각은 선택한 봉 주기의 UTC 경계에 맞춰야 한다.
가격과 마크가격은 같은 봉 주기로 수집하지만 펀딩은 실제 정산 시각의 데이터로 유지한다.
수집기는 `mark_price.parquet`에 마크 시가를, `mark_high_price.parquet`와
`mark_low_price.parquet`에 같은 주기의 실제 마크 고가·저가를 저장한다.
Numba의 기존 `numba_futures.liquidation_model: "mark_ohlc"`에서 봉중 청산 판정에 사용한다.
`mark_open`과 기존 엔진의 기본 동작은 바꾸지 않는다. `1m-open`으로 시가만 복구한 봉의
고저가는 `NaN`으로 남으며, 다른 주기의 고저가나 거래가격 고저가로 대체하지 않는다.
아카이브 관측 캐시도 동일한 필드를 내보낸다. 기존 종목별 체크포인트의
`_mark_high`·`_mark_low`(기록된 공식 복구값 포함)를 공용 `wide`에 발행하므로,
같은 입력으로 `--normalize-only --cache-incomplete`를 재실행하면 ZIP 재해석 없이 추가된다.
기존 `--cache-end`, 메타데이터 입력, `--exclude-incomplete-prefix` 등 범위·정책 옵션은
동일하게 유지해야 한다. 결측은 그대로 남고 `mark_ohlc`는 거래 가능한 봉의 필수 고저가가
없으면 중단한다. 출처는 wide manifest의 `mark_extrema` 및 종목 체크포인트의
필드 해시·`candle_repairs`에 기록한다. 봉 안의 정확한 경로·청산 시각은 OHLC만으로 복원하지 못한다.
`--mark-gap-policy` 기본값은 `strict`이며 마크 봉 누락 시 누락 개수와 첫 시각을 표시하고 중단한다.
`1m-open`을 명시하면, 기존 마크 봉의 시가는 유지하고 누락된 각 시각에 대해서만
공식 `/fapi/v1/markPriceKlines?interval=1m`을 조회해 **같은 시각**의 실측 시가를 대체 값으로 사용한다.
해당 1분봉도 누락되거나 잘못된 값이면 저장 전에 중단한다. 직전 값 유지나 보간은 하지 않는다.
이 옵션은 1분보다 긴 봉에서만 사용할 수 있으며 가격 OHLC·거래량·펀딩 누락에는 적용하지 않는다.
전체 5분봉 OHLC를 복원하는 기능은 아니며, 공식 5분봉의 시가와 동일하다고 보장하지 않는다.
원본 마크 응답은 그대로 `raw/markPriceKlines.json`에 보존하고 추가 1분봉 응답은
`raw/markPriceKlines1mFallback.json`에 저장한다. 추가 조회가 없으면 빈 배열을 기록한다.
`manifest.json`의 `mark_gap_policy`, `mark_native_bars`, `mark_original_missing_bars`,
`mark_fallback_bars`, `mark_fallbacks`에 대체 개수·UTC 시각·값·출처를 남긴다.
`mark_bars`와 `mark_missing_bars`는 대체 후 최종 캐시 기준이다. 원본 응답별 SHA-256도 기록한다.
BTC의 2023-11-10 UTC 03:40·03:45·03:50·03:55·04:00은 공식 5분봉이 없지만
같은 시각의 1분봉은 확인됐다. 2020-01-19와 2020-12-17의 일부 누락 시각은
1분봉도 없으므로 위 예시는 마지막 해당 구간 다음 날인 `2020-12-18`부터 요청한다.
이 날짜는 CLI 기본값이 아니며 백테스트 평가 시작일도 자동으로 바꾸지 않는다.
5분봉 백테스트는 해당 캐시와 `timeframe: "5m"`, 연율화 계수 `105120`을 사용하고,
시간 단위 파라미터도 5분봉 개수로 변환해야 한다. 기존 1시간봉 config를 그대로 쓰지 않는다.
`--symbol ETHUSDT`처럼 다른 USDT 무기한 선물도 지정할 수 있다.
`--start`는 포함, `--end`는 제외이며 모두 UTC 시각이다. 종료일을 생략하면
바이낸스 서버 시각의 당일 UTC 자정까지 완료된 봉을 수집한다.
예를 들어 `--start 2019-12-24 --end 2026-10-28`은 10월 27일 마지막 봉까지 받는다.
기본 정산 간격은 8시간이며 시장에 맞게 `--funding-interval-hours`로 지정한다.
기간 중 간격이 바뀌는 시장은 `--funding-schedule-json`으로 명시한 일정을 사용한다.
이 옵션은 일정 배열 JSON 또는 v2 그리드 config의
`backtest_spec_template.freqtrade.funding_schedule`을 읽으므로 같은 config를
수집과 백테스트에 함께 사용할 수 있다. 예:

```json
"funding_schedule": [
  {"start": "2020-09-15T00:00:00Z", "interval_hours": 8},
  {"start": "2022-11-09T20:00:00Z", "interval_hours": 4},
  {"start": "2022-11-10T06:00:00Z", "interval_hours": 2},
  {"start": "2022-11-18T08:00:00Z", "interval_hours": 8}
]
```

각 `start`는 그 일정이 적용되는 경계이며 다음 일정의 `start` 직전까지 유효하다.
첫 일정은 요청·평가 시작을 포함해야 하며, 시각은 중복 없이 오름차순이고
UTC 정시에 맞아야 한다. 경계 자체에 정산이 있을 필요는 없다. 실제 정산은 UTC 자정 기준
해당 간격의 배수 시각에만 발생한다. 예를 들어 1시간→4시간 변경을 05:00부터 적용하면
이전 구간의 04:00 정산은 유지하고 다음은 08:00이다. 배열을 생략하면 기존 고정 간격을 유지한다.
수집기는 일정을 `manifest.json`의 `funding_schedule`에 정규화해 기록하고,
백테스트는 config의 같은 일정을 사용한다. 여러 종목을 한 실행에 넣으면 이 일정이
공통 적용되므로 정산 일정이 다른 종목을 하나의 일정으로 혼합하지 않는다.

일정은 관측 공백에서 자동 추측하지 않는다. 수집기와 어댑터는 공통 검증 모듈
`lib/funding_schedule_v2.py`로 예상 정산 시각을 생성하며, 누락·예상 밖 정산을
오류로 처리한다. 결측을 0으로 채우거나 실제 정산을 고정 간격에 맞춰 버리지 않는다.
모든 정산은 실행 봉 시각에 있어야 하므로 2시간 정산을 건너뛰는 4시간봉 등은 거부한다.
요약의 `Funding Schedule Mode`는 `fixed` 또는 `segments`, `Funding Settlement Events`는
평가 구간에 포함된 예상 정산 시각 수다. 실제 포지션이 비용을 낸 횟수와는 다르다.

공개 API를 사용하며 키가 필요 없다. 가격·마크·펀딩을 전부 다운로드하고
누락·중복·가격 범위를 검증한 뒤, 요청한 전체 구간으로 캐시를 교체한다.
증분 병합 방식이 아니므로 짧은 구간을 지정하면 기존 긴 캐시도 그 구간으로 교체된다.
파일별 저장은 임시 파일에서 교체하며 manifest를 마지막에 기록한다.
전체 파일 묶음이 하나의 트랜잭션은 아니므로 수집기의 저장 단계와 백테스트를
동시에 실행하지 않는다. 수집기는 평가 시작일을 정하지 않으며, 평가 구간은
백테스트 config의 `trim_start_timestamp`로 지정한다.

평가 범위의 연속된 가격·마크·거래량과 시작 직전 bar 한 개가 필요하다.
펀딩은 예상 정산 시각이 빠지면 오류로 중단하며 0으로 채우지 않는다.
평가 bar가 정산 간격보다 길거나, 정산 시각과 bar 시작이 맞지 않는 입력도
중단한다. 현재 정산 간격은 실행 전체·종목 공통 설정이므로 구간마다 간격이
바뀌는 시장은 일정에 맞춘 별도 입력 처리가 필요하다. 현물 OHLC를 선물 OHLC로
대체하지 않는다.

목표비중 timestamp는 **실행할 bar의 시가 시각**이다. 이미 유효한 비중을
Freqtrade로 넘기는 계약이며, 어댑터가 신호를 추가로 한 bar 늦추지 않는다.
종가로 계산한 전략은 유니버스 등의 기존 `signal_lag`를 사용해 미리 지연해야 한다.
NaN 비중은 완료된 주문의 보유 수량을 유지한다. 아직 완료되지 않은 요청이
있으면 당시 확정한 목표 수량의 미체결분만 다음 봉에서 재시도한다. 목표 수량을
현재 자산·가격으로 매번 다시 계산하지 않는다. 새 명시적 비중은 이전 미완료
목표를 교체하고, 명시적 `0`은 미완료 진입을 취소하거나 기존 포지션을 청산한다.
체결 후 강제청산된 포지션은 새 비중 요청 없이 자동 재진입하지 않는다.
여러 종목의 비중 조정은
동일한 조정 전 계좌 자산을 기준으로 계산하고, 감액·청산 후 증액·진입한다.
신규 진입·부분 조정은 거래소 최소 주문액, 정밀도, 가용 증거금 제한을 따른다.
비중에서 계산한 수량을 거래 단위에 맞춰 확정한 뒤, Freqtrade의 증거금→수량
변환에서도 그 수량을 보존한다. 부동소수점 오차로 한 단위 적게 체결하거나
이를 보충하는 추가 주문이 생기지 않도록 체결 전에 검증·적용한다. 최소 금액을
맞추기 위해 목표 수량을 자동으로 늘리거나, 증거금 부족을 숨기며 줄이지 않는다.
요청한 총 절대비중이 거래소 레버리지를 초과하면 실행 전에 오류로 중단한다.

펀딩은 정산 시각 **직전까지 보유한 수량**에 먼저 반영한 뒤 주문한다.
어댑터는 Freqtrade 펀딩 계산 함수의 시작 경계만 마지막 체결 직후로 설정해
진입/조정이 정산 시각과 겹칠 때 중복 부과하지 않는다. 거래소 주문 체결 시각을
bar보다 세밀하게 복원하는 방식은 아니다.

봉 시작 시 거래 시가 또는 마크 시가가 청산가를 넘으면 새 목표 수량을 계산하기 전에
그 시점의 거래 시가로 청산한다. 이 단계에서는 현재 봉의 이후 고가·저가를 읽지 않는다.
봉 내부의 청산 판단·체결은 기존 선물 OHLC와 Freqtrade의 청산 모델을 따른다.
마크 고가·저가나 호가 경로를 복원하지 않으며 청산 수수료·보험기금 정산도 모델링하지 않는다.
가격 갭 때문에 청산 후 잔액이 0 이하가 될 수 있다. 모든 포지션이 닫힌 뒤 평가 장부와
기본 잔액의 일치를 확인한 유한한 0 이하 자산은 `FuturesBankruptcyError`로 구분한다.
NaN/무한대, 장부 불일치, 청산되지 않은 포지션이 남은 음수 자산은 계산·엔진 오류로 중단한다.

검증에 사용하는 자산곡선은 시가 시각의 조정 전 마크 평가액으로, 체결 수수료,
정산된 펀딩과 미실현손익을 포함한다. 마지막 시각에는 새 비중을 적용하지 않고
남은 포지션을 선물 거래 시가로 강제 종료해 수수료까지 반영한다. 모든 거래가
끝난 계좌와 Freqtrade의 최종 잔액이 맞지 않으면 오류로 중단한다.
WF의 각 IS/OOS와 배포 구간은 기존 방식처럼 독립 초기자금으로 시작하고
구간 끝에서 종료한다. 구간 간 포지션·증거금을 계속 들고 가는 시뮬레이션은 아니다.

실행 엔진을 선택해도 기존 그리드·PBO·SPA·WF 명령은 그대로 사용한다.
그리드의 `save_run_artifacts=true`이면 기존 성과 파일에 더해
`futures_trades.json`, `futures_orders.csv`, `futures_account.parquet`를 저장한다.
기본적으로 계좌 기록과 자산곡선은 Zstd 압축 Parquet로 저장하며, `artifact_format="csv"`이면 CSV로 저장한다.
통계 검증은 변환된 자산곡선에서 수익률을 읽는다. 벤치마크는 기존처럼
레버리지가 없는 가격/포트폴리오 기준선이다. 실험별 `out_dir`를 분리해 보관한다.

단일 v2 실행기는 `--engine freqtrade --init-cash 1000 --timeframe 60m
--execution-price-column opening_price --freqtrade-spec-json <설정.json>`을 지원한다.
설정 JSON에는 위 표의 `freqtrade` 객체 내용만 넣는다. 나머지 기존 피처·유니버스·
비중·캐시·출력 인자는 그대로 지정한다.

목표비중 시장가 주문의 체결가는 해당 봉의 원본 선물 거래 시가다. 마지막 강제
종료와 부분 조정도 같다. 과거 시가를 현재 호가 단위로 반올림하면 당시 봉의
고저가 범위를 벗어나 시장가 청산이 미체결로 남을 수 있어, 이 주문에는 현재
호가 단위로 가격을 다시 반올림하지 않는다. 이는 시가 체결 백테스트 모형이며
과거 호가·호가창 복원이나 실제 시장가 체결을 보장하는 모형은 아니다.
엔진의 강제청산(`liquidation`)은 이 시가 보정을 적용하지 않고 기존 가격 판정을 쓴다.

요약의 `Futures Execution Model=market_open_fixed_quantity`는 위 수정이 적용된
결과를 나타낸다. 이전 결과 파일을 자동 갱신하지 않으므로 기존 선물 연구는
재실행해야 수정된 체결 모형으로 비교할 수 있다.
`Rejected Signals`는 기존 엔진 거절과 어댑터의 실패한 주문 시도 횟수를 합한다.
`Native Rejected Signals`는 기존 엔진 값이며, `Rejected Below Minimum`,
`Rejected Below Quantity Precision`, `Rejected Insufficient Margin`,
`Rejected Native Stake Adjustment`, `Rejected Native Entry`,
`Rejected Reduction Below Minimum`은 어댑터 사유별 집계다. 같은 목표의 재시도도
횟수에 포함된다. `Unfilled Target Requests`는 새 목표로 교체되거나 실행 끝까지
남은 미완료 목표의 수다. 최소 수량 미만으로 0이 된 비영 목표도 여기에 집계한다.

현재 비례 슬리피지를 조용히 무시하지 않고 `slippage != 0`이면 중단한다.
강제청산은 Freqtrade의 OHLC 가정 및 거래소 모형을 따르며 실제 청산
수수료는 Freqtrade가 계산하지 않는다. 별도 전략 손절·ROI 청산은 넣지 않고
목표비중 청산과 엔진의 증거금/청산 처리를 사용한다. 이 연결은 연구용
백테스트이며 기존 현물 라이브 실행기·스케줄러를 변경하지 않는다.

마크가격은 자산평가·펀딩에 사용하며, 청산 발동은 Freqtrade의 선물 거래 OHLC
고저가 가정을 따른다. 마크 고저가에 따른 별도 청산 판정은 추가하지 않았다.
계약 정밀도·증거금 구간은 공개 거래소 메타데이터를 사용한다. 거래소의 과거
규칙 변경까지 복원하는 입력은 현재 연결에 포함되지 않는다.

기준 동작: [Freqtrade callbacks](https://www.freqtrade.io/en/stable/strategy-callbacks/),
[Freqtrade leverage와 백테스트 제한](https://www.freqtrade.io/en/stable/leverage/).

## 평가 기간과 통계 검증

`trim_start_mode="timestamp"`이면 PBO와 워크포워드도 `trim_start_timestamp`
이후의 가격 시점부터 평가 구간을 나눈다. 후보마다 날짜가 다르면 가장 늦은
날짜를 공통 시작점으로 사용한다. 지표 계산에 필요한 이전 데이터는 유지한다.
워크포워드의 `--is-months`는 이 시작점부터 세므로, 준비 기간을 더해 지정하지
않는다. 예를 들어 준비 기간 2년을 포함해 `48`개월로 실행하던 실험에서 실제
평가 기간이 2년이었다면, 같은 OOS 시작점을 유지하려면 `24`개월을 지정한다.
`none`과 `first_weight`의 기존 동작은 유지하며, `first_weight`로 PBO/WF의
공통 시작점을 자동 추정하는 기능을 추가한 것은 아니다.

피처 준비와 메모리: 그리드, 워크포워드, PBO, SPA는 같은 피처 준비 경로
(`CandidateFeatureBuilder`, `scripts/run_cross_section_grid_v2.py`)를 쓴다.
`shared_feature_spec_template`은 데이터 사용 정책마다 한 번만 만들고, 후보의
`feature_spec_template`은 그 위에 출력만 더한다. 공유 피처 중 어느 후보의 universe·weight·
백테스트 설정도 참조하지 않는 중간 결과는 워크포워드·PBO·SPA에서 버린다(이 도구들은 피처
산출물을 저장하지 않는다). 그리드는 기존 규칙(`save_run_artifacts`일 때 전부 보관)을 따른다.
PBO·SPA는 후보 피처를 평가 직전에 만들고 평가 후 해제하므로 최대 메모리가 후보 수에
비례하지 않는다. 워크포워드는 후보를 바깥 반복으로 돌며 후보마다 피처를 한 번 만들고, 그 후보의
모든 폴드 IS·OOS 구간에서 전체 기간 universe와 비중을 재사용한다(`universe.holding`이 없고
구간 데이터 정책 적용 후 universe 설정이 같을 때). 결과 행 순서는 폴드 순서를 유지한다.
공유 피처를 뺀 후보별 프레임은 `--candidate-feature-cache-mb`(기본 1024) 안에서 처음 들어온
후보부터 보관해 이어붙인 OOS 곡선을 만들 때 다시 쓰고, 상한을 넘는 후보는 그때 다시 만든다.
결과는 같고 시간만 늘어난다. `strategy_sleeves` config는 sleeve 준비 캐시가 피처 객체 동일성을
쓰므로 모든 후보를 보관한다.

워크포워드 구간 시작: 각 IS·OOS 구간과 이어붙일 OOS 배치 구간은 빈 계좌로 시작한다. 리밸런싱
사이를 `NaN`(수량 유지)으로 두는 비중 출력은 구간 첫 행의 `NaN` 칸을 그 시점까지 종목별로 마지막에
낸 목표로 채워 첫날 진입한다(`change_only`는 원래 첫 행에 전체 목표를 낸다). 진입 비용은 첫날 낸다.
`universe.holding`과 `strategy_sleeves`는 각자의 구간 규칙을 따른다. 이어붙인 OOS 곡선은 구간
첫 값의 `init_cash` 대비 변화를 포함하고, `single_market` 벤치마크는 맞닿은 구간 경계를 같은
가격 시계열로 이어 실제 지수와 같다. 포트폴리오형 벤치마크는 경계일 수익률을 0으로 둔다.

후보 피처의 재사용은 피처 정의 전체 단위다. 한 정의 안의 앞부분 단계가 모든 후보에서 같고
마지막 단계만 grid 값으로 달라지면, 앞부분도 후보마다 다시 계산된다. 이때 공통 앞부분을
`shared_feature_spec_template`의 피처로 분리하고 후보 피처는 그 피처를 `source`로 삼아 달라지는
단계만 둔다. 예를 들어 `trade_price → simple_return → mask_by_feature → cross_percentile →
calendar_mean → calendar_rolling_sum({rank_months}, {skip_months})`는 `calendar_mean`까지를 공유
피처로 옮긴다. KOSPI 보통주 시총 100 16개 후보 그리드에서 결과는 같고 실행 시간이 152초에서
92초로 줄었다(최대 메모리 3.09GB → 3.25GB).

PBO 실행기 `scripts/research/analyze_pbo.py`는 후보마다 전체 평가 기간을 한 번
백테스트하고 공통 날짜의 수익률을 사용한다. IS/OOS에 선택된 구간의 수익률을
시간순으로 이어 붙여 지표를 다시 계산하며, 구간별 지표의 평균으로 순위를
정하지 않는다. `air`는 전략과 벤치마크의 수익률 차이, 나머지 지표는 전략
수익률을 입력으로 사용한다. 연결된 수익률로 CAGR/낙폭을 계산할 때는 단위
초기 자산을 앞에 두므로 관측치 한 개가 복리 계산 한 구간에 대응한다.
후보별 연율화는 백테스트의 `periods_per_year`를 따른다.

모든 절반 조합과 IS/OOS를 뒤집은 조합을 포함한다. OOS 순위는 오름차순
평균 순위를 `후보 수 + 1`로 나누며, PBO는 `lambda <= 0`, 즉 중앙 순위
이하인 비율이다. 동점 IS 최고 후보는 이름 정렬상 첫 후보로 정한다.
점수를 계산할 수 없는 후보가 있는 조합은 후보를 빼고 진행하지 않고 오류로
알린다. `--max-splits 0`은 전체 조합이며, 제한할 때는 양방향 쌍을 유지하도록
짝수를 지정한다. 여러 anchor를 지정하면 전체 조합을 합쳐 PBO를 집계하며,
anchor별 PBO도 별도로 저장한다. PBO의 IS/OOS 할당은 Walk-Forward와 달리 시간순으로 고정되지 않는다.

기존 `--subperiod-months`와 `--anchor-month-offsets`는 유지한다. 이 분할은
달력 기준이므로 블록별 관측치 수가 같다는 보장은 없다. `pypbo`의 동일 관측치
수 분할과 비교할 때는 입력 수익률뿐 아니라 블록 경계도 맞춰야 한다.
불완전한 마지막 구간과, 구간 수가 홀수이면 마지막 완전 구간도 제외하는 기존
규칙은 유지한다. 요약 JSON의 `coverage_by_anchor`에 실제 사용 시작/끝,
블록별 관측치 수, 제외한 앞/뒤 관측치 수를 기록하며 실행 종료 시에도 출력한다.
공통 날짜 정렬로 제외된 관측치 수도 요약에 기록한다.

결과 파일명과 실행 옵션은 유지한다. `pbo_period_score_matrix.parquet`와
`pbo_period_scores_long.csv`는 구간별 진단용이며 PBO 계산의 직접 입력이 아니다.
long CSV는 선택한 지표와 관측치 수를 기록하며 이전의 개별 구간 백테스트
통계 열은 제공하지 않는다. 요약 JSON의 `method_version: 2`가 새 계산을 식별한다.
기존 계산 결과는 자동 변경되지 않으므로 새 방식의 PBO가 필요하면 다시 실행한다.
SPA와 워크포워드의 계산 방법은 이 PBO 변경으로 바뀌지 않는다.

벤치마크 상대지표는 전략과 벤치마크에 실제 값이 모두 존재하는 공통 관측기간에서 계산합니다. 전략의 전체기간 CAGR, MDD 등 절대지표는 전략 자체의 전체기간을 유지하며, 요약에는 `Benchmark Comparison Start`, `Benchmark Comparison End`, `Benchmark Comparison Observations`가 기록됩니다.

전략 절대지표의 시작점: 그리드·단일 백테스트·워크포워드(폴드와 이어붙인 OOS) 요약의 `CAGR [%]`, `Calmar Ratio`, `Ulcer Index [%]`, `Martin Ratio`, Sharpe·Sortino·CVaR Sharpe는 자산곡선 첫 값 한 봉 앞에 초기 자금을 붙여 계산한다(TPE 평가와 같은 기준). 첫 봉의 진입 수수료·슬리피지가 수익률 한 기간으로 들어가고, 연환산 기간 수는 관측치 수와 같다. 이어붙인 OOS 요약의 `Start Value`·`Total Return [%]`·`Max Drawdown [%]`도 초기 자금 기준이다. vectorbt 통계의 `Total Return [%]`·`Max Drawdown [%]`와 벤치마크 비교 지표(공통 기간 첫 값으로 정규화)는 바뀌지 않았다.

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

이 설정에서 전략 가격과 피처는 최상위 `source_cache_dir`에서 읽고, KOSPI 가격만 `kr_stock_daily/index_price.parquet`에서 읽는다. 벤치마크 캐시는 성과 비교에만 사용되며 전략의 투자 후보나 피처 계산에는 들어가지 않는다. 백테스트가 저장한 `benchmark_curve.csv` 또는 `.parquet`는 불확실성 분석 등 후속 검증에서도 그대로 재사용한다.

`kr_stock_daily/index_price`의 열: `KOSPI`(^KS11 종합지수), `KOSDAQ`(^KQ11)은 원천 `index_price.parquet`(yfinance)에서,
`KOSPI200`은 ETF 원천 `data/kr_etfs/raw/ohlcv_pykrx.parquet`의 069500 `IndexValue`(pykrx, 배당 미포함 가격지수)에서 온다.
캐시 날짜에 없는 값은 채우지 않고 `NaN`으로 둔다. 출처와 결측 수는 `source_cache_report.json`의 `index_sources`에 남는다.

```
py scripts/kr_stocks/build_kr_stock_source_cache.py --include-industry --industry-frequency monthly --etf-index-columns KOSPI200=069500
```

`--etf-index-columns NAME=ETF코드[,…]`로 다른 ETF 지수도 같은 방식으로 붙일 수 있다(`--etf-index-raw`, `--etf-index-value-column`으로 원천 파일·열 변경).
이미 있는 열 이름이면 오류로 중단한다. 이 스크립트는 캐시의 모든 원천 파일을 다시 쓰므로, 원천이 갱신된 상태에서
재생성하면 다른 피처도 바뀔 수 있다. 피처를 유지하려면 임시 `--out-dir`에 만든 뒤 기존 캐시와 비교하고 필요한 파일만 반영한다.

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

### 그리드·검증 사전선별 (`preselection`, 선택 사항)

`scripts/run_cross_section_grid_v2.py`의 전수 grid와 같은 실행기의 TPE/random 후보 평가에서
사용하며, 같은 config를 받는 v2 PBO·SPA·WF 재계산에도 적용한다. 생략하거나 `null`이면
기존 로딩·유니버스·비중 경로를 유지하며 선별 파일도 읽거나
쓰지 않는다. 별도 선별 실행기나 새 피처 연산자를 만들지 않고 기존 FeatureSpec 의존성,
UniverseSpec 조건, Parquet 종목 열 제한 읽기를 연결한다.

```json
"preselection": {
  "rebalance_frequency": "daily",
  "universe_spec": {
    "feature_column": "liquidity_7d",
    "mode": "top_n",
    "top_n": 50,
    "ascending": false,
    "lag": 0,
    "signal_lag": 1
  }
}
```

- `universe_spec`는 필수다. 기존 `mode`, `value_filters`, `rank_filters`, `filter_stages`,
  `min_age_days`, 경고 제외, 허용/제외 종목 목록을 사용한다. 피처 이름은 기존
  `shared_feature_spec_template`와 렌더링된 `feature_spec_template`에서 찾고 필요한 선행
  피처만 계산한다. 원시 필드(`trade_price`, 거래량 등)를 직접 지정할 수도 있다.
  정의되지 않은 피처나 순환/상충 정의는 오류이며 임의의 대체 피처를 만들지 않는다.
- `rebalance_frequency` 기본값은 `daily`다. 기존 주기 판정으로 각 날짜/주/월 등의 첫 행에서
  선별 목록을 갱신하고 다음 갱신까지 유지한다. 기존 `every_bar`, `weekly`, `monthly`,
  `quarterly`, `semiannual` 등도 같은 의미로 사용한다. 실제 주문 주기는 기존 WeightSpec이
  담당한다. UTC 인덱스면 UTC 날짜 기준이며 로컬 날짜로 임의 변환하지 않는다.
- 지연은 기존처럼 `lag + signal_lag`이며 필터에도 `signal_lag`를 더한다. 위 예시는 직전
  물리 봉까지의 값을 사용한다. **전체 봉에서 지연 적용 후** 갱신 행을 추출한다. 일별로
  먼저 축약한 표에 1행 지연을 적용해 하루가 더 늦어지는 방식이 아니다. 조건의 지연은
  음이 아닌 정수여야 한다. 피처 자체의 시점/미래 정보 사용 규칙도 기존과 같다.
- 본 전략의 명시적 허용 종목과 사전선별의 허용 종목은 교집합, 제외 종목은 합집합이다.
  본 전략의 경고 제외와 최소 연령도 사전선별에 반영한다. 캐시 거래 가능 기간과 실제
  후보의 펀딩 시작/가격 제외 정책을 선별 피처·순위에 적용한다. 후보별 조건·정책이 다르면
  별도 선별 결과를 사용한다. grid 자리표시자는 이 블록에서도 렌더링된다.
- 전체 캐시 기간에 한 번이라도 선별된 종목의 합집합을 후속 계산의 허용 목록으로 전달한다.
  원래 `allowed_markets_file`를 함께 남겨 목록이 다시 확대되지 않게 한다. BTC 등 피처 참조,
  헤지·방어 자산은 계산에 보존한다. 종목별 이전 봉을 자르거나 미선별 날짜의 가격을 가리지
  않으므로 이후 진입의 롤링/베타 예열은 유지한다. 선별 합집합이 크면 절감 효과는 작다.
- 실제 UniverseSpec을 평가할 때 날짜별 선별 마스크를 **기본 자격에 먼저 교차**한다.
  이후 기존 값/순위/최종 선택 조건이 적용된다. 기간 합집합만으로 과거 진입을 허용하지
  않는다. 이 마스크는 비중 계산의 가격 사용 가능 마스크로 전달하지 않으며, 목록 변경을
  매 봉 강제 청산 명령으로 해석하지 않는다. 보유/청산은 기존 비중·주문 주기를 따른다.
- 후속 FeatureSpec에 `breadth`, `cross_rank`, `cross_percentile`, `group_demean`, `group_rank`,
  `group_percentile`, `corr_greedy_filter`가 있으면 입력 종목 범위를 유지하고 날짜별 선별만
  적용한다. 향후 선별 합집합이 과거 횡단면 계산값에 영향을 주지 않게 하기 위한 처리다.
  양수 `tail_hours`나 `portfolio_group`/`portfolio_group_rebalance` 벤치마크도 각각 원래
  시간축/벤치마크 종목 범위를 유지하기 위해 열 축소를 생략하고 콘솔에 사유를 출력한다.
- 결과 폴더의 `preselection/<key>.parquet`는 갱신 시점별 Boolean 목록,
  `<key>.json`은 원래 조건·피처·데이터 정책·선별 합집합·제외 기록·지문을 보관한다.
  같은 조건은 후보 간 공유하며 재실행에서도 재사용한다. 데이터 지문은 캐시 파일의
  크기·수정/생성 시각을 사용하며 코드·패키지 버전도 고려한다. Parquet 전체 내용의 해시는
  매번 계산하지 않으므로 내용을 바꾸면서 이 파일 속성까지 인위적으로 복원한 경우는
  감지하지 못할 수 있다. 이 경우 결과의 `preselection` 폴더를 비우고 재계산한다.
  저장된 선별 표 자체는 SHA-256으로 검사하며 손상되면 재계산한다. 조건·자료가 바뀌면
  새 key 파일이 생기고 이전 실험 기록은 자동 삭제하지 않는다.
- 요약 CSV에 `preselection_key`, `preselection_union_markets`, `preselection_candidate_markets`,
  `preselection_projection_enabled`가 추가된다. 상세 저장 시 specs의 `preselection.json`과
  백테스트 요약/선물 메타데이터에도 연결 경로를 남긴다. `features.json`·`universe.json`만으로
  단일 백테스트를 재실행하면 날짜별 선별이 재현되지 않으므로 원래 grid config와 선별
  기록을 함께 사용해야 한다.

검증 연결:

- `scripts/research/analyze_pbo.py`, `scripts/research/analyze_spa.py`,
  `scripts/walkforward/run_cross_section_grid_walkforward_v2.py`도 동일한 블록을 읽는다.
  기존 `--grid-config-json` 명령을 사용하며 새 CLI 인자는 없다. 선별·열 축소·날짜별
  조건 교차는 그리드와 같은 함수를 호출한다. 검증마다 결과 폴더의 `preselection/`에
  기록하며, 같은 실행 폴더에서의 재실행은 해당 선별 결과를 재사용한다.
- PBO/SPA는 후보별 전체 비중·자산곡선을 만들 때 이 조건을 적용한다. PBO의 CSCV 분할,
  점수 계산·상보 방향 계산·SPA 초과수익/부트스트랩 수식은 바꾸지 않는다. PBO/SPA의
  공용 피처 준비에는 후보별 펀딩 시작 정책과 `weight_market_scores_spec_template`도
  전달하며, 시작 정책이나 비중 점수가 다른 후보의 피처 캐시는 별도로 관리한다.
- WF는 fold 시작 전에 후보별 선별을 한 번 준비한다. IS·OOS 및 이어 붙이는 배포 구간
  모두 같은 후보의 날짜별 목록을 적용한다. 구간 첫날부터 선별 지표를 다시 예열하거나,
  주간/월간 선별의 갱신일을 구간 첫날로 옮기지 않는다. 기존 전체 과거 피처와 비중 이력을
  만든 뒤 평가 구간을 잘라 현재 목표를 재현하는 방식은 유지한다.
- 동적 유니버스의 미래 진입 종목 때문에 전체 검증 시작이 그 종목 상장일로 밀리지 않도록,
  옵션 사용 시 PBO는 공통 피처 시간축에서 출발해 자산곡선의 공통 구간을 정하고, SPA/WF는
  기본적으로 캐시의 시간축을 사용한다. 명시한 `trim_start_timestamp`는 그대로 적용한다.
  `tail_hours`를 쓴 SPA/WF는 기존 종목별 tail/기준 가격의 기간 처리를 유지한다.
- PBO/SPA 요약 JSON의 `preselection`에 후보별 key·기록 경로·열 축소 여부를 남긴다.
  SPA 후보 CSV에도 key를 기록하고 WF 후보 결과의 IS/OOS 열에도 key·기록 경로를 남긴다.
  선별 옵션이 잘못되면 검증에서도 명시적으로 오류를 낸다. 옵션 없는 기존 검증은 유지한다.

기존 48개 연구 config에는 자동으로 추가하지 않았으며, 활성화하면 기존 유니버스 조건에
앞단 선별이 추가된다. 특히 유동성→베타 순서와 베타 유효 종목 안에서 유동성을 고르는
순서는 서로 다른 전략 정의이므로 기존 조건을 자동 삭제하거나 순서를 바꾸지 않는다.

2026-11-03 실제 캐시의 선별 단계만 진단했다. 653종목×59,808시간봉에서 최근 168봉
거래대금 추정치 상위 50개를 매일 고르면 2,492개 갱신 시점의 선별 합집합은 585종목,
BTC 헤지를 더한 후속 대상은 586종목이었다. 48개 후보가 하나의 선별 결과를 공유했다.
단일 초기 실행의 선별 준비는 약 24.4초였고 전체 백테스트·최대 메모리는 측정하지 않았다.
현재 조건에서 종목 수 감소는 약 10%라 큰 속도 개선을 보장하지 않는다. 진단 산출물은
임시 폴더에서 확인 후 제거했으며 기존 실험 config·성과·원본 캐시는 변경하지 않았다.

### 그리드 피처 메모리 관리

별도 설정 없이 기존 피처·그리드 경로에서 적용한다. 원본 입력은 변경하지 않고 한 번만
작업 사본을 만들며, 동일한 거래 가능 마스크를 같은 의존성에 반복 적용하지 않는다.
피처 그래프의 재귀 함수 참조는 계산 종료 시 해제해 큰 배열이 순환 가비지 수거를
기다리지 않도록 한다. 수치 정밀도·워밍업·시점별 거래 가능 조건은 유지한다.

공통 피처는 첫 후보의 실제 펀딩 시작 정책으로 바로 준비한다. 다른 시작 정책이 있는
후보는 각 설정에 맞게 준비한 피처를 사용하며, 시작 정책이 없는 후보와도 섞지 않는다. 개별 후보에는
그 후보가 읽는 외부 피처만 전달하고, 이미 공통 입력에 있는 원시 필드는 다시 읽지 않는다.
`save_run_artifacts=false`이면 전체 후보의 피처·유니버스·비중·시장점수가 참조하지 않는
공통 중간 표를 계산 후 버린다. 비중의 중첩 기본값과 다른 후보의 베타 창도 보존한다.
상세 산출물을 저장하는 실행은 기존 전체 피처 구성을 유지한다. 필요한 이름을 안전하게
판정할 수 없는 잘못된 후보는 표를 버리지 않고 기존 오류 기록 경로에서 처리한다.

128MiB 노드 캐시에 들어갈 수 없는 큰 수치 피처는 재사용되지 않을 키를 만들기 위해
원본 전체를 해싱하지 않는다. 캐시에 들어가는 피처의 내용·기간·데이터 정책 기반 무효화는
유지한다. 디스크 피처 캐시나 새 CLI 옵션을 추가하지 않았다.

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

## Public release manifest v2

`configs/public_release_v2.json`은 비공개 저장소에서 공개 저장소로 동기화할 파일을 명시적으로 제한한다. 새 파일은 자동 공개되지 않으며, 공개하려면 `include`에 경로를 추가하고 검토해야 한다.

| 필드 | 역할 |
|---|---|
| `schema_version` | manifest 형식 버전. 현재 값은 `2`다. |
| `include` | 비공개 저장소를 원본으로 관리할 공개 파일의 명시적 allowlist다. |
| `preserve` | 공개 저장소에서만 관리하며 동기화나 prune으로 건드리지 않을 README와 예제 파일이다. 백테스트 사례 보고서 `notes/strategy_examples.md`는 비공개 저장소의 `include` 원본에서 동기화한다. |
| `deny` | 데이터, 캐시, 실계좌 설정, 로그, 상태 및 credential 계열 경로를 차단한다. |
| `ignore` | `__pycache__`처럼 Git에 공개되지 않는 로컬 생성물은 드리프트 계산에서 제외한다. |
| `forbidden_content_patterns` | 선택된 파일에서 private key나 알려진 token 형식을 탐지한다. |

먼저 쓰기 없는 검사를 수행한다.

```powershell
py scripts/export_public_release_v2.py --check --public-dir "C:\path\to\quant-trading-system-public"
```

검토가 끝난 뒤에만 동기화한다. 원본과 공개 저장소 Git 작업트리가 모두 깨끗해야 하며, 미관리 파일이 있으면 기본적으로 중단한다.

```powershell
py scripts/export_public_release_v2.py --sync --public-dir "C:\path\to\quant-trading-system-public"
```

미관리 파일을 검토하고 삭제하기로 결정한 경우에만 `--prune`을 추가한다. `--check` 종료 코드는 동기화 상태면 `0`, 안전하지만 차이가 있으면 `1`, manifest 오류나 민감정보 차단이면 `2`다.

기본적으로 원본 작업트리가 dirty면 동기화를 거부한다. 긴급하게 검토 완료된 작업트리 내용을 공개해야 할 때만 `--allow-dirty-source`를 명시하며, 이 경우에도 공개 저장소 작업트리는 반드시 clean이어야 한다.

## 자동 전략 탐색 v2 — 랜덤 / TPE

`scripts/research/search_strategies_v2.py`는 기존 v2 연산을 조립해 **완성된 목표 비중 전략**을 만들고,
기존 백테스트 엔진으로 평가한다. 기본 목적함수는 비용 반영 순수익이며, 지표 선택·가중합·사용자
Python 함수로 교체할 수 있다. 가격 예측 모델을 학습하는 경로와는 별개다.
공통 코드는 `lib/strategy_search_v2/`, 초기 설정은
`configs/research/strategy_search_v2/sol_1h_tpe_v2.json`에 있다.

### 설치와 실행

```powershell
py -m pip install -r requirements-research.txt
$s = 'configs/research/strategy_search_v2/sol_1h_tpe_v2.json'
py scripts/research/search_strategies_v2.py --config-json $s --check
py scripts/research/search_strategies_v2.py --config-json $s
```

동일한 명령을 다시 실행하면 동일 실험을 이어서 진행한다.
`search.budget_mode: "evaluations"`에서 `--trials 200`은 기존 완료 결과를 포함한 **유효 평가 총 200개**이며
추가 200개가 아니다. 유한한 점수가 SQLite에 COMPLETE로 저장된 후보만 세며, 제약을 어긴
`infeasible`도 포함한다. `rejected`·`bankrupt`·중단·실패처럼 점수 없는 후보는 이 예산에서 뺀다.
동일 주문 경로의 계산을 재사용했더라도 점수가 있는 후보는 포함하므로 고유 주문 경로 200개를
보장하는 옵션은 아니다. Optuna의 trial 번호는 생성 시도 번호로 유지하고 평가 번호는 따로 기록한다.

무한 탈락을 막기 위해 전체 생성 시도 상한도 둔다. `--max-attempts` 또는 `search.max_attempts`로
누적 상한을 정하며, null/생략 시 유효 평가 목표의 10배다. 예를 들어 `--trials 600`이면 기본 생성
상한은 6,000회다. 상한에 도달하면 유효 평가가 부족해도 종료하고 로그에 수치와 이유를 명시한다.
다시 진행하려면 같은 실험의 생성 상한을 늘릴 수 있다. `--timeout-seconds 600`은 이번 실행의
소프트 시간 제한으로, 이미 실행 중인 후보는 마친다.

기존 설정에서 `budget_mode`를 생략하면 `attempts` 방식을 유지한다. 이 방식의 `--trials 200`은
탈락·중단·실패를 포함한 전체 생성 200회를 뜻하며, 기본 생성 상한도 200회다.

처음부터 랜덤 탐색과 비교하려면 config의 `search.sampler`를 `random`으로 바꾸고 별도 `out_dir`을
사용한다. 기존 실험의 비용·문법·데이터·코드·패키지를 바꿔 재사용하면 비교가 성립하지 않으므로
재시작을 거부한다. 목표 횟수·생성 상한·실행 시간 예산은 늘릴 수 있지만 예산 방식 변경은 새 실험이다.

### 설정 구조

| 항목 | 의미 |
|---|---|
| `schema_version` | 탐색 설정 형식. 현재 `1`; 실행·피처 엔진은 v2다. |
| `market`, `timeframe`, `source_cache_dir` | 첫 구현은 단일 시장, 일정한 봉 간격의 v2 wide cache를 사용한다. |
| `signal_lag` | 최소 1봉. 종가까지 관측한 신호는 이후 봉 시가에서 실행한다. |
| `period.start`, `period.end` | 공통 평가 범위 제한. 시작 포함·끝 제외, null이면 이용 가능한 범위. |
| `search.sampler` | `random` 또는 `tpe`. 둘 다 같은 문법·평가기·기록을 사용한다. |
| `search.n_trials`, `timeout_seconds` | `budget_mode` 기준 누적 목표와 이번 실행의 시간 상한. |
| `search.budget_mode` | `evaluations`: 유한한 점수가 저장된 후보 수. `attempts`: 탈락 등을 포함한 생성 수. 생략 시 기존 호환용 `attempts`. |
| `search.max_attempts` | null 또는 양의 정수인 누적 생성 상한. CLI `--max-attempts` 우선. null이면 평가 방식은 목표의 10배, 생성 방식은 목표와 동일. |
| `search.seed`, `startup_trials` | 재현용 시드와 TPE의 초기 랜덤 시도 수. |
| `search.workers` | 현재 `1`만 허용. Freqtrade의 전역 거래 상태를 스레드로 공유하지 않는다. |
| `search.max_cached_materials` | 메모리에 유지할 재료 묶음 수. LRU 방식이며 0은 캐시 보관 안 함. |
| `validation.train_fraction` | 공통 평가 구간의 앞부분 중 탐색에 사용하는 비율. |
| `validation.validation_fraction` | 그 다음 평가 비율. 두 비율의 합이 1이면 훈련·평가 두 구간, 1보다 작으면 나머지를 마지막 test 구간으로 사용한다. |
| `selection.min_trades` | 학습 구간 최소 거래 수. 기본 1; 0이면 거래 없는 후보도 허용. |
| `selection.max_drawdown_pct` | 선택 가능한 최대 낙폭. null이면 낙폭에 의한 탈락 조건을 적용하지 않는다. |
| `selection.metric` | 기본 `net_return`. `cagr`, `sharpe`, `sortino`, `calmar`, `martin`, `ir`, `air`, `weighted_sum`, `custom`도 지원한다. |
| `selection.weights` | `weighted_sum`에서만 사용하는 지표 이름 → 가중치의 객체. |
| `selection.function` | `custom`에서만 사용하는 import 가능한 `module:function` 경로. |
| `selection.function_kwargs` | 사용자 함수에 전달할 고정 keyword 인자. 기본 `{}`. |
| `selection.function_files` | 사용자 함수가 참조하는 추가 helper·설정 파일의 경로 목록. 코드 해시에 포함한다. 기본 `[]`. |
| `backtest` | 엔진·시가 체결·초기 자금·수수료·슬리피지·선물 설정. 후보가 변경하지 못한다. |

### 평가함수 선택

목적함수는 항상 **큰 값이 좋은 점수**를 반환한다. 기존 설정처럼 `selection.metric`을 생략하면
`net_return`을 사용한다. `min_trades`와 `max_drawdown_pct`는 목적함수와 별도로 검사하는
Optuna 정식 제약이다. 예를 들어 마틴 비율을 최대화하려면 기존 `selection`을 다음처럼 설정한다.

```json
"selection": {"metric": "martin", "min_trades": 1, "max_drawdown_pct": null}
```

유효한 학습 백테스트에는 다음 제약값을 계산해 Optuna 5의 `Trial.set_constraint`로 전달한다.
각 값이 **0 이하면 충족, 양수면 위반**이며 경계값은 허용한다.

- `min_trades > 0`: `(min_trades - total_trades) / min_trades`. 0이면 이 제약을 끈다.
- `max_drawdown_pct != null`: `(실제 낙폭[%] - 상한[%]) / 상한[%]`. null이면 이 제약을 끈다.

서로 단위가 다른 거래 수와 낙폭을 각 기준값으로 나눠 무차원 값으로 전달한다.
예를 들어 최소 30회 중 15회 거래는 `0.5`, 낙폭 상한 50% 중 실제 60%는 `0.2`의 위반이다.
제약 위반값을 목적함수 점수에서 빼지 않는다. 유한한 점수가 계산된 후보는 위반 여부와 관계없이
실제 점수를 보존하고 Optuna에는 `COMPLETE`로 전달한다. 프로젝트 기록에서는 충족 후보를
`complete`, 위반 후보를 `infeasible`로 구분한다. TPE는 제약 정보를 다음 제안에 사용하지만,
새 후보가 반드시 제약을 지킨다는 보장은 없다. 랜덤 탐색은 제약으로 다음 샘플을 학습하지 않는다.

최종 후보는 **모든 제약을 충족한 COMPLETE trial 중 최고 점수**로 고른다. 전부 위반하면
`best.json`을 만들지 않고 검증·테스트도 시작하지 않는다. 탐색 예산을 늘려 계속 찾을 수 있다.
제약은 완료된 학습 성과로 판단하므로 백테스트를 조기에 중단하는 기능은 아니다. 위반 후보도
유효 평가 예산에 포함한다. 미정의 점수·모순 조건·파산처럼 유한한 점수가 없는 후보는
Optuna에 FAIL로 전달해 TPE 학습에서 제외하고 다음 후보로 넘어간다.
제약 기준 변경은 새 실험이므로 새 `out_dir`가 필요하다. 생략 시 기본 최소 거래 수는 1이며,
낙폭 상한은 null로 끌 수 있다.
이는 학습 결과의 선택 기준이며 매매 중 강제로 50%에서 손절하는 기능이 아니다.

지표 계산은 기존 v2 성과 함수를 재사용한다. 순수익, CAGR, 낙폭, Ulcer Index에는 초기 자금도
포함하고, 첫 실행 봉의 수익률·손실도 반영한다. 연환산은 기존 timeframe 규칙을 따른다.
SOL 1시간봉은 연 8,760개, 5분봉은 105,120개이며 `daily`는 기존 주식 기준 252개다.
샤프·소르티노의 목표 수익률과 마틴의 무위험 수익률은 기존 계산과 같은 0이다.
소르티노는 전략 봉 수익률의 평균을 `sqrt(mean(min(수익률, 0)^2))`로 나눈 뒤 연환산
계수의 제곱근을 곱한다. SOL 바이앤홀드 대비 초과수익률이 아닌 전략 자체의 수익률을 사용한다.
하방편차가 0이면 미정의 값이며, 소르티노를 목적으로 선택한 해당 후보는 제외한다.

`selection.metric: "ir"` 또는 `"air"`는 **같은 시장의 같은 평가 구간에 대한 1배 바이앤홀드 대비 정보비율**을
최대화한다. 시장은 config의 `market`에서 자동으로 정하므로 SOL·BTC 등의 벤치마크를 별도로
지정하지 않는다. 각 split의 첫 실행 봉 시가에 초기 자금을 전부 투자한 뒤 수량을 유지하고,
마지막 실행 봉 시가로 평가한다. 전략의 첫 진입일이나 전체 캐시의 첫 날짜를 시작점으로 쓰지 않는다.
벤치마크는 기존 보고서와 같은 **가격 기준 참조곡선**이며 수수료·슬리피지·펀딩은 포함하지 않는다.
따라서 비용과 펀딩을 반영한 1배 선물 계좌 시뮬레이션과는 다르다.

`air = mean(전략 봉 수익률 - 벤치마크 봉 수익률) / std(초과수익률, ddof=0) * sqrt(연환산 계수)`다.
`ir`는 이 식에서 연환산 계수의 제곱근을 곱하기 전 값이다. 동일한 봉 주기의 단독 목적함수에서는
두 지표의 후보 순위는 같지만 값의 크기가 다르므로, 다른 지표와 가중합할 때는 각 지표의 가중치를 별도로 설정해야 한다.
기존 `compute_information_ratio`를 재사용하며 양쪽 봉 인덱스는 정확히 일치해야 한다. 벤치마크의
첫 수익률은 0이고 전략은 초기 자금 대비 첫 봉의 비용·손익을 포함한다. 매번 해당 split 안에서
새로 시작하므로 훈련 점수에 검증·테스트 가격이 들어가지 않는다. 추가 백테스트나 데이터 다운로드는
필요 없다. 초과수익률의 표준편차가 0이면 AIR는 미정의이며, 점수로 선택한 후보는 탈락시킨다.
`metrics`에는 `ir`, `air`, `benchmark_market`, `benchmark_mode`, `benchmark_end_value`,
`benchmark_total_return_pct`를 함께 기록한다. AIR는 시기별 성과 균일성이나 미래 성과를 보장하는 지표는 아니다.

다음은 가중합의 **설정 형식 예시**이며, 특정 가중치를 권장하는 설정은 아니다.

```json
"selection": {
  "metric": "weighted_sum",
  "weights": {"cagr": 1.0, "ulcer_index": -0.5, "complexity": -0.1},
  "min_trades": 1,
  "max_drawdown_pct": null
}
```

위 점수는 `CAGR[%] - 0.5 * Ulcer Index[%] - 0.1 * complexity`다. 가중치의 합으로 나누거나
지표를 자동 표준화하지 않는다. 낮을수록 좋은 지표는 음수 가중치로 지정한다.

| 가중합 지표 이름 | 단위·의미 / 사용자 함수의 `metrics` 키 |
|---|---|
| `net_return`, `cagr` | 퍼센트 단위 / `net_return_pct`, `cagr_pct` |
| `sharpe`, `sortino`, `calmar`, `martin` | 비율 / 같은 이름의 키 |
| `ir`, `air` | 자동 바이앤홀드 대비 정보비율·연환산 정보비율 / 같은 이름의 키. 둘 다 단독 선택·가중합을 지원한다. |
| `max_drawdown`, `ulcer_index` | 양수 퍼센트 단위 / `max_drawdown_pct`, `ulcer_index_pct` |
| `complexity` | 아래에서 정의한 구조 크기 / `complexity` |
| `rule_count`, `atom_count`, `logical_count`, `holding_count` | 규칙·비교·AND/OR·보유 상태 수 / 같은 이름의 키 |
| `total_trades`, `liquidations` | 거래 수·강제청산 수 / 같은 이름의 키. 현물 엔진의 강제청산 수는 미정의다. |

`complexity = rule_count + atom_count + logical_count + holding_count`다. 진입과 보유형의 청산
조건을 모두 세고, 생성된 원래 전략 AST를 기준으로 한다. 자동 단순화나 중복 비교 제거는 하지
않는다. 피처의 숫자 파라미터 수를 세는 통계적 자유도나 실행 시간 지표는 아니다.
같은 주문 경로의 백테스트를 재사용하더라도 현재 후보의 복잡도는 다시 계산한다.

가중합은 적어도 하나의 0이 아닌 유한한 가중치를 요구한다. 가중치가 0인 항목은 계산에 사용하지
않는다. 선택한 지표가 미정의이거나 점수가 NaN/무한대이면 후보를 `rejected`/FAIL로 기록하고
다음 시도로 넘어간다. 예를 들어 낙폭이 0인 마틴·칼마를 무한대나 임의의 0점으로 대체하지 않는다.
선택에 사용하지 않은 미정의 지표는 JSON에서 null로 기록할 수 있다.

사용자 Python 함수는 기존 프로젝트 모듈 또는 import 가능한 모듈에 정의하고 경로로 등록한다.
아래 `my_scores`는 작성할 사용자 모듈의 예시 이름이며 기본 제공 파일이 아니다.

```json
"selection": {
  "metric": "custom",
  "function": "my_scores:score",
  "function_kwargs": {"ulcer_penalty": 0.5},
  "min_trades": 1,
  "max_drawdown_pct": null
}
```

```python
def score(context, ulcer_penalty=0.5):
    return context.metrics["cagr_pct"] - ulcer_penalty * context.metrics["ulcer_index_pct"]
```

`context`는 `lib.strategy_search_v2.evaluator.ObjectiveContext`이며 다음을 제공한다.

- `metrics`: 훈련 구간 성과와 현재 후보의 복잡도. 읽기 전용 mapping이다.
- `equity`: 훈련 구간 계좌 자산곡선의 복사본. 초기 자금 행을 덧붙이지 않은 실제 실행 봉 인덱스다.
- `returns`: 같은 인덱스의 봉별 수익률 복사본. 첫 값은 `equity.iloc[0] / initial_cash - 1`이다.
- `strategy`: 후보 전략 AST의 깊은 복사본.
- `initial_cash`, `annualization_factor`: 초기 자금과 기존 timeframe 기준 연환산 계수.

함수는 하나의 유한한 실수를 반환해야 한다. NaN/무한대 반환은 후보 탈락이며, 잘못된 반환형이나
함수의 일반 Python 예외는 오류를 기록하고 탐색을 중단한다. 전략별 탈락을 명시하려면
`CandidateRejected`를 발생시킬 수 있다. 검증·테스트·replay에서는 사용자 점수 함수를 호출하지 않는다.
사용자 함수는 전체 곡선을 받을 수 있도록 주문 경로가 같아도 백테스트를 새로 평가한다.
기본 지표·가중합은 기존 주문 경로 캐시를 유지한다.

`--check`도 사용자 모듈을 import하고 함수 인자 형식을 검사하지만 점수 함수를 실행하지는 않는다.
함수가 정의된 소스 파일과 등록 모듈 파일은 실험 해시에 자동 포함하며, 별도 helper나 외부 설정
파일은 `function_files`에 명시한다. 상대 경로는 프로젝트 루트 기준이다. 외부 의존성을 재귀적으로
추적하지 않으므로 별도 파일·패키지·외부 데이터에 의존하는 함수의 재현성은 추가 관리가 필요하다.
사용자 함수는 일반 Python 코드로 실행된다. 훈련 자료만 전달하지만 함수 내부의 외부 자료 접근까지
차단하지는 않는다. 주어진 context와 고정 인자만으로 결정적으로 계산하고, 함수를 수정하면 Python을
다시 실행해 새 `--out-dir`에서 시작한다. 평가함수 설정·가중치·사용자 소스 변경은 기존 실험 재개를 거부한다.

탐색 로그와 `best.json`에는 목적함수 이름·점수와 실제 순수익을 각각 기록한다. 검증·테스트는 같은 전략을
계속 평가하며 그 성과를 탐색 점수로 되돌려 보내지 않는다. 시기별 안정성이나 K-ratio를 자동 계산하는
내장 목적함수는 이번 선택 기능에 포함하지 않았으며, 사용자 함수에서 훈련 곡선으로 정의할 수 있다.

### 재료와 숫자 범위

`space.materials`는 재료 ID별 기존 FeatureSpec 템플릿, 숫자 범위, 비교 기준을 등록한다.
각 재료는 `parameters`, `features`, `output`, `operators`, `threshold`를 가진다.

```json
{
  "parameters": {"window": {"type": "int", "low": 6, "high": 240, "step": 6}},
  "features": [{
    "source": "trade_price",
    "steps": [{"kind": "simple_return", "params": {"window": "{window}"}}],
    "column_name": "{prefix}_value"
  }],
  "output": "{prefix}_value",
  "operators": ["gt", "lt"],
  "threshold": {"type": "float", "low": -0.1, "high": 0.1, "step": 0.005}
}
```

- `{prefix}`는 재료별 이름 충돌을 막는 접두사다. 컬럼 이름은 `{prefix}_`로 시작한다.
- 템플릿 값 전체가 `{window}`이면 정수 자료형을 보존한다. 임의 Python 표현식/eval은 실행하지 않는다.
- 숫자 도메인은 `int`, `float`, `categorical`을 지원한다. `step` 없는 float는 연속값,
  `choices`는 지정 목록, `log: true`는 양수 로그 범위다. 상한은 간격 격자에 맞아야 한다.
- 같은 후보 안에서 같은 재료의 파라미터는 공유한다. 동일 종류를 서로 다른 파라미터로 쓰려면
  별도 재료 ID로 등록한다. 실제로 선택된 재료만 샘플링·계산한다.
- 선택적으로 `constraints: [{"left":"fast","operator":"lt","right":"slow"}]`처럼
  파라미터 두 개를 비교할 수 있다. 정수 파라미터 두 개와 `left < right` 조건 하나만 있는 재료는
  left를 먼저 뽑고 right의 하한을 left보다 큰 원래 격자값으로 올려서 제안한다.
  right를 뽑을 자리가 남도록 left의 상한도 원래 격자 안에서 제한한다.
  유효한 쌍이 전혀 없으면 탐색 전 설정 검사에서 거부한다.
  다른 형식의 제약은 기존처럼 샘플링 후 검사하고 불만족 후보를 기록·탈락시킨다.
- SOL 예제 재료는 SMA 비율, EMA 비율, VWMA 비율, 과거 수익률, 평활 수익률, 실현 변동성,
  상대 거래량의 7종이다. RSI는 포함하지 않는다. 상대 거래량의 평균은
  현재 봉을 제외한 직전 봉들로 계산한다. `lib/strategy_search_v2/space.py`의 인과적 연산
  등록 목록에 있는 기존 v2 변환만 허용한다. custom/미래 라벨·음수 shift·미검토 변환은 거부한다.

| SOL 재료 | 탐색 파라미터와 비교 기준 |
|---|---|
| `sma_ratio`, `ema_ratio`, `vwma_ratio` | 각 종류의 단기/장기 비율. 단기 5~240봉(간격 1), 장기 20~1440봉(간격 5), 단기 < 장기. 비율 기준 0.975~1.025(간격 0.0025). |
| `past_return` | 2~1440봉 수익률(간격 2), 기준 -0.10~0.10(간격 0.005). |
| `smoothed_return` | 2~1440봉 수익률(간격 2)을 EMA 2~48봉(간격 2)으로 평활. 기준은 과거 수익률과 동일. |
| `realized_vol` | 6~240봉(간격 6), 1봉 수익률의 표준편차. 기준 0.002~0.05(간격 0.002). |
| `relative_volume` | 직전 6~240봉(간격 6)의 평균 대비 현재 거래량. 기준 0.5~3배(간격 0.1). |

EMA는 `ewma` 연산으로 계산하며 별도 지표 두 개로 중복 등록하지 않는다. VWMA는 종가와
`candle_acc_trade_volume`(기초자산 거래량)을 사용한다. SMA·EMA·VWMA의 기간은 종류별로
독립적으로 샘플링하며, 같은 종류를 사용하는 규칙끼리는 그 후보의 기간을 공유한다.
각 종류 안에서는 단기를 먼저 뽑고, 장기는 그보다 긴 5봉 격자에서 뽑는다. 예를 들어 단기가
53봉이면 장기 하한은 55봉, 단기가 125봉이면 장기 하한은 130봉이다. 기간을 뽑은 뒤 정렬하거나
덮어쓰지 않으며 Optuna가 기록한 값으로 그대로 계산한다. 단기/장기 기간 역전으로 인한 후보 탈락을
방지하고, 원래 허용한 유효 기간 쌍과 숫자 간격은 유지한다. Optuna 패키지 내부를 수정하지 않는다.
이평 재료는 각각 두 기간의 비율이며 단·중·장 정배열을 강제하지 않는다.
평활 수익률의 최대 수익률 기간 1440봉과 평활 기간 48봉을 포함해 공통 예열은 1488봉이고,
모든 후보를 그 이후 신호 지연까지 반영한 같은 평가 구간에서 비교한다.

### 조립 문법, 비중, 상태

`space.max_rules`는 순서가 있는 비중 규칙 수, `max_atoms`는 진입/청산 조건 하나에 들어갈
원자 비교 수 상한, `max_depth`는 AND/OR 결합 깊이 상한이다. `logical`에서 AND/OR 허용 여부를
정한다. 각 규칙의 방향과 크기는 `directions`, `exposure.long`, `exposure.short`에서 선택한다.

규칙은 **위에서부터 첫 활성 규칙 하나**가 적용되고 그 외는 현금이다. 여러 규칙이 겹쳐도
비중을 더하지 않는다. 따라서 강한 조건의 규칙을 앞에 놓으면 +3배, 그 다음 기본 조건에서
+1배처럼 단계별 규모를 표현할 수 있다. 규모는 `max_exposure` 이하여야 한다.
`change_only`로 목표가 바뀔 때 주문하므로 같은 목표에서는 수량을 유지하며, 실제 시가 평가
노출은 가격 변동에 따라 달라질 수 있다. 목표 비중 상한이 매 봉 실제 노출을 강제로 고정하지는 않는다.

`state_modes`는 `none`과 `holding`을 허용한다. `none`은 현재 조건, `holding`은 기존
StateSpec의 진입/청산과 `cooldown_bars`를 사용한다. **각 규칙의 신호 상태**를 따로 계산한 뒤
우선순위를 적용하므로, 낮은 순위의 보유 신호는 상위 규칙에 가려졌다가 다시 적용될 수 있다.
`cooldown_bars`는 음이 아닌 정수 `int` 도메인 또는 실제 정수로만 이루어진 `categorical` 목록을
받는다. `float` 도메인과 `2.0` 같은 소수형 목록 값은 사전 검사에서 거부한다.
이 상태는 실제 체결·청산 여부를 기억하는 계좌 상태가 아니다. 진입 당시 규모 기억이나
고정 N봉 보유, PnL 의존 청산을 새로 구현한 것은 아니다. 필요한 경우 범용 기능으로 별도 검토한다.
새로 생성하는 `holding` 규칙은 **청산 조건이 참인 동안 새 진입을 금지**한다. 원래 진입 조건을
`진입 AND NOT(청산)`으로 감싼 피처를 기존 StateSpec에 전달한다. 보유 중에는 원래 청산
조건을 적용하며, 청산한 같은 봉에 다시 진입하지 않고 이후 지정된 유효 봉 수만큼 기다린다.
청산 조건이 거짓으로 바뀌고 대기가 끝나면 원래 진입 조건이 계속 참이어도 재진입할 수 있다.
진입 신호가 거짓이 된 이력이나 별도의 최소 보유 시간을 요구하지 않는다.

이 정책은 탐색 파라미터가 아니며, 생성된 AST의 각 `holding` 규칙에 `exit_blocks_entry: true`로
기록한다. 컴파일 결과에 AND/NOT 피처를 추가하되 탐색 조건 수·깊이와 복잡도는 원래 AST로 센다.
이 표시가 없는 예전 후보는 기존 동작을 보존한다. 예전 후보에서 진입·청산이 동시에 참이면
미보유 상태에서는 진입하고 보유 상태에서는 청산한다. 새 표시가 있는 후보의 `replay`와
내보낸 v2 설정은 새 정책을 재현한다. 공통 StateSpec·기존 수동 백테스트·라이브의 동작은 바꾸지 않는다.
정책은 규칙별로 적용하므로 다른 규칙으로의 전환, 봉마다 조건이 바뀌며 생기는 잦은 매매까지
막지는 않는다.

### 탐색 후보의 논리 검사

새 탐색 후보는 구조 검사 후, 피처 계산·백테스트 전에 각 진입 조건과 `holding` 청산 조건이
논리적으로 성립 가능한지 검사한다. 동일 피처의 `gt/ge/lt/le` 경계를 AND로 교차하고,
OR의 가능한 갈래를 검사한다. `x > 3 AND x <= 3`은 탈락하고,
`x >= 3 AND x <= 3`은 값 3에서 가능하므로 허용한다. OR 한 갈래가 모순이어도 다른 갈래가
가능하면 전체 조건은 성립 가능하지만, 불필요한 갈래는 아래 중복 검사에서 제외한다.

`holding` 규칙에는 추가 탐색 정책을 적용한다. 동일 시점에서 `진입 AND NOT(청산)`이 가능한
상태가 하나도 없으면, 진입이 청산을 반드시 함의하므로 `rule.<번호>.entry_implies_exit`로
제외한다. 예를 들어 진입 `x < -0.08`, 청산 `x < 0.055`는 제외한다. AND/OR를 포함한
청산 조건의 부정은 드모르간 법칙과 비교 연산자의 여집합으로 검사하며, `>`와 `>=` 등
경계값 포함 여부까지 확인한다. 진입 `x >= 3`, 청산 `x > 3`은 값 3에서 진입만 가능하므로
허용한다. 진입·청산이 일부 겹치는 규칙도 허용한다.

새 후보는 청산 조건으로 진입을 차단하므로 위 함의가 성립하는 규칙은 진입할 수 없다.
이 사전 검사를 유지해 거래하지 않는 규칙의 불필요한 평가를 피한다.
`cooldown_bars` 값과 관계없이 적용하고 `none` 규칙에는 적용하지 않는다.
동일 시점의 함의만 검사하므로 다음 봉의 청산이나 모든 비중 왕복을 막는다고 보장하지 않는다.
서로 다른 피처 이름은 독립 실수 변수로 취급하며, 피처 간 수학적 관계나 관측 데이터는 사용하지 않는다.

성립 가능성과 보유 규칙 검사 후에는 진입·청산 조건 안의 **논리적으로 불필요한 항**을 검사한다.
같은 AND 묶음에서 나머지 항들이 어떤 항을 함의하거나, 같은 OR 묶음에서 어떤 항이 이미
나머지 대안에 포함되면 `Redundant condition: rule.<번호>.entry/exit...`로 후보를 제외한다.
예를 들어 `x > 0.02 AND x > 0.004`, `x > 0.02 OR x > 0.004`, `A AND A`는 제외하지만,
`x > 0.004 AND x < 0.02`처럼 양쪽 경계가 필요한 범위 조건은 허용한다. `>`와 `>=` 등
경계값 포함 여부까지 확인하며, 중첩된 같은 연산자는 함께 검사한다. AND와 OR를 서로 섞어
평탄화하지 않는다. `A AND (A OR B)` 같은 포함 관계도 기존 구간 논리 검사로 증명되면 제외한다.
다른 이름의 피처는 독립 변수로 취급하므로 지표 간 상관관계나 비슷한 값이라는 이유로 제외하지 않는다.

모순·위 보유 규칙·중복 중 하나라도 증명된 후보는 `rejected`/FAIL로 기록하고 다음 시도로 넘어간다.
원래 전략·샘플링 파라미터·실패 위치(`rule.<번호>.entry/exit` 및 하위 위치)를 보존하고 점수는 만들지 않는다.
제외된 후보는 전체 생성 시도 상한에 포함하지만, `evaluations` 방식의 유효 평가 예산에서는 뺀다.
같은 trial 안에서 몰래 재추첨하지 않고 FAIL로 확정한 뒤 다음 trial을 생성한다.
검사는 가격 이력을 읽지 않는다. 과거 미발생 조건, 서로 다른 피처 사이의 수학적 관계,
상위 규칙이 하위 규칙을 가리는지 여부는 이 검사로 판정하지 않는다. 조건을 자동 삭제·변형하지 않는다.

추가 config 키 없이 새 탐색에 적용한다. 수동 v2 전략의 컴파일·백테스트와 저장된 후보를 받는
`replay`는 이 추가 검사로 차단하지 않는다. 기존 실험 기록은 유지하며, 코드 해시가 달라진
새 탐색은 기존 정책대로 새 `--out-dir`를 사용한다.

### 평가 기간과 표본 외 검증

모든 후보는 **허용된 모든 재료의 최대 lookback + 신호 지연 이후의 동일 기간**에서 평가한다.
첫 거래가 늦다는 이유로 앞의 현금 기간을 제외하지 않는다. 데이터 공백을 메우거나 유효한
평가 구간의 미정의 신호를 0으로 바꾸지 않는다. 계산에는 해당 split 마지막 봉 이후 자료를
전달하지 않는다. 순수익은 초기 자금 대비 최종 자산으로 계산하고, 낙폭에는 초기 자금도 포함한다.

탐색이 끝나고 설정을 고정한 뒤 검증한다.

두 비율은 각각 0보다 크고 1보다 작아야 하며 합은 1 이하여야 한다. 합이 1이면
훈련·평가 두 구간만 생성한다. 부동소수점 합산 오차는 절대오차 `1e-12`까지 허용한다.
이때 훈련 봉 수는 내림하고 남은 모든 봉을 평가에 포함하므로 끝부분을 버리거나 한두 봉짜리
test를 만들지 않는다. 합이 1보다 작으면 기존처럼 세 구간을 생성하며, 실제 생성되는 각 구간은
예열·기간 제한 후 최소 3봉이 있어야 한다.

훈련 60%·평가 40%로 나누는 설정 예시는 다음과 같다.

```json
"validation": {"train_fraction": 0.6, "validation_fraction": 0.4},
"selection": {"metric": "sortino", "min_trades": 30, "max_drawdown_pct": 50}
```

평가 40%는 기존 명령 이름 `validate`와 결과 이름 `validation.json`을 사용한다.

```powershell
py scripts/research/search_strategies_v2.py --config-json $s --phase validate
# 세 구간 설정에서 별도의 마지막 test가 있을 때만 실행
py scripts/research/search_strategies_v2.py --config-json $s --phase test
```

`validate`는 학습 성과만으로 선택한 후보를 **평가 전에 고정**하고 탐색 재개를 잠근다.
두 구간 설정에서 `test`를 요청하면 별도 test가 없다는 오류를 내며, 그 요청으로 평가를 열거나
탐색을 잠그지 않는다. 세 구간 설정의 `test`도 같은 후보 하나만 평가하며, 검증 성과로 다시 후보를 고르지 않는다. 재실행은 저장된
결과를 반환한다. 각 구간은 새 자금·새 계좌로 시작하고, 신호 상태와 수치 피처는 과거 봉으로
예열할 수 있다. 구간별 곡선을 연속 실거래 계좌 결과로 합산해 해석하지 않는다.

이 첫 구현은 단일 시계열 분리 검증이다. **GP, 탐색을 구간마다 다시 수행하는 nested WF,
자동 생성 후보를 받는 PBO/SPA 연결은 아직 추가하지 않았다.** 기존 정적 grid의 WF/PBO/SPA를
전체 데이터에서 발견한 후보에 적용했다고 탐색 편향이 자동 해소되는 것은 아니다.
여러 실험에서 같은 검증 구간을 반복 참고하면 그 구간도 연구에 사용한 것으로 취급한다.

### 기록, 재현, 오류

`out_dir`에는 `manifest.json`(설정·데이터 해시·코드 해시·패키지·공통 기간), `study.sqlite3`,
`trials/<번호>.json`, `trials_summary.csv`, `best.json`, `best_v2/*.json`을 저장한다.
평가된 후보 JSON에는 `constraints`와 `feasible`을, CSV에는 `feasible`과
`constraint_min_trades`·`constraint_max_drawdown_pct` 중 활성화된 제약 열을 저장한다.
`feasible`은 수치 제약 충족 여부이며, 최종 선택에는 유효한 점수와 COMPLETE 상태도 필요하다.
`best.json`에도 선택한 후보의 제약값을 보존한다.
동일한 학습 주문 경로는 백테스트를 재사용하되 원래 전략 표현은 각각 보존한다.
중단된 trial은 재시작 시 interrupted/FAIL로 남기며, 오류 점수를 수익 0으로 대체하지 않는다.
0으로 나누기 등 후보의 계산 결과가 유효 구간에서 NaN/무한대가 되면 이유를 기록하고
rejected/FAIL로 탈락시킨 뒤 다음 후보를 탐색한다. 선물 엔진이 청산·잔액 대사를 마친
실제 계좌 소진(`FuturesBankruptcyError`)은 `bankrupt`/FAIL로 기록하고 다음 후보를 탐색한다.
파산 시각과 자산은 `bankruptcy`에 보존하며 수익률을 임의의 0이나 -100으로 채점하지 않는다.
검증·최종 테스트·개별 백테스트에서는 이 예외를 그대로 알리고 정상 성과로 저장하지 않는다.
여기서 FAIL은 Optuna의 학습 대상에서 제외하는 상태다. 후보 JSON의 `rejected`·`bankrupt`는
예상된 후보 탈락이므로 탐색을 계속하고, `failed`는 데이터·엔진·사용자 함수 등의 오류로 실행을 멈춘다.
점수 없는 PRUNED 후보는 Optuna 내부에서 제약 위반 COMPLETE보다 좋은 후보 묶음에 먼저 들어갈 수
있으므로 사용하지 않는다. 정상 평가된 `infeasible`은 실제 점수·제약값을 가진 COMPLETE로 유지한다.
TPE의 `startup_trials`는 FAIL을 제외한 점수 있는 후보가 쌓일 때까지 기다린다. `evaluations` 방식의
유효 평가 예산에는 FAIL을 넣지 않으며 생성 상한에만 포함한다.
기존 `attempts` 방식의 예산에는 FAIL도 포함한다. 전부 무효 후보이면 랜덤 제안 단계에 머무를 수 있다.
데이터 누락·불완전한 계좌 곡선·엔진 오류는 상세 기록 후 실행을 멈춘다.
완료 결과 저장 이후의 출력 오류나 DB 커밋 응답 오류로
이미 완료된 trial을 실패로 다시 기록하지 않는다. 출력 오류는 실행을 중단할 수 있지만 저장된
완료 결과는 유지되며, 재실행하면 그다음 시도부터 이어간다. 프로세스 OS lock으로 동일
실험에 대한 동시 쓰기를 막으며 SQLite 연결은 실행 종료 때 닫는다. 재개 시 유효 평가 수는
SQLite의 유한한 점수가 있는 COMPLETE 기록에서 다시 세므로 커밋 후 출력 오류나 응답 유실이
있어도 완료 결과를 중복 집계하지 않는다. 로그에는 평가 수와 전체 생성 수를 각각 표시하고,
후보 JSON·CSV의 `evaluation_number`에는 유효 평가 순번을 저장한다. SQLite study의
`search_budget` 속성에는 마지막 정상 종료 시 목표·평가 수·생성 수·상한·종료 이유를 저장한다.

`best_v2`는 기존 FeatureSpec/UniverseSpec/WeightSpec 형식이다. 동일 기간·계좌 초기화 조건까지
맞춰 학습 결과를 재현하려면 아래 명령을 사용한다. 기존 runner에서 전체 데이터로 돌린 결과는
이 탐색의 학습 구간 결과와 기간이 다르므로 직접 비교하지 않는다.

```powershell
py scripts/research/search_strategies_v2.py --config-json $s --phase replay --candidate-json data/research/strategy_search_v2/sol_1h_tpe_sortino_train60_eval40_dd50_scored_v2/best.json
```

데이터와 생성 결과는 기존 `/data/` Git 제외 정책을 따른다. 기존 수동 전략·백테스트·라이브
실행 명령은 유지한다. 선물 공통 연결부의 비용·청산 수정은 기존 선물 그리드·검증에도 적용된다.
코드가 바뀐 이전 탐색 결과는 새 결과와 섞어 이어가지 않고 새 `--out-dir`에서 실행한다.
자동으로 실제 주문이나 공개 저장소 업로드를 수행하지 않는다.
