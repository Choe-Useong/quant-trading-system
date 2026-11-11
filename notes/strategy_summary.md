# 전략별 백테스트

**연환산 기준:** 국내 주식·미국 ETF의 CAGR은 일봉 연 252개, BTC 현물은 1시간봉 연 8,760개 수익률 관측치 기준이다. 전체 기간 성과와 Walk-Forward OOS는 동일한 연환산 기준을 사용하지만, 평가기간과 후보 선택 방식은 다르다. 자세한 검증 조건과 결과는 [전략별 상세 보고서](strategy_examples.md)를 참고한다.

## 국내 주식 · 업종 내 순위 모멘텀

<table width="100%">
  <tr>
    <td colspan="4" width="70%" valign="top"><a href="assets/kr_stock_case.png"><img src="assets/kr_stock_case.png" alt="국내 주식 백테스트 성과 및 낙폭" width="1100"></a></td>
    <td colspan="2" width="30%" valign="top"><strong>전체 기간</strong><br>2016.01–2026.09<br><br><strong>전략 성과</strong><br>CAGR <strong>37.56%</strong><br>MDD <strong>-57.79%</strong><br>Sharpe <strong>1.22</strong><br><br><strong>KOSPI200</strong><br>CAGR 16.09%<br>MDD -41.19%</td>
  </tr>
  <tr><th colspan="6" align="left">01 / 전략 구성</th></tr>
  <tr>
    <td colspan="3" width="50%" valign="top"><strong>유니버스</strong><br>KOSPI 보통주<br>전월 평균 시가총액 상위 100개</td>
    <td colspan="3" width="50%" valign="top"><strong>업종 기준</strong><br>전월 말 업종 분류<br>업종 내 순위 계산 시 최소 3종목</td>
  </tr>
  <tr>
    <td colspan="3" width="50%" valign="top"><strong>점수 계산</strong><br>일별 수익률 업종 내 백분위 → 월평균</td>
    <td colspan="3" width="50%" valign="top"><strong>모멘텀 기간</strong><br>12개월 중 최근 3개월 제외<br>과거 9개월 월평균 점수 합산</td>
  </tr>
  <tr>
    <td colspan="3" width="50%" valign="top"><strong>종목 선정</strong><br>점수 상위 5개<br>업종당 최대 2개</td>
    <td colspan="3" width="50%" valign="top"><strong>비중·재조정</strong><br>종목별 20% 동일비중<br>월별 재조정</td>
  </tr>
  <tr>
    <td colspan="3" width="50%" valign="top"><strong>거래비용</strong><br>수수료 0.15% · 슬리피지 0.10%</td>
    <td colspan="3" width="50%" valign="top"><strong>실행 제약</strong><br>거래량 없는 날 주문 연기<br>상장폐지 시 마지막 유효가 정산</td>
  </tr>
  <tr><th colspan="6" align="left">02 / 통계 검증</th></tr>
  <tr>
    <td colspan="2" width="33%" align="center" valign="top"><strong>후보군</strong><br>모멘텀 9·12·15·18개월<br>최근 제외 0·1·2·3개월 (16개)</td>
    <td colspan="2" width="33%" align="center" valign="top"><strong>SPA p값</strong><br><strong>0.0246</strong></td>
    <td colspan="2" width="33%" align="center" valign="top"><strong>PBO</strong><br><strong>19.44%</strong></td>
  </tr>
  <tr><th colspan="6" align="left">Walk-Forward · 2018.01–2026.01</th></tr>
  <tr>
    <td colspan="3" width="50%" valign="top"><strong>Rolling</strong><br>CAGR <strong>23.10%</strong><br>MDD -53.33%</td>
    <td colspan="3" width="50%" valign="top"><strong>Expanding</strong><br>CAGR <strong>26.67%</strong><br>MDD -48.16%</td>
  </tr>
  <tr><td colspan="6" valign="top"><strong>동일기간 KOSPI200</strong><br>CAGR 8.77%<br>MDD -41.05%</td></tr>
</table>

---

## 미국 ETF · 1배 ETF 순위 모멘텀

