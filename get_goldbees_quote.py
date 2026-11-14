"""Fetch the NSE quote for Nippon India ETF Gold BeES."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any

import httpx


YAHOO_GOLDBEES_URL = "https://query1.finance.yahoo.com/v8/finance/chart/GOLDBEES.NS"
ZERODHA_GOLDBEES_PAGE_URL = "https://zerodha.com/markets/etf/BSE/GOLDBEES/"
ZERODHA_GOLDBEES_INTRADAY_URL = "https://zerodha.com/markets/etf/intraday/BSE/{instrument_id}/"


@dataclass
class GoldBeESQuote:
    """Market quote for Nippon India ETF Gold BeES."""

    last: float
    change: float | None
    change_pct: float | None
    prev_close: float | None
    open_: float | None
    high: float | None
    low: float | None
    last_trade: str | None
    source: str = "Yahoo Finance · NSE"


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


def fetch_goldbees_quote() -> GoldBeESQuote:
    """Fetch the latest NSE Gold BeES quote and its previous close."""
    try:
        response = httpx.get(
            YAHOO_GOLDBEES_URL,
            params={"range": "1d", "interval": "1m"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15.0,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload["chart"]["result"][0]
        meta = result["meta"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
        raise RuntimeError(f"Could not retrieve NSE Gold BeES quote: {error}") from error

    last = _positive_number(meta.get("regularMarketPrice"))
    if last is None:
        raise RuntimeError("Yahoo Finance did not return a valid Gold BeES market price.")

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

    return GoldBeESQuote(
        last=last,
        change=change,
        change_pct=change_pct,
        prev_close=previous_close,
        open_=last_valid("open"),
        high=last_valid("high"),
        low=last_valid("low"),
        last_trade=last_trade,
    )


def fetch_zerodha_bse_goldbees_quote() -> GoldBeESQuote:
    """Fetch the BSE Gold BeES price displayed on Zerodha's public market page."""
    try:
        page_response = httpx.get(
            ZERODHA_GOLDBEES_PAGE_URL,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15.0,
        )
        page_response.raise_for_status()
        match = re.search(
            r'<script id="etf-id" type="application/json">\s*(\d+)\s*</script>',
            page_response.text,
        )
        if match is None:
            raise RuntimeError("Could not find the Gold BeES ETF ID on Zerodha's page.")
        intraday_url = ZERODHA_GOLDBEES_INTRADAY_URL.format(
            instrument_id=match.group(1)
        )
        quote_response = httpx.get(
            intraday_url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": ZERODHA_GOLDBEES_PAGE_URL},
            timeout=15.0,
        )
        quote_response.raise_for_status()
        payload = quote_response.json()
    except (httpx.HTTPError, ValueError, TypeError) as error:
        raise RuntimeError(
            f"Could not retrieve Zerodha BSE Gold BeES quote: {error}"
        ) from error

    if not isinstance(payload, dict):
        raise RuntimeError("Zerodha returned an invalid BSE Gold BeES response.")

    last = _positive_number(payload.get("ltp_to_show_bse"))
    if last is None:
        raise RuntimeError(
            "Zerodha did not return a valid BSE Gold BeES market price."
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

    return GoldBeESQuote(
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
