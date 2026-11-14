"""Historical gold-price momentum used by the dashboard outlook."""

from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any

import httpx


YAHOO_GOLD_HISTORY_URL = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
YAHOO_GOLD_HISTORY_PAGE = "https://finance.yahoo.com/quote/GC=F/history/"


def _period_result(
    closes: list[float],
    *,
    sessions: int,
    label: str,
) -> dict[str, Any]:
    start_index = max(0, len(closes) - 1 - sessions)
    start_price = closes[start_index]
    latest_price = closes[-1]
    change_pct = (latest_price / start_price - 1) * 100
    direction = "up" if change_pct > 0 else "down" if change_pct < 0 else "flat"
    return {
        "label": label,
        "sessions": len(closes) - 1 - start_index,
        "start_close_usd_oz": start_price,
        "latest_close_usd_oz": latest_price,
        "change_pct": change_pct,
        "direction": direction,
    }


def fetch_gold_momentum() -> dict[str, Any]:
    """Fetch one year of daily COMEX gold futures closes and calculate momentum."""
    try:
        response = httpx.get(
            YAHOO_GOLD_HISTORY_URL,
            params={"range": "1y", "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20.0,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
        raise RuntimeError(f"Could not retrieve Yahoo Finance gold history: {error}") from error

    observations = [
        (timestamp, float(close))
        for timestamp, close in zip(timestamps, closes)
        if close is not None and isfinite(float(close)) and float(close) > 0
    ]
    if len(observations) < 6:
        raise RuntimeError(
            f"Yahoo Finance returned only {len(observations)} valid gold closes; "
            "at least six are required."
        )

    latest_timestamp, latest_close = observations[-1]
    close_values = [close for _, close in observations]
    short_term = _period_result(
        close_values,
        sessions=5,
        label="5 trading sessions",
    )
    one_month = _period_result(
        close_values,
        sessions=21,
        label="1 month (21 trading sessions)",
    )
    long_term = (
        _period_result(
            close_values,
            sessions=252,
            label="1 year",
        )
        if len(close_values) >= 200
        else None
    )

    return {
        "instrument": "COMEX Gold Futures (GC=F)",
        "latest_close_usd_oz": latest_close,
        "as_of": datetime.fromtimestamp(
            latest_timestamp,
            tz=timezone.utc,
        ).isoformat(),
        "short_term": short_term,
        "one_month": one_month,
        "long_term": long_term,
        "source": "Yahoo Finance",
        "source_url": YAHOO_GOLD_HISTORY_PAGE,
    }