<table width="100%">
  <tr>
    <td colspan="4" width="70%" valign="top"><a href="assets/us_etf_case.png"><img src="assets/us_etf_case.png" alt="미국 ETF 백테스트 성과 및 낙폭" width="1100"></a></td>
    <td colspan="2" width="30%" valign="top"><strong>전체 기간</strong><br>2012.05–2026.05<br><br><strong>전략 성과</strong><br>CAGR <strong>21.50%</strong><br>MDD <strong>-21.82%</strong><br>Sharpe <strong>1.19</strong><br><br><strong>원화 환산 SPY</strong><br>CAGR 17.08%<br>MDD -29.62%</td>
  </tr>
  <tr><th colspan="6" align="left">01 / 전략 구성</th></tr>
  <tr>
    <td colspan="3" width="50%" valign="top"><strong>유니버스</strong><br>미국 1배 ETF 17개<br>섹터·국가·금·장기채 고정 목록</td>
    <td colspan="3" width="50%" valign="top"><strong>순위 신호</strong><br>일별 ETF 간 수익률 순위 → 월평균<br>최근 12개월 중 1개월 제외한 11개월 합산</td>
  </tr>
  <tr>
    <td colspan="3" width="50%" valign="top"><strong>절대·상승월 조건</strong><br>126거래일 수익률 &gt; 0<br>최근 20개월 중 상승월 11개월 이상</td>
    <td colspan="3" width="50%" valign="top"><strong>추세·변동성 조건</strong><br>252일 상승일 비율 하위 10% 제외<br>10일 변동성의 자기 252일 백분위 ≤ 97.5%</td>
  </tr>
  <tr>
    <td colspan="3" width="50%" valign="top"><strong>종목·비중</strong><br>조건 충족 상위 최대 2개<br>2개면 각 50% · 1개면 100%</td>
    <td colspan="3" width="50%" valign="top"><strong>조정·비용</strong><br>종목 변경 시 재조정<br>수수료 0.05% · 슬리피지 0%</td>
  </tr>
  <tr><th colspan="6" align="left">02 / 통계 검증</th></tr>
  <tr>
    <td colspan="2" width="33%" align="center" valign="top"><strong>후보군</strong><br>모멘텀 · 절대 모멘텀 · 종목 수 (36개)</td>
    <td colspan="2" width="33%" align="center" valign="top"><strong>SPA p값</strong><br><strong>0.2302</strong></td>
    <td colspan="2" width="33%" align="center" valign="top"><strong>PBO</strong><br><strong>7.50%</strong></td>
  </tr>
  <tr><th colspan="6" align="left">Walk-Forward · 2014.05–2026.04</th></tr>
  <tr>
    <td colspan="3" width="50%" valign="top"><strong>Rolling</strong><br>CAGR <strong>23.84%</strong><br>MDD -23.55%</td>
    <td colspan="3" width="50%" valign="top"><strong>Expanding</strong><br>CAGR <strong>19.10%</strong><br>MDD -24.82%</td>
  </tr>
  <tr><td colspan="6" valign="top"><strong>동일기간 SPY</strong><br>CAGR 17.21%<br>MDD -29.62%</td></tr>
</table>

---

## BTC 현물 · 이동평균·변동성

<table width="100%">
  <tr>
    <td colspan="4" width="70%" valign="top"><a href="assets/btc_spot_case.png"><img src="assets/btc_spot_case.png" alt="BTC 현물 백테스트 성과 및 낙폭" width="1100"></a></td>
    <td colspan="2" width="30%" valign="top"><strong>전체 기간</strong><br>2019.09–2026.10<br><br><strong>전략 성과</strong><br>CAGR <strong>80.03%</strong><br>MDD <strong>-35.78%</strong><br>Sharpe <strong>1.91</strong><br><br><strong>BTC 단순 보유</strong><br>CAGR 38.90%<br>MDD -74.85%</td>
  </tr>
  <tr><th colspan="6" align="left">01 / 전략 구성</th></tr>
  <tr>
    <td colspan="3" width="50%" valign="top"><strong>유니버스</strong><br>KRW-BTC 현물 단일 자산<br>1시간봉</td>
    <td colspan="3" width="50%" valign="top"><strong>추세 조건</strong><br>SMA 10시간 &gt; SMA 120시간</td>
  </tr>
  <tr>
    <td colspan="3" width="50%" valign="top"><strong>변동성 측정</strong><br>최근 240시간 로그수익률 표준편차</td>
    <td colspan="3" width="50%" valign="top"><strong>변동성 제한</strong><br>최근 약 2년(17,520시간) 자기 분포<br>95백분위 이하</td>
  </tr>
  <tr>
    <td colspan="3" width="50%" valign="top"><strong>비중</strong><br>두 조건 충족: BTC 100%<br>그 외: 현금 100%</td>
    <td colspan="3" width="50%" valign="top"><strong>체결·비용</strong><br>매시간 재조정 · 다음 봉 시가 반영<br>수수료 0.05% · 슬리피지 0%</td>
  </tr>
  <tr><th colspan="6" align="left">02 / 통계 검증</th></tr>
  <tr>
    <td colspan="2" width="33%" align="center" valign="top"><strong>후보군</strong><br>단기 SMA 3 × 장기 SMA 3<br>변동성 제한 3 = 27개</td>
    <td colspan="2" width="33%" align="center" valign="top"><strong>SPA p값</strong><br><strong>0.2627</strong></td>
    <td colspan="2" width="33%" align="center" valign="top"><strong>PBO</strong><br><strong>32.99%</strong></td>
  </tr>
  <tr><th colspan="6" align="left">Walk-Forward · 2021.09–2026.09</th></tr>
  <tr>
    <td colspan="3" width="50%" valign="top"><strong>Rolling</strong><br>CAGR <strong>35.53%</strong><br>MDD -36.28%</td>
    <td colspan="3" width="50%" valign="top"><strong>Expanding</strong><br>CAGR <strong>40.72%</strong><br>MDD -36.28%</td>
  </tr>
  <tr><td colspan="6" valign="top"><strong>동일기간 BTC 보유</strong><br>CAGR 17.47%<br>MDD -74.85%</td></tr>
