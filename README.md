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
| **CAGR 37.56% · 최대낙폭 -57.79%**<br>2016-01-04 ~ 2026-09-21 | **CAGR 21.50% · 최대낙폭 -21.82%**<br>2012-05-01 ~ 2026-05-29 |
| [국내 주식 상세 보기](notes/strategy_examples.md#strategy-kr-stock) | [미국 ETF 상세 보기](notes/strategy_examples.md#strategy-us-etf) |

<br>

| BTC 현물 · 이동평균·변동성 필터 | BTC 제외 USDT 무기한 선물 · 그룹별 롱숏 |
| --- | --- |
| [![BTC 현물 성과 그래프](notes/assets/btc_spot_case.png)](notes/strategy_examples.md#strategy-btc-spot) | [![선물 롱숏 성과 그래프](notes/assets/alt_futures_case_preview.png)](notes/strategy_examples.md#strategy-alt-futures) |
| **CAGR 80.03% · 최대낙폭 -35.78%**<br>2019-09-25 ~ 2026-10-27 | **누적수익률 1위: CAGR 40.88% · 최대낙폭 -35.05%**<br>2020-01-01 ~ 2026-10-27 |
| [BTC 현물 상세 보기](notes/strategy_examples.md#strategy-btc-spot) | [선물 롱숏 상세 보기](notes/strategy_examples.md#strategy-alt-futures) |

그래프 또는 ‘상세 보기’를 누르면 상세 보고서의 해당 전략으로 이동합니다. 선물 미리보기는 전체 기간 누적수익률 기준 상위 조합을 보여줍니다.

## 더 보기

| 문서 | 설명 |
| --- | --- |
| [기능 목록](notes/feature_inventory.md) | 구현된 연구·검증·라이브 기능 |
| [설정 파일 레퍼런스](notes/config_reference_v2.md) | 전략 설정 옵션 |
| [4개 전략 요약](notes/strategy_summary.md) | 4개 전략의 신호·비중·성과·검증 결과를 한눈에 비교 |
| [전략별 상세 보고서](notes/strategy_examples.md) | 전략 명세·성과·SPA/PBO·워크포워드·72개 선물 조합 비교 |
