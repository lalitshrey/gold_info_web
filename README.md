# Gold Market info

A Python dashboard for precious-metals prices. It displays COMEX gold and
silver converted to INR, USD/INR, and directly fetched MCX gold, silver, and
crude oil contract quotes, plus Nippon India Gold BeES (NSE: GOLDBEES) and
Silver BeES (NSE: SILVERBEES), in a browser-based FastAPI app.

## Setup

Python 3.10 or later is recommended.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run

### Browser dashboard (FastAPI)

```bash
python fastapi_app.py
```

Open http://127.0.0.1:8000/ in your browser. The page shows COMEX gold/silver,
USD/INR, MCX gold, MCX silver, MCX crude oil, separate NSE and Zerodha BSE
prices for Gold BeES and Silver BeES, stock/market headlines, VADER sentiment
for headlines mentioning NSE-listed companies, and a gold outlook based on
recent/longer-term COMEX gold futures momentum and cited fundamental research.
The gold outlook is not a forecast or investment advice.
Use **Refresh prices** to fetch updated quotes. The JSON endpoints are
`GET /api/prices`, `POST /api/refresh`, `GET /api/news`, and
`GET /api/gold-outlook`.

Alternatively, run the app with Uvicorn:

```bash
uvicorn fastapi_app:app --host 127.0.0.1 --port 8000
```

### Terminal dashboard

```bash
python precious_metals_dashboard.py
```

## Data sources

- COMEX gold and silver: [comexlive.org](https://comexlive.org/)
- MCX gold: Moneycontrol, with an Economic Times fallback when Moneycontrol
  blocks the request or the quote cannot be parsed.
- MCX silver: [Economic Times](https://economictimes.indiatimes.com/commoditysummary/symbol-SILVER.cms).
- MCX crude oil: [Economic Times](https://economictimes.indiatimes.com/commoditysummary/symbol-CRUDEOIL.cms).
- Nippon India ETF Gold BeES (NSE symbol `GOLDBEES`): Yahoo Finance chart API.
- Zerodha BSE Gold BeES: Zerodha's public
  [GOLDBEES ETF page](https://zerodha.com/markets/etf/BSE/GOLDBEES/) and its
  public BSE intraday quote endpoint.
- Nippon India Silver BeES (NSE symbol `SILVERBEES`): Yahoo Finance chart API.
- Zerodha BSE Silver BeES: Zerodha's public
  [SILVERBEES ETF page](https://zerodha.com/markets/etf/BSE/SILVERBEES/) and
  its public BSE intraday quote endpoint.
- Stock and market headlines: RSS sources configured in
  `stock_news_agent/providers/rss_provider.py`.
- Headline sentiment: VADER. The first request downloads NLTK's
  `vader_lexicon` if it is not already available. Sentiment is a text signal,
  not investment advice.
- Gold momentum: daily COMEX gold futures (`GC=F`) closes from Yahoo Finance;
  the dashboard compares five trading sessions, one month, and approximately
  one year. This is historical momentum, not a price forecast.
- Long-term gold demand research: World Gold Council's Q3 2026
  [Gold Demand Trends](https://www.gold.org/goldhub/research/gold-demand-trends/gold-demand-trends-q3-2026).
- USD/INR: Moneycontrol, with a dated reference-rate fallback when unavailable.

The MCX gold, silver, and crude oil quotes are futures contract prices. Crude
oil's dollar value is converted from INR per barrel using the displayed
USD/INR rate; it is not a WTI or Brent benchmark quote. The COMEX prices are
separate international quotes converted using USD/INR; they are not the same
market prices.

The gold converter applies 15% import duty and 3% GST. The silver converter
applies 6% import duty and 3% GST.

## Main modules

- `precious_metals_dashboard.py` — dashboard orchestration and display.
- `fastapi_app.py` — FastAPI application and JSON price endpoints.
- `templates/metals_dashboard.html` — browser dashboard UI.
- `get_gold_silver_comexlive.py` — COMEX quote fetch and parsing.
- `get_goldbees_quote.py` — Yahoo Finance and Zerodha public BSE quote fetchers
  for Gold BeES and Silver BeES.
- `get_gold_silver_moneycontrol.py` — MCX gold and silver quotes, plus the
  Moneycontrol silver quote used by the terminal dashboard.
- `get_usdinr_moneycontrol.py` — USD/INR fetch and fallback.
- `gold_price_converter.py`, `silver_price_converter.py` — currency, weight,
  duty, and GST calculations.
