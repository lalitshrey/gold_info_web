# Gold Info Web

A Python dashboard for precious-metals prices. It displays COMEX gold and
silver converted to INR, USD/INR, and a directly fetched MCX gold contract
quote in a terminal dashboard.

## Setup

Python 3.10 or later is recommended.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run

```bash
python precious_metals_dashboard.py
```

## Data sources

- COMEX gold and silver: [comexlive.org](https://comexlive.org/)
- MCX gold: Moneycontrol, with an Economic Times fallback when Moneycontrol
  blocks the request or the quote cannot be parsed.
- USD/INR: Moneycontrol, with a dated reference-rate fallback when unavailable.

The MCX gold quote is a futures contract price. The COMEX prices are separate
international quotes converted using USD/INR; they are not the same market
price.

The gold converter applies 15% import duty and 3% GST. The silver converter
applies 6% import duty and 3% GST.

## Main modules

- `precious_metals_dashboard.py` — dashboard orchestration and display.
- `get_gold_silver_comexlive.py` — COMEX quote fetch and parsing.
- `get_gold_silver_moneycontrol.py` — MCX gold and Moneycontrol silver quotes.
- `get_usdinr_moneycontrol.py` — USD/INR fetch and fallback.
- `gold_price_converter.py`, `silver_price_converter.py` — currency, weight,
  duty, and GST calculations.
