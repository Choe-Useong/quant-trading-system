# 전략별 백테스트

## 국내 주식 · 업종 내 순위 모멘텀

<table width="100%">
  <tr>
    <td width="71%" valign="top">
      <a href="assets/kr_stock_case.png"><img src="assets/kr_stock_case.png" alt="국내 주식 전략과 KOSPI200 백테스트 수익률 및 낙폭" width="1100"></a>
    </td>
    <td width="29%" valign="top">
      <strong>전체 기간</strong><br>2016.01–2026.09<br><br>
      <strong>전략 성과</strong><br>CAGR <strong>37.61%</strong><br>MDD <strong>-57.79%</strong><br>Sharpe <strong>1.22</strong><br><br>
      <strong>KOSPI200</strong><br>CAGR 16.09%<br>MDD -41.19%
    </td>
  </tr>
  <tr>
    <td colspan="2" valign="top">
      <strong>전략 구성</strong>
      <ul>
        <li><strong>유니버스</strong> — KOSPI 보통주 · 전월 평균 시가총액 상위 100개 · 전월 기준 업종 분류</li>
        <li><strong>신호·필터</strong> — 업종 내 일별 수익률 백분위(업종 3종목 이상) → 월평균 → 최근 3개월 제외한 9개월 합산</li>
        <li><strong>종목·비중</strong> — 점수 상위 5개 · 업종당 최대 2개 · 동일비중 각 20%</li>
        <li><strong>재조정·비용</strong> — 월별 · 수수료 0.15% · 슬리피지 0.10%</li>
      </ul>
    </td>
  </tr>
  <tr>
    <td colspan="2" valign="top">
      <strong>검증 결과</strong>
      <ul>
        <li><strong>후보·통계검증</strong> — 모멘텀 9·12·15·18개월 × 최근 제외 0·1·2·3개월 = 16개 · SPA p=0.0246 · PBO 19.44%</li>
        <li><strong>워크포워드</strong> — 2018.01–2026.01 · Rolling CAGR 18.13% / MDD -56.56% · Expanding CAGR 21.18% / MDD -50.31%</li>
        <li><strong>같은 기간 비교</strong> — KOSPI200 CAGR 8.52% · MDD -41.05%</li>
      </ul>
    </td>
  </tr>
</table>

---

## 미국 ETF · 상승월·추세 지속성

<table width="100%">
  <tr>
    <td width="71%" valign="top">
      <a href="assets/us_etf_case.png"><img src="assets/us_etf_case.png" alt="미국 ETF 전략과 원화 환산 SSO 백테스트 수익률 및 낙폭" width="1100"></a>
    </td>
    <td width="29%" valign="top">
      <strong>전체 기간</strong><br>2012.05–2026.05<br><br>
      <strong>전략 성과</strong><br>CAGR <strong>36.16%</strong><br>MDD <strong>-32.15%</strong><br>Sharpe <strong>1.14</strong><br><br>
      <strong>원화 환산 SSO</strong><br>CAGR 26.63%<br>MDD -56.74%
    </td>
  </tr>
  <tr>
    <td colspan="2" valign="top">
      <strong>전략 구성</strong>
      <ul>
        <li><strong>유니버스</strong> — 미국 상장 1배·2배 ETF 33개 · 국가·산업·자산군별 고정 목록</li>
        <li><strong>순위 신호</strong> — 12개월 중 최근 1개월 제외한 11개월의 일별 횡단 순위 월평균 합산</li>
        <li><strong>매수 필터</strong> — 126거래일 수익률 &gt; 0 · 최근 20개월 중 상승월 ≥ 11개 · 252일 상승일 비율 하위 10% 제외 · 10일 변동성의 자체 252일 백분위 ≤ 97.5%</li>
        <li><strong>종목·비중</strong> — 통과 종목 중 상위 최대 2개 · 2개면 각 50%, 1개면 100%</li>
        <li><strong>재조정·비용</strong> — 종목 변경 시 · 수수료 0.05% · 슬리피지 0%</li>
      </ul>
    </td>
  </tr>
  <tr>
    <td colspan="2" valign="top">
      <strong>검증 결과</strong>
      <ul>
        <li><strong>후보·통계검증</strong> — 상승월 기준 9개 · SPA p=0.2508 · PBO 10.00%</li>
        <li><strong>워크포워드</strong> — 2014.05–2026.04 · CAGR 32.86% · MDD -32.15%</li>
        <li><strong>같은 기간 비교</strong> — 원화 환산 SSO CAGR 24.94% · MDD -56.74%</li>
      </ul>
    </td>
  </tr>
</table>

---

## BTC 현물 · 이동평균·변동성

