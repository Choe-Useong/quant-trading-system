# 전략별 백테스트

## 국내 주식 · 업종 내 순위 모멘텀

<table>
  <tr>
    <td width="58%" valign="top">
      <a href="assets/kr_stock_case.png"><img src="assets/kr_stock_case.png" alt="국내 주식 백테스트 성과와 낙폭" width="720"></a>
    </td>
    <td width="42%" valign="top">
      <table>
      <tr><th align="left" valign="top">대상</th><td valign="top">KOSPI 시가총액 상위 100개 보통주</td></tr>
      <tr><th align="left" valign="top">전략</th><td valign="top">업종 내 순위 모멘텀 12개월 · 최근 3개월 제외 · 상위 5개 · 업종당 최대 2개</td></tr>
      <tr><th align="left" valign="top">기간</th><td valign="top">2016.01–2026.09</td></tr>
      <tr><th align="left" valign="top">성과</th><td valign="top"><strong>CAGR 37.61% · MDD -57.79%</strong><br>Sharpe 1.22</td></tr>
      <tr><th align="left" valign="top">비교</th><td valign="top">KOSPI200 CAGR 16.09% · MDD -41.19%</td></tr>
      <tr><th align="left" valign="top">검증</th><td valign="top">SPA p=0.0246 · PBO 19.44% (16개 후보)</td></tr>
      <tr><th align="left" valign="top">WF</th><td valign="top">Rolling CAGR 18.13%<br>Expanding CAGR 21.18%<br>KOSPI200 CAGR 8.52%</td></tr>
      <tr><th align="left" valign="top">비용</th><td valign="top">수수료 0.15% · 슬리피지 0.10%</td></tr>
      </table>
    </td>
  </tr>
</table>

## 미국 ETF · 상승월·추세 지속성

<table>
  <tr>
    <td width="58%" valign="top">
      <a href="assets/us_etf_case.png"><img src="assets/us_etf_case.png" alt="미국 ETF 백테스트 성과와 낙폭" width="720"></a>
    </td>
    <td width="42%" valign="top">
      <table>
      <tr><th align="left" valign="top">대상</th><td valign="top">미국 1배·2배 ETF 33개</td></tr>
      <tr><th align="left" valign="top">전략</th><td valign="top">순위 모멘텀 · 상승월·추세 지속성·변동성 조건 · 최대 2개 동일비중</td></tr>
      <tr><th align="left" valign="top">기간</th><td valign="top">2012.05–2026.05</td></tr>
      <tr><th align="left" valign="top">성과</th><td valign="top"><strong>CAGR 36.16% · MDD -32.15%</strong><br>Sharpe 1.14</td></tr>
      <tr><th align="left" valign="top">비교</th><td valign="top">원화 환산 SSO CAGR 26.63% · MDD -56.74%</td></tr>
      <tr><th align="left" valign="top">검증</th><td valign="top">SPA p=0.2508 · PBO 10.00% (9개 후보)</td></tr>
      <tr><th align="left" valign="top">WF</th><td valign="top">CAGR 32.86%<br>SSO CAGR 24.94%</td></tr>
      <tr><th align="left" valign="top">비용</th><td valign="top">수수료 0.05% · 슬리피지 0%</td></tr>
      </table>
    </td>
  </tr>
</table>

## BTC 현물 · 이동평균·변동성

<table>
  <tr>
    <td width="58%" valign="top">
      <a href="assets/btc_spot_case.png"><img src="assets/btc_spot_case.png" alt="BTC 현물 백테스트 성과와 낙폭" width="720"></a>
    </td>
    <td width="42%" valign="top">
      <table>
      <tr><th align="left" valign="top">대상</th><td valign="top">KRW-BTC · 1시간봉</td></tr>
      <tr><th align="left" valign="top">전략</th><td valign="top">SMA 10시간 > 120시간 · 변동성 95백분위 제한 · BTC/현금 100% 전환</td></tr>
      <tr><th align="left" valign="top">기간</th><td valign="top">2019.09–2026.10</td></tr>
      <tr><th align="left" valign="top">성과</th><td valign="top"><strong>CAGR 80.03% · MDD -35.78%</strong><br>Sharpe 1.91</td></tr>
      <tr><th align="left" valign="top">비교</th><td valign="top">BTC 보유 CAGR 38.90% · MDD -74.85%</td></tr>
      <tr><th align="left" valign="top">검증</th><td valign="top">SPA p=0.2627 · PBO 32.99% (27개 후보)</td></tr>
      <tr><th align="left" valign="top">WF</th><td valign="top">CAGR 35.56%<br>BTC 보유 CAGR 17.47%</td></tr>
      <tr><th align="left" valign="top">비용</th><td valign="top">수수료 0.05% · 슬리피지 0%</td></tr>
      </table>
    </td>
  </tr>
</table>

## 알트코인 선물 · 거래대금×수익률 롱·숏

<table>
  <tr>
    <td width="58%" valign="top">
      <a href="assets/alt_futures_case_preview.png"><img src="assets/alt_futures_case_preview.png" alt="알트코인 선물 롱숏 조합 성과와 낙폭" width="720"></a>
    </td>
    <td width="42%" valign="top">
      <table>
      <tr><th align="left" valign="top">대상</th><td valign="top">BTC 제외 바이낸스 USDT 무기한 선물</td></tr>
      <tr><th align="left" valign="top">전략</th><td valign="top">7일 거래대금 × 7일 수익률 3분위 · 9개 그룹 · 롱/숏 72개 조합</td></tr>
      <tr><th align="left" valign="top">기간</th><td valign="top">2020.01–2026.10</td></tr>
      <tr><th align="left" valign="top">성과</th><td valign="top">전체 기간 누적수익률 1위 조합<br><strong>CAGR 40.88% · MDD -35.05%</strong><br>Sharpe 1.32</td></tr>
      <tr><th align="left" valign="top">비교</th><td valign="top">BTC 보유 CAGR 42.55% · MDD -77.24%</td></tr>
      <tr><th align="left" valign="top">검증</th><td valign="top">72개 조합 전체 기간 비교 · 양수 22개 · SPA/PBO/WF 미실시</td></tr>
      <tr><th align="left" valign="top">구성</th><td valign="top">롱 50% · 숏 50% · 주 1회 조정</td></tr>
      <tr><th align="left" valign="top">비용</th><td valign="top">수수료 0.05% · 슬리피지 0.02% · 펀딩·강제청산</td></tr>
      </table>
    </td>
  </tr>
</table>

[상세 보고서](strategy_examples.md)