</table>

---

## 알트코인 선물 · 거래대금×수익률 롱·숏

<table width="100%">
  <tr>
    <td colspan="4" width="70%" valign="top"><a href="assets/alt_futures_case_preview.png"><img src="assets/alt_futures_case_preview.png" alt="알트코인 선물 롱숏 백테스트 성과 및 낙폭" width="1100"></a></td>
    <td colspan="2" width="30%" valign="top"><strong>전체 기간</strong><br>2020.01–2026.10<br>전체 기간 누적수익률 1위 조합<br><br><strong>전략 성과</strong><br>CAGR <strong>40.88%</strong><br>MDD <strong>-35.05%</strong><br>Sharpe <strong>1.32</strong><br><br><strong>BTC 가격 보유</strong><br>CAGR 42.55%<br>MDD -77.24%</td>
  </tr>
  <tr><th colspan="6" align="left">01 / 전략 구성</th></tr>
  <tr>
    <td colspan="3" width="50%" valign="top"><strong>유니버스</strong><br>BTC 제외 바이낸스 USDT 무기한 선물<br>시점별 계약·주문·데이터 조건 충족</td>
    <td colspan="3" width="50%" valign="top"><strong>거래대금 신호</strong><br>직전 완료된 UTC 일봉 7일<br>USDT 거래대금 합계</td>
  </tr>
  <tr>
    <td colspan="3" width="50%" valign="top"><strong>수익률 신호</strong><br>직전 완료된 일봉 종가 기준<br>7일 누적수익률</td>
    <td colspan="3" width="50%" valign="top"><strong>그룹 구성</strong><br>두 지표 각각 3분위<br>교차 9개 그룹</td>
  </tr>
  <tr>
    <td colspan="3" width="50%" valign="top"><strong>1위 롱 / 숏</strong><br>롱: 거래대금 하위·수익률 하위<br>숏: 거래대금 상위·수익률 하위</td>
    <td colspan="3" width="50%" valign="top"><strong>비중·재조정</strong><br>롱 50% / 숏 50% · 방향별 동일비중<br>매주 월요일 00:00 UTC</td>
  </tr>
  <tr>
    <td colspan="3" width="50%" valign="top"><strong>비용</strong><br>수수료 0.05% · 슬리피지 0.02%</td>
    <td colspan="3" width="50%" valign="top"><strong>위험 처리</strong><br>펀딩 · 강제청산 반영</td>
  </tr>
  <tr><th colspan="6" align="left">02 / 조합 비교</th></tr>
  <tr>
    <td colspan="2" width="33%" align="center" valign="top"><strong>비교 조합</strong><br>9 × 8 = <strong>72개</strong></td>
    <td colspan="2" width="33%" align="center" valign="top"><strong>누적수익률 양수</strong><br><strong>22개</strong></td>
    <td colspan="2" width="33%" align="center" valign="top"><strong>선정 기준</strong><br>전체 기간<br><strong>누적수익률 1위</strong></td>
  </tr>
  <tr><td colspan="6" valign="top"><strong>성과 분포</strong> · <a href="assets/alt_ls9_72_cagr_heatmap.png">72개 조합 CAGR 히트맵</a></td></tr>
</table>

[상세 보고서](strategy_examples.md)
