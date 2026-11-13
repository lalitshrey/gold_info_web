# Gold Info Web

A Python dashboard for precious-metals prices. It displays COMEX gold and
silver converted to INR, USD/INR, and directly fetched MCX gold, silver, and
crude oil contract quotes in a browser-based FastAPI app.

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
USD/INR, MCX gold, MCX silver, and MCX crude oil prices. Use **Refresh prices**
to fetch updated quotes.
The JSON endpoints are `GET /api/prices` and `POST /api/refresh`.

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
- `get_gold_silver_moneycontrol.py` — MCX gold and silver quotes, plus the
  Moneycontrol silver quote used by the terminal dashboard.
- `get_usdinr_moneycontrol.py` — USD/INR fetch and fallback.
- `gold_price_converter.py`, `silver_price_converter.py` — currency, weight,
  duty, and GST calculations.
