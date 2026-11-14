#!/usr/bin/env python3
"""FastAPI browser dashboard for precious-metals prices."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from stock_news_agent.aggregator import merge_filter_sort
from stock_news_agent.providers.rss_provider import fetch_rss
from gold_research import fetch_gold_momentum
from get_goldbees_quote import (
    fetch_goldbees_quote,
    fetch_zerodha_bse_goldbees_quote,
)
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
_NSE_SYMBOLS_FILE = _BASE_DIR / "stock_news_agent" / "static" / "nse_symbols.json"
_CACHE_SECONDS = 60
_logger = logging.getLogger(__name__)
_cache: dict[str, Any] | None = None
_cache_updated_at: datetime | None = None
_refresh_lock = asyncio.Lock()
_sentiment_lock = threading.Lock()
_sentiment_analyzer: Any = None

app = FastAPI(
    title="Gold Info Web",
    description="COMEX, MCX and Gold BeES prices, and USD/INR.",
    version="1.0.0",
)


def _sentiment_label(compound: float) -> str:
    if compound >= 0.05:
        return "Positive"
    if compound <= -0.05:
        return "Negative"
    return "Neutral"


def _get_sentiment_analyzer() -> Any:
    global _sentiment_analyzer
    with _sentiment_lock:
        if _sentiment_analyzer is not None:
            return _sentiment_analyzer

        import nltk
        from nltk.sentiment import SentimentIntensityAnalyzer

        try:
            nltk.data.find("sentiment/vader_lexicon.zip")
        except LookupError:
            if not nltk.download("vader_lexicon", quiet=True):
                raise RuntimeError("Could not download the VADER sentiment lexicon.")

        _sentiment_analyzer = SentimentIntensityAnalyzer()
        return _sentiment_analyzer


@lru_cache(maxsize=1)
def _stock_matchers() -> tuple[
    tuple[str, str, tuple[re.Pattern[str], ...]], ...
]:
    with _NSE_SYMBOLS_FILE.open(encoding="utf-8") as symbols_file:
        stocks = json.load(symbols_file)

    matchers: list[tuple[str, str, re.Pattern[str]]] = []
    for stock in stocks:
        symbol = stock["symbol"]
        name = stock["name"]
        aliases = {
            name,
            re.sub(
                r"(?:\s+(?:ltd\.?|limited|corp\.?|corporation|inc\.?|incorporated|company|co\.?))+$",
                "",
                name,
                flags=re.IGNORECASE,
            ).strip(),
        }
        patterns = [
            re.compile(
                r"(?<!\w)" + r"\s+".join(re.escape(word) for word in alias.split()) + r"(?!\w)",
                re.IGNORECASE,
            )
            for alias in aliases
            if len(alias) >= 5
        ]
        if len(symbol) >= 3:
            patterns.append(
                re.compile(rf"(?<![A-Z0-9])\$?{re.escape(symbol)}(?![A-Z0-9])")
            )
        matchers.append((symbol, name, tuple(patterns)))
    return tuple(matchers)


def _analyze_news(
    articles: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    analyzer = _get_sentiment_analyzer()
    ticker_scores: dict[str, list[float]] = {}
    ticker_names: dict[str, str] = {}
    analyzed_articles: list[dict[str, Any]] = []

    for article in articles:
        text = f"{article.title}\n{article.summary or ''}".strip()
        scores = analyzer.polarity_scores(text)
        compound = float(scores["compound"])
        sentiment = {
            "compound": compound,
            "label": _sentiment_label(compound),
            "positive": float(scores["pos"]),
            "neutral": float(scores["neu"]),
            "negative": float(scores["neg"]),
        }
        related_stocks = [
            {"symbol": symbol, "name": name}
            for symbol, name, patterns in _stock_matchers()
            if any(pattern.search(text) for pattern in patterns)
        ]
        for stock in related_stocks:
            symbol = stock["symbol"]
            ticker_scores.setdefault(symbol, []).append(compound)
            ticker_names[symbol] = stock["name"]

        analyzed_articles.append(
            {
                "title": article.title,
                "url": str(article.url),
                "published_at": (
                    article.published_at.isoformat()
                    if article.published_at
                    else None
                ),
                "source": article.source.model_dump(mode="json"),
                "related_stocks": related_stocks,
                "sentiment": sentiment,
            }
        )

    stock_sentiments = []
    for symbol, scores in ticker_scores.items():
        average = sum(scores) / len(scores)
        stock_sentiments.append({
            "symbol": symbol,
            "name": ticker_names[symbol],
            "article_count": len(scores),
            "compound": average,
            "label": _sentiment_label(average),
        })
    stock_sentiments.sort(
        key=lambda stock: (-stock["article_count"], stock["symbol"])
    )
    return analyzed_articles, stock_sentiments


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
    goldbees: Any = None
    zerodha_goldbees: Any = None

    fetchers = {
        "comex": get_gold_silver,
        "usd_inr": get_usdinr,
        "mcx_gold": get_mcx_gold,
        "mcx_silver": get_mcx_silver_price,
        "mcx_crude_oil": get_mcx_crude_oil_price,
        "goldbees": fetch_goldbees_quote,
        "zerodha_goldbees": fetch_zerodha_bse_goldbees_quote,
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
    goldbees = results.get("goldbees")
    zerodha_goldbees = results.get("zerodha_goldbees")

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
        "goldbees": _quote_dict(goldbees) if goldbees is not None else None,
        "zerodha_goldbees": (
            _quote_dict(zerodha_goldbees)
            if zerodha_goldbees is not None
            else None
        ),
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
            and not payload["goldbees"]
            and not payload["zerodha_goldbees"]
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


@app.get("/api/news")
async def stock_news() -> dict[str, Any]:
    try:
        items = await fetch_rss()
        articles = merge_filter_sort(
            items,
            lookback_minutes=7 * 24 * 60,
            limit=12,
        )
        analyzed_articles, stock_sentiments = await asyncio.to_thread(
            _analyze_news,
            articles,
        )
    except Exception as error:
        _logger.exception("Could not fetch stock and market news")
        raise HTTPException(
            status_code=503,
            detail=f"Stock news or sentiment analysis is temporarily unavailable: {error}",
        ) from error

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "articles": analyzed_articles,
        "stock_sentiments": stock_sentiments,
    }


@app.get("/api/gold-outlook")
async def gold_outlook() -> dict[str, Any]:
    try:
        momentum = await asyncio.to_thread(fetch_gold_momentum)
    except RuntimeError as error:
        _logger.warning("Could not build gold outlook: %s", error)
        raise HTTPException(
            status_code=503,
            detail="Gold price history is temporarily unavailable.",
        ) from error

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "momentum": momentum,
        "research": {
            "source": "World Gold Council",
            "published": "2026-11-03",
            "url": (
                "https://www.gold.org/goldhub/research/"
                "gold-demand-trends/gold-demand-trends-q3-2026"
            ),
            "long_term_assessment": (
                "Mixed: strong institutional demand supports gold, while "
                "high prices are weighing on jewellery and retail demand."
            ),
            "supporting_factors": [
                "Central banks bought 321 tonnes in Q3 2026, up 42% year over year.",
                "Gold ETFs added 211 tonnes in Q3 after Q2 outflows.",
            ],
            "headwinds": [
                "Jewellery demand fell 17% year over year as high prices constrained affordability.",
                "Bar and coin demand fell 19% year over year as buyers waited for a clearer price trend.",
            ],
            "watch_factors": [
                "US real interest rates and Federal Reserve policy",
                "US dollar direction",
                "Central-bank purchases and gold ETF flows",
                "Geopolitical and fiscal-risk developments",
            ],
        },
        "disclaimer": (
            "Historical momentum and cited research are not price forecasts or "
            "investment advice."
        ),
    }


def main() -> None:
    uvicorn.run("fastapi_app:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
