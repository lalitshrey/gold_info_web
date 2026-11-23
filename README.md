# AuricAI

A Python dashboard for precious-metals prices. It displays COMEX gold and
silver converted to INR, USD/INR, and directly fetched MCX gold, silver, and
crude oil contract quotes, plus Nippon India Gold BeES (NSE: GOLDBEES) and
Silver BeES (NSE: SILVERBEES), in a browser-based FastAPI app.

## Setup

Python 3.10 or later is recommended.
The browser UI uses Tailwind CSS. Install Node.js and npm, then build the local
stylesheet before starting the app:

```bash
npm install
npm run build:css
```

Install the Python dependencies separately:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## MySQL and accounts

AuricAI requires MySQL for account registration and saved news. Create a
database and a dedicated MySQL user with permission to create and update
tables, then configure the connection in a local `.env` file:

```bash
cp .env.example .env
```

Set `DATABASE_URL` to
`mysql+pymysql://USER:PASSWORD@HOST:3306/DATABASE`. URL-encode special
characters in the username or password. Set `SESSION_SECRET` to a unique
random value of at least 32 characters, for example:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

For local HTTP development, keep `SESSION_COOKIE_SECURE=false`. Set it to
`true` when serving the application over HTTPS. The `.env` file is ignored by
Git and must not be committed. On startup, AuricAI creates the `users`,
`news_links`, and `news_articles` tables if they do not exist. News URLs are
stored in `news_links`; each saved article references its URL row by a
foreign key. Use `/register` to create an account and `/login` to sign in;
the dashboard and its data APIs require authentication.
Registration collects first name, last name, mobile number, email, password,
and password confirmation. Existing user tables receive the profile columns
automatically at startup.

Password reset emails use SMTP. Configure `PUBLIC_BASE_URL`,
`SMTP_HOST`, `SMTP_PORT`, and `SMTP_FROM_EMAIL` in `.env`; add
`SMTP_USERNAME` and `SMTP_PASSWORD` when the mail server requires login. Use
`SMTP_STARTTLS=true` for STARTTLS (the default) or set it to `false` and
`SMTP_USE_SSL=true` for implicit TLS. Reset links expire after 30 minutes and
can only be used once. Configure a real HTTPS `PUBLIC_BASE_URL` in production.
The token is placed in the link fragment so it is not sent in the initial page
request or routine HTTP referrer headers.

To enable Google sign-in, create an OAuth 2.0 Web application client in Google
Cloud Console. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`, and
add `https://YOUR_HOST/auth/google/callback` (or
`http://127.0.0.1:8000/auth/google/callback` for local development) as an
authorized redirect URI. `PUBLIC_BASE_URL` must match the same origin. The
Google button is shown only on the sign-in page; Google accounts must have a
verified email address.

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
Stock sentiment cards show the average news-text tone for matched Indian
companies over 7-day, 30-day, and 90-day windows. Matching uses the bundled NSE
symbol list; BSE-only listings are not covered. Results depend on the date range
available from the RSS feeds, and sentiment is not a price forecast or
investment recommendation.
Use **Refresh prices** to fetch updated quotes. The JSON endpoints are
`GET /api/prices`, `POST /api/refresh`, `GET /api/news`, and
`GET /api/gold-outlook`, and `GET /api/gold-news`. The gold outlook also shows
recent gold-market headlines with sentiment labels; these reflect headline
text tone and are not price forecasts or investment advice.
The stock and market news panel also includes a separate stream of recent
conflict and geopolitical headlines related to gold, stocks, oil, or markets.
Choose **Indian stock sentiment** from the dashboard's **Sections** menu to open
the standalone sentiment view in the current tab.
The stock page also shows the latest available NSE price and indicative
30-trading-day stop-loss and target levels, calculated with Wilder ATR-14
(stop-loss at 2×ATR below price; target at twice the risk, or 1:2
risk/reward). These educational estimates may use delayed prices and are not
investment advice; gaps, costs, and slippage can change actual results.
Recommendations are saved once per signed-in user, stock, and India calendar
date. Later completed daily candles update each open recommendation as target
hit, stop-loss hit, both hit on the same day (order unknown), still open, or
expired after 30 sessions. Tracking updates when the sentiment page checks
prices, including for stocks no longer present in recent headlines; daily
candles cannot determine intraday order or guarantee execution.

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
  `vader_lexicon` if it is not already available. Stock-level summaries use
  dated articles in nested 7-, 30-, and 90-day windows and the bundled NSE
  symbol list; BSE-only listings are not covered. Sentiment is a text signal,
  not investment advice.
- Gold momentum: daily COMEX gold futures (`GC=F`) closes from Yahoo Finance;
  the dashboard compares five trading sessions, one month, and approximately
  one year. This is historical momentum, not a price forecast.
- Long-term gold demand research: World Gold Council's Q3 2026
  [Gold Demand Trends](https://www.gold.org/goldhub/research/gold-demand-trends/gold-demand-trends-q3-2026).
  The dashboard includes an attributed summary and balanced conclusion from
  this report, published 3 November 2026 with data through 30 September 2026.
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
- `database.py` — MySQL account and normalized news/article persistence.
- `templates/metals_dashboard.html` — browser dashboard UI.
- `templates/auth.html` — email sign-in and registration UI.
- `get_gold_silver_comexlive.py` — COMEX quote fetch and parsing.
- `get_goldbees_quote.py` — Yahoo Finance and Zerodha public BSE quote fetchers
  for Gold BeES and Silver BeES.
- `get_gold_silver_moneycontrol.py` — MCX gold and silver quotes, plus the
  Moneycontrol silver quote used by the terminal dashboard.
- `get_usdinr_moneycontrol.py` — USD/INR fetch and fallback.
- `gold_price_converter.py`, `silver_price_converter.py` — currency, weight,
  duty, and GST calculations.