<table width="100%">
  <tr>
    <td width="71%" valign="top">
      <a href="assets/btc_spot_case.png"><img src="assets/btc_spot_case.png" alt="BTC 현물 전략과 단순 보유 백테스트 수익률 및 낙폭" width="1100"></a>
    </td>
    <td width="29%" valign="top">
      <strong>전체 기간</strong><br>2019.09–2026.10<br><br>
      <strong>전략 성과</strong><br>CAGR <strong>80.03%</strong><br>MDD <strong>-35.78%</strong><br>Sharpe <strong>1.91</strong><br><br>
      <strong>BTC 단순 보유</strong><br>CAGR 38.90%<br>MDD -74.85%
    </td>
  </tr>
  <tr>
    <td colspan="2" valign="top">
      <strong>전략 구성</strong>
      <ul>
        <li><strong>유니버스</strong> — KRW-BTC 현물 1종목 · 1시간봉</li>
        <li><strong>신호·필터</strong> — 10시간 SMA &gt; 120시간 SMA · 최근 240시간 로그수익률 변동성이 최근 17,520시간(약 2년) 자기 분포의 95백분위 이하</li>
        <li><strong>비중·체결</strong> — 두 조건 충족 시 BTC 100%, 그 외 현금 100% · 신호 계산 다음 시간봉 시가 적용</li>
        <li><strong>재조정·비용</strong> — 매시간 · 수수료 0.05% · 슬리피지 0%</li>
      </ul>
    </td>
  </tr>
  <tr>
    <td colspan="2" valign="top">
      <strong>검증 결과</strong>
      <ul>
        <li><strong>후보·통계검증</strong> — 단기 SMA 3 × 장기 SMA 3 × 변동성 제한 3 = 27개 · SPA p=0.2627 · PBO 32.99%</li>
        <li><strong>워크포워드</strong> — 2021.09–2026.09 · CAGR 35.56% · MDD -36.28%</li>
        <li><strong>같은 기간 비교</strong> — BTC 보유 CAGR 17.47% · MDD -74.85%</li>
      </ul>
    </td>
  </tr>
</table>

---

## 알트코인 선물 · 거래대금×수익률 롱·숏

<table width="100%">
  <tr>
    <td width="71%" valign="top">
      <a href="assets/alt_futures_case_preview.png"><img src="assets/alt_futures_case_preview.png" alt="알트코인 선물 롱숏 상위 조합 백테스트 수익률 및 낙폭" width="1100"></a>
    </td>
    <td width="29%" valign="top">
      <strong>전체 기간</strong><br>2020.01–2026.10<br>전체 기간 누적수익률 1위 조합<br><br>
      <strong>전략 성과</strong><br>CAGR <strong>40.88%</strong><br>MDD <strong>-35.05%</strong><br>Sharpe <strong>1.32</strong><br><br>
      <strong>BTC 가격 보유</strong><br>CAGR 42.55%<br>MDD -77.24%
    </td>
  </tr>
  <tr>
    <td colspan="2" valign="top">
      <strong>전략 구성</strong>
      <ul>
        <li><strong>유니버스</strong> — BTC 제외 바이낸스 USDT 무기한 선물 · 시점별 계약·주문 규칙 및 거래 데이터 조건 충족 종목</li>
        <li><strong>그룹 신호</strong> — 직전 완료된 7일 일봉의 USDT 거래대금과 종가 수익률을 각각 3분위 → 교차 9개 그룹</li>
        <li><strong>조합 선정</strong> — 서로 다른 롱·숏 그룹 72개 비교 · 1위: 롱 거래대금 하위/수익률 하위, 숏 거래대금 상위/수익률 하위</li>
        <li><strong>비중·재조정</strong> — 롱 50% + 숏 50% · 방향별 동일비중 · 매주 월요일 00:00 UTC</li>
        <li><strong>비용·위험</strong> — 수수료 0.05% · 슬리피지 0.02% · 펀딩·강제청산 반영</li>
      </ul>
    </td>
  </tr>
  <tr>
    <td colspan="2" valign="top">
      <strong>조합 평가</strong>
      <ul>
        <li><strong>비교 대상</strong> — 서로 다른 9개 그룹의 롱 9 × 숏 8 = 72개</li>
        <li><strong>조합 평가</strong> — 72개 중 누적수익률 양수 22개 · 전체 기간 누적수익률 순위로 상위 조합 선정</li>
        <li><strong>성과 분포</strong> — <a href="assets/alt_ls9_72_cagr_heatmap.png">72개 조합 CAGR 히트맵</a></li>
      </ul>
    </td>
  </tr>
</table>

[상세 보고서](strategy_examples.md)
