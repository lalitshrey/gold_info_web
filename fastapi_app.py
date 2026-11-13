#!/usr/bin/env python3
"""FastAPI browser dashboard for precious-metals prices."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from get_gold_silver_comexlive import get_gold_silver
from get_gold_silver_moneycontrol import (
    get_mcx_crude_oil_price,
    get_gold_price as get_mcx_gold,
    get_mcx_silver_price,
)
from get_usdinr_moneycontrol import get_usdinr
from gold_price_converter import (
    add_duty_and_gst as add_gold_duty_and_gst,
    convert_gold_price_usd_oz_to_inr_10g,
)
from silver_price_converter import (
    add_duty_and_gst as add_silver_duty_and_gst,
    convert_silver_price_usd_oz_to_inr_kg,
)


_BASE_DIR = Path(__file__).resolve().parent
_DASHBOARD_FILE = _BASE_DIR / "templates" / "metals_dashboard.html"
_CACHE_SECONDS = 60
_logger = logging.getLogger(__name__)
_cache: dict[str, Any] | None = None
_cache_updated_at: datetime | None = None
_refresh_lock = asyncio.Lock()

app = FastAPI(
    title="Gold Info Web",
    description="COMEX and MCX commodity prices, and USD/INR.",
    version="1.0.0",
)


def _quote_dict(quote: Any) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "last": quote.last,
            "change": quote.change,
            "change_pct": quote.change_pct,
            "close": getattr(quote, "close", None),
            "high": quote.high,
            "low": quote.low,
            "open": getattr(quote, "open_", None),
            "prev_close": getattr(quote, "prev_close", None),
            "last_trade": getattr(quote, "last_trade", None),
            "source": quote.source,
        }.items()
        if value is not None
    }


def _collect_prices() -> dict[str, Any]:
    """Fetch quotes independently and calculate COMEX-derived INR values."""
    errors: dict[str, str] = {}
    comex: Any = None
    usdinr: Any = None
    mcx_gold: Any = None
    mcx_silver: Any = None
    mcx_crude_oil: Any = None

    fetchers = {
        "comex": get_gold_silver,
        "usd_inr": get_usdinr,
        "mcx_gold": get_mcx_gold,
        "mcx_silver": get_mcx_silver_price,
        "mcx_crude_oil": get_mcx_crude_oil_price,
    }
    with ThreadPoolExecutor(max_workers=len(fetchers)) as executor:
        futures = {
            name: executor.submit(fetcher)
            for name, fetcher in fetchers.items()
        }
        results: dict[str, Any] = {}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as error:
                _logger.warning("Could not fetch %s: %s", name, error)
                errors[name] = str(error)

    comex = results.get("comex")
    usdinr = results.get("usd_inr")
    mcx_gold = results.get("mcx_gold")
    mcx_silver = results.get("mcx_silver")
    mcx_crude_oil = results.get("mcx_crude_oil")

    gold_data = None
    silver_data = None
    if comex is not None:
        gold, silver = comex
        gold_data = _quote_dict(gold)
        silver_data = _quote_dict(silver)
        if usdinr is not None and usdinr.rate > 0:
            gold_inr = convert_gold_price_usd_oz_to_inr_10g(
                gold.last,
                usdinr.rate,
                use_troy=True,
            )
            silver_inr = convert_silver_price_usd_oz_to_inr_kg(
                silver.last,
                usdinr.rate,
                use_troy=True,
            )
            gold_data["inr_10g"] = gold_inr
            gold_data["after_tax_inr_10g"] = add_gold_duty_and_gst(gold_inr)
            silver_data["inr_kg"] = silver_inr
            silver_data["after_tax_inr_kg"] = add_silver_duty_and_gst(silver_inr)

    fx_data = None
    if usdinr is not None:
        fx_data = {
            "rate": usdinr.rate,
            "source": usdinr.source,
            "as_of": (usdinr.raw or {}).get("as_of"),
        }

    mcx_data = _quote_dict(mcx_gold) if mcx_gold is not None else None
    if mcx_data is not None:
        mcx_data["unit"] = "INR per 10g"
        mcx_data["expiry"] = (mcx_gold.raw or {}).get("expiry")

    mcx_silver_data = _quote_dict(mcx_silver) if mcx_silver is not None else None
    if mcx_silver_data is not None:
        mcx_silver_data["unit"] = "INR per kg"
        mcx_silver_data["expiry"] = (mcx_silver.raw or {}).get("expiry")

    mcx_crude_oil_data = (
        _quote_dict(mcx_crude_oil) if mcx_crude_oil is not None else None
    )
    if mcx_crude_oil_data is not None:
        mcx_crude_oil_data["unit"] = "INR per barrel"
        mcx_crude_oil_data["expiry"] = (mcx_crude_oil.raw or {}).get("expiry")
        if usdinr is not None and usdinr.rate > 0:
            mcx_crude_oil_data["usd_inr_rate"] = usdinr.rate
            mcx_crude_oil_data["usd_last"] = mcx_crude_oil.last / usdinr.rate
            if mcx_crude_oil.change is not None:
                mcx_crude_oil_data["usd_change"] = (
                    mcx_crude_oil.change / usdinr.rate
                )
            if mcx_crude_oil.prev_close is not None:
                mcx_crude_oil_data["usd_prev_close"] = (
                    mcx_crude_oil.prev_close / usdinr.rate
                )
            if mcx_crude_oil.open_ is not None:
                mcx_crude_oil_data["usd_open"] = (
                    mcx_crude_oil.open_ / usdinr.rate
                )

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "usd_inr": fx_data,
        "comex_gold": gold_data,
        "comex_silver": silver_data,
        "mcx_gold": mcx_data,
        "mcx_silver": mcx_silver_data,
        "mcx_crude_oil": mcx_crude_oil_data,
        "errors": errors,
    }


async def _refresh_prices(force: bool = False) -> dict[str, Any]:
    global _cache, _cache_updated_at
    async with _refresh_lock:
        now = datetime.now(timezone.utc)
        if (
            not force
            and _cache is not None
            and _cache_updated_at is not None
            and (now - _cache_updated_at).total_seconds() < _CACHE_SECONDS
        ):
            return _cache

        payload = await asyncio.to_thread(_collect_prices)
        if (
            not payload["usd_inr"]
            and not payload["comex_gold"]
            and not payload["mcx_gold"]
            and not payload["mcx_silver"]
            and not payload["mcx_crude_oil"]
        ):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "All commodity and exchange-rate sources failed.",
                    "errors": payload["errors"],
                },
            )
        _cache = payload
        _cache_updated_at = datetime.now(timezone.utc)
        return payload


@app.get("/", response_class=FileResponse, include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(_DASHBOARD_FILE, media_type="text/html")


@app.get("/api/prices")
async def prices() -> dict[str, Any]:
    return await _refresh_prices()


@app.post("/api/refresh")
async def refresh_prices() -> dict[str, Any]:
    return await _refresh_prices(force=True)


def main() -> None:
    uvicorn.run("fastapi_app:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
