"""Fetch Yahoo Finance and Zerodha quotes for Nippon India Gold and Silver BeES."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any

import httpx


@dataclass
class ETFQuote:
    """Market quote for a precious-metal ETF."""

    last: float
    change: float | None
    change_pct: float | None
    prev_close: float | None
    open_: float | None
    high: float | None
    low: float | None
    last_trade: str | None
    source: str = "Yahoo Finance · NSE"


GoldBeESQuote = ETFQuote


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _positive_number(value: Any) -> float | None:
    number = _finite_number(value)
    return number if number is not None and number > 0 else None


def _fetch_yahoo_etf_quote(symbol: str, name: str) -> ETFQuote:
    try:
        response = httpx.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS",
            params={"range": "1d", "interval": "1m"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15.0,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload["chart"]["result"][0]
        meta = result["meta"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
        raise RuntimeError(f"Could not retrieve NSE {name} quote: {error}") from error

    last = _positive_number(meta.get("regularMarketPrice"))
    if last is None:
        raise RuntimeError(f"Yahoo Finance did not return a valid {name} market price.")

    previous_close = (
        _positive_number(meta.get("chartPreviousClose"))
        or _positive_number(meta.get("previousClose"))
    )
    change = last - previous_close if previous_close is not None else None
    change_pct = (
        (change / previous_close * 100)
        if change is not None and previous_close is not None
        else None
    )

    timestamps = result.get("timestamp") or []
    last_trade = None
    if timestamps:
        last_trade = datetime.fromtimestamp(
            timestamps[-1],
            tz=timezone.utc,
        ).isoformat()

    indicators = result.get("indicators", {}).get("quote", [{}])[0]

    def last_valid(key: str) -> float | None:
        values = indicators.get(key) or []
        return next(
            (
                number
                for value in reversed(values)
                if (number := _positive_number(value)) is not None
            ),
            None,
        )

    return ETFQuote(
        last=last,
        change=change,
        change_pct=change_pct,
        prev_close=previous_close,
        open_=last_valid("open"),
        high=last_valid("high"),
        low=last_valid("low"),
        last_trade=last_trade,
    )


def fetch_goldbees_quote() -> ETFQuote:
    """Fetch the latest NSE Gold BeES quote and its previous close."""
    return _fetch_yahoo_etf_quote("GOLDBEES", "Gold BeES")


def fetch_silverbees_quote() -> ETFQuote:
    """Fetch the latest NSE Silver BeES quote and its previous close."""
    return _fetch_yahoo_etf_quote("SILVERBEES", "Silver BeES")


def _fetch_zerodha_bse_etf_quote(symbol: str, name: str) -> ETFQuote:
    page_url = f"https://zerodha.com/markets/etf/BSE/{symbol}/"
    try:
        page_response = httpx.get(
            page_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15.0,
        )
        page_response.raise_for_status()
        match = re.search(
            r'<script id="etf-id" type="application/json">\s*(\d+)\s*</script>',
            page_response.text,
        )
        if match is None:
            raise RuntimeError(f"Could not find {name}'s ETF ID on Zerodha's page.")
        intraday_url = (
            "https://zerodha.com/markets/etf/intraday/BSE/"
            f"{match.group(1)}/"
        )
        quote_response = httpx.get(
            intraday_url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": page_url},
            timeout=15.0,
        )
        quote_response.raise_for_status()
        payload = quote_response.json()
    except (httpx.HTTPError, ValueError, TypeError) as error:
        raise RuntimeError(
            f"Could not retrieve Zerodha BSE {name} quote: {error}"
        ) from error

    if not isinstance(payload, dict):
        raise RuntimeError(f"Zerodha returned an invalid BSE {name} response.")

    last = _positive_number(payload.get("ltp_to_show_bse"))
    if last is None:
        raise RuntimeError(
            f"Zerodha did not return a valid BSE {name} market price."
        )

    trade_time = payload.get("updated_on")
    if trade_time:
        try:
            parsed_trade_time = datetime.fromisoformat(
                str(trade_time).replace("Z", "+00:00")
            )
            last_trade = parsed_trade_time.isoformat()
        except ValueError:
            last_trade = str(trade_time)
    else:
        last_trade = None

    return ETFQuote(
        last=last,
        change=_finite_number(payload.get("change_to_show_bse")),
        change_pct=_finite_number(payload.get("per_change_to_show_bse")),
        prev_close=None,
        open_=None,
        high=None,
        low=None,
        last_trade=last_trade,
        source="Zerodha · BSE",
    )


def fetch_zerodha_bse_goldbees_quote() -> ETFQuote:
    """Fetch the BSE Gold BeES price displayed on Zerodha's public market page."""
    return _fetch_zerodha_bse_etf_quote("GOLDBEES", "Gold BeES")


def fetch_zerodha_bse_silverbees_quote() -> ETFQuote:
    """Fetch the BSE Silver BeES price displayed on Zerodha's public market page."""
    return _fetch_zerodha_bse_etf_quote("SILVERBEES", "Silver BeES")
