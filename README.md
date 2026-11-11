# 퀀트 리서치 & 자동매매 시스템

전략 아이디어를 데이터 수집, 신호 계산, 검증, 목표 비중 산출, 실계좌 자동매매까지 하나의 흐름으로 연결한 퀀트 트레이딩 시스템입니다.

> **공개 범위**
>
> 시스템 구조와 전략 연구·백테스트 결과를 정리한 포트폴리오입니다. 실제 운용 설정, 계좌 정보, 원천 데이터 및 주문·체결 기록은 포함하지 않습니다.

**백테스트:** [4개 전략 요약](notes/strategy_summary.md) · [전략 명세·성과·검증 상세](notes/strategy_examples.md)

**시스템:** [기능 목록](notes/feature_inventory.md) · [설정 레퍼런스](notes/config_reference_v2.md)

## 전체 구조

![퀀트 트레이딩 시스템 아키텍처](notes/assets/system_architecture.png)

## 백테스트 사례 미리보기

| 국내 주식 · 업종 내 순위 모멘텀 | 미국 ETF · 1배 ETF 순위 모멘텀 |
| --- | --- |
| [![국내 주식 성과 그래프](notes/assets/kr_stock_case.png)](notes/strategy_examples.md#strategy-kr-stock) | [![미국 ETF 성과 그래프](notes/assets/us_etf_case.png)](notes/strategy_examples.md#strategy-us-etf) |
| **CAGR 37.61% · 최대낙폭 -57.79%**<br>2016-01-04 ~ 2026-09-21 | **CAGR 21.52% · 최대낙폭 -21.82%**<br>2012-05-01 ~ 2026-05-29 |
| [국내 주식 상세 보기](notes/strategy_examples.md#strategy-kr-stock) | [미국 ETF 상세 보기](notes/strategy_examples.md#strategy-us-etf) |

<br>

| BTC 현물 · 이동평균·변동성 필터 | BTC 제외 USDT 무기한 선물 · 그룹별 롱숏 |
| --- | --- |
| [![BTC 현물 성과 그래프](notes/assets/btc_spot_case.png)](notes/strategy_examples.md#strategy-btc-spot) | [![선물 롱숏 성과 그래프](notes/assets/alt_futures_case_preview.png)](notes/strategy_examples.md#strategy-alt-futures) |
| **CAGR 80.03% · 최대낙폭 -35.78%**<br>2019-09-25 ~ 2026-10-27 | **누적수익률 1위: CAGR 40.88% · 최대낙폭 -35.05%**<br>2020-01-01 ~ 2026-10-27 |
| [BTC 현물 상세 보기](notes/strategy_examples.md#strategy-btc-spot) | [선물 롱숏 상세 보기](notes/strategy_examples.md#strategy-alt-futures) |

그래프 또는 ‘상세 보기’를 누르면 상세 보고서의 해당 전략으로 이동합니다. 선물 미리보기는 전체 기간 누적수익률 기준 상위 조합을 보여줍니다.

## Walk-Forward 표본 외 성과

아래 표는 **Walk-Forward 표본 외(OOS) 평가 결과**입니다. Rolling은 학습 구간을 이동하고, Expanding은 학습 시작 시점을 고정한 채 자료를 누적합니다. 전체 기간 국내 주식·미국 ETF CAGR은 일봉 연 252개 관측치, BTC 현물은 1시간봉 연 8,760개 관측치로 연환산했습니다. 반면 Walk-Forward CAGR은 각 OOS 평가기간의 실제 달력일 수를 사용했습니다. 따라서 두 결과를 비교할 때는 평가기간과 계산 방식의 차이를 고려해야 합니다. 세부 설정은 [전략별 상세 보고서](notes/strategy_examples.md)를 참고하세요.

| 전략 | OOS 기간 | Rolling CAGR / MDD | Expanding CAGR / MDD | 동일기간 비교 대상 CAGR |
| --- | --- | ---: | ---: | ---: |
| 국내 주식 | 2018-01-04 ~ 2026-01-02 | 18.13% / -56.56% | **21.18% / -50.31%** | KOSPI200 8.52% |
| 미국 ETF | 2014-05-01 ~ 2026-04-30 | 23.82% / -23.55% | **19.09% / -24.82%** | 원화 환산 SPY 17.17% |
| BTC 현물 | 2021-09-25 ~ 2026-09-25 | 35.56% / -36.28% | **40.75% / -36.28%** | BTC 보유 17.47% |

선물 롱·숏은 72개 조합의 전체 기간 비교이며, 이 표의 Walk-Forward 검증 대상에 포함하지 않았습니다.

## 시스템 구성

| 구성 요소 | 역할 |
| --- | --- |
| 데이터 준비 | 시장 가격, 거래량, 거래대금, 환율, 종목 메타데이터를 수집하고 전략 계산용 캐시로 정리 |
| 전략 설정 | 설정 파일로 유니버스, 신호, 필터, 비중 산출 방식을 정의 |
| 신호·비중 계산 | 전략 설정을 데이터에 적용해 후보 자산과 목표 비중 생성 |
| 후보 전략 검증 | 그리드 탐색, walk-forward, PBO, SPA, StepM, 낙폭과 회복기간 분석으로 후보 전략 점검 |
| 매매 계획 생성 | 최신 목표 비중과 실제 계좌 보유를 비교해 주문 수량 계산 |
| 주문 실행 | preview 또는 live 모드로 주문 제출과 체결 확인 처리 |
| 성과·계좌 기록 | 실행 결과와 계좌 스냅샷을 저장하고 주요 이벤트를 알림으로 전달 |

## 더 보기

| 문서 | 설명 |
| --- | --- |
| [기능 목록](notes/feature_inventory.md) | 구현된 연구·검증·라이브 기능 |
| [설정 파일 레퍼런스](notes/config_reference_v2.md) | 전략 설정 옵션 |
| [4개 전략 요약](notes/strategy_summary.md) | 4개 전략의 신호·비중·성과·검증 결과를 한눈에 비교 |
| [전략별 상세 보고서](notes/strategy_examples.md) | 전략 명세·성과·SPA/PBO·워크포워드·72개 선물 조합 비교 |
