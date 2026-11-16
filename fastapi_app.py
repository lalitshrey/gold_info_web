#!/usr/bin/env python3
"""FastAPI browser dashboard for precious-metals prices."""

from __future__ import annotations

import asyncio
import hashlib
import os
import json
import logging
import re
import secrets
import smtplib
import threading
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlencode, urlsplit

import uvicorn
import httpx
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.starlette_client import OAuth
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, field_validator
from pwdlib import PasswordHash
from sqlalchemy.exc import IntegrityError
from starlette.middleware.sessions import SessionMiddleware

from stock_news_agent.aggregator import merge_filter_sort
from stock_news_agent.providers.rss_provider import fetch_rss
from database import (
    User,
    close_database,
    create_google_user,
    create_user,
    create_password_reset_token,
    get_user_by_email,
    get_user_by_id,
    initialize_database,
    revoke_password_reset_token,
    reset_user_password,
    store_news_articles,
)
from gold_research import fetch_gold_momentum
from get_goldbees_quote import (
    fetch_goldbees_quote,
    fetch_silverbees_quote,
    fetch_zerodha_bse_goldbees_quote,
    fetch_zerodha_bse_silverbees_quote,
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
load_dotenv(_BASE_DIR / ".env")
_DASHBOARD_FILE = _BASE_DIR / "templates" / "metals_dashboard.html"
_AUTH_FILE = _BASE_DIR / "templates" / "auth.html"
_STATIC_DIR = _BASE_DIR / "static"
_NSE_SYMBOLS_FILE = _BASE_DIR / "stock_news_agent" / "static" / "nse_symbols.json"
_GOLD_NEWS_FEED = "https://news.google.com/rss/search?" + urlencode({
    "q": '"gold price" OR bullion OR "gold market" OR "gold demand" OR "central bank gold"',
    "hl": "en-US",
    "gl": "US",
    "ceid": "US:en",
})
_WAR_MARKET_NEWS_FEED = "https://news.google.com/rss/search?" + urlencode({
    "q": (
        '(war OR conflict OR invasion OR airstrike OR missile OR ceasefire '
        'OR sanctions OR "military strike" OR "geopolitical tension") '
        '(gold OR stocks OR oil OR markets OR economy)'
    ),
    "hl": "en-US",
    "gl": "US",
    "ceid": "US:en",
})
_CACHE_SECONDS = 60
_logger = logging.getLogger(__name__)
_cache: dict[str, Any] | None = None
_cache_updated_at: datetime | None = None
_refresh_lock = asyncio.Lock()
_sentiment_lock = threading.Lock()
_sentiment_analyzer: Any = None
_password_hash = PasswordHash.recommended()
_dummy_password_hash = _password_hash.hash("auricai-invalid-login-password")
_google_client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
_google_client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
if bool(_google_client_id) != bool(_google_client_secret):
    raise RuntimeError("Configure both GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.")

oauth = OAuth()
if _google_client_id and _google_client_secret:
    oauth.register(
        name="google",
        client_id=_google_client_id,
        client_secret=_google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required. Configure it in .env before starting AuricAI.")
    if not database_url.startswith(("mysql+pymysql://", "sqlite:///")):
        raise RuntimeError("DATABASE_URL must use mysql+pymysql:// (or sqlite:/// for tests).")

    session_secret = os.getenv("SESSION_SECRET", "")
    if len(session_secret) < 32:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters.")

    await asyncio.to_thread(initialize_database, database_url)
    try:
        yield
    finally:
        await asyncio.to_thread(close_database)


app = FastAPI(
    title="AuricAI",
    description="COMEX, MCX and Gold BeES prices, and USD/INR.",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET") or "invalid-session-secret-configure-env",
    session_cookie="auricai_session",
    same_site="lax",
    https_only=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


class AccountCredentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)


class RegistrationCredentials(AccountCredentials):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    mobile_number: str = Field(min_length=7, max_length=32)
    password_confirmation: str = Field(max_length=128)

    @field_validator("first_name", "last_name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("This field is required.")
        return normalized

    @field_validator("mobile_number")
    @classmethod
    def validate_mobile_number(cls, value: str) -> str:
        normalized = value.strip()
        digits = re.sub(r"\D", "", normalized)
        if (
            not re.fullmatch(r"\+?[0-9\s().-]+", normalized)
            or not 7 <= len(digits) <= 15
        ):
            raise ValueError("Enter a valid mobile number with 7 to 15 digits.")
        return normalized


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetCredentials(BaseModel):
    token: str = Field(min_length=32, max_length=128)
    password: str = Field(min_length=10, max_length=128)
    password_confirmation: str = Field(max_length=128)


class GoogleIdentity(BaseModel):
    sub: str = Field(min_length=1)
    email: EmailStr
    email_verified: bool
    given_name: str | None = None
    family_name: str | None = None


def _deliver_password_reset_email(email: str, reset_url: str, token_hash: str) -> None:
    try:
        _send_password_reset_email(email, reset_url)
    except (OSError, smtplib.SMTPException):
        _logger.exception("Could not send password reset email.")
        revoke_password_reset_token(token_hash)


async def require_user(request: Request) -> User:
    user_id = request.session.get("user_id")
    if not isinstance(user_id, int):
        raise HTTPException(status_code=401, detail="Sign in to continue.")

    user = await asyncio.to_thread(get_user_by_id, user_id)
    if user is None:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return user


def _normalized_email(email: EmailStr) -> str:
    return str(email).strip().lower()


def _google_redirect_uri() -> str:
    base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    parsed_base_url = urlsplit(base_url)
    if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.netloc:
        raise HTTPException(
            status_code=503,
            detail="Google sign-in is not configured. Contact the site administrator.",
        )
    return f"{base_url}/auth/google/callback"


def _password_reset_email_settings() -> tuple[str, int, str, str, str, bool, bool]:
    host = os.getenv("SMTP_HOST", "").strip()
    sender = os.getenv("SMTP_FROM_EMAIL", "").strip()
    base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    parsed_base_url = urlsplit(base_url)
    if not host or not sender or not base_url:
        raise HTTPException(
            status_code=503,
            detail="Password reset email is not configured. Contact the site administrator.",
        )
    if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.netloc:
        raise HTTPException(
            status_code=503,
            detail="Password reset email is not configured. Contact the site administrator.",
        )

    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    if bool(username) != bool(password):
        raise HTTPException(
            status_code=503,
            detail="Password reset email is not configured. Contact the site administrator.",
        )

    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError as error:
        raise HTTPException(
            status_code=503,
            detail="Password reset email is not configured. Contact the site administrator.",
        ) from error

    starttls = os.getenv("SMTP_STARTTLS", "true").lower() == "true"
    use_ssl = os.getenv("SMTP_USE_SSL", "false").lower() == "true"
    if (starttls and use_ssl) or (username and not (starttls or use_ssl)):
        raise HTTPException(
            status_code=503,
            detail="Password reset email is not configured. Contact the site administrator.",
        )

    return (
        host,
        port,
        username,
        password,
        sender,
        starttls,
        use_ssl,
    )


def _send_password_reset_email(email: str, reset_url: str) -> None:
    host, port, username, password, sender, starttls, use_ssl = (
        _password_reset_email_settings()
    )
    message = EmailMessage()
    message["Subject"] = "Reset your AuricAI password"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        "We received a request to reset your AuricAI password.\n\n"
        f"Use this link within 30 minutes to choose a new password:\n{reset_url}\n\n"
        "If you did not request this, you can ignore this email."
    )

    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_class(host, port, timeout=15) as smtp:
        if starttls and not use_ssl:
            smtp.starttls()
        if username:
            smtp.login(username, password)
        smtp.send_message(message)


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
                "summary": article.summary,
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


def _analyze_gold_news(articles: list[Any]) -> list[dict[str, Any]]:
    if not articles:
        return []
    analyzer = _get_sentiment_analyzer()
    analyzed_articles = []
    for article in articles:
        text = f"{article.title}\n{article.summary or ''}".strip()
        scores = analyzer.polarity_scores(text)
        compound = float(scores["compound"])
        analyzed_articles.append({
            "title": article.title,
            "url": str(article.url),
            "published_at": (
                article.published_at.isoformat()
                if article.published_at
                else None
            ),
            "source": article.source.model_dump(mode="json"),
            "summary": article.summary,
            "sentiment": {
                "compound": compound,
                "label": _sentiment_label(compound),
            },
        })
    return analyzed_articles


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
    silverbees: Any = None
    zerodha_silverbees: Any = None

    fetchers = {
        "comex": get_gold_silver,
        "usd_inr": get_usdinr,
        "mcx_gold": get_mcx_gold,
        "mcx_silver": get_mcx_silver_price,
        "mcx_crude_oil": get_mcx_crude_oil_price,
        "goldbees": fetch_goldbees_quote,
        "zerodha_goldbees": fetch_zerodha_bse_goldbees_quote,
        "silverbees": fetch_silverbees_quote,
        "zerodha_silverbees": fetch_zerodha_bse_silverbees_quote,
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
    silverbees = results.get("silverbees")
    zerodha_silverbees = results.get("zerodha_silverbees")

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
        "silverbees": _quote_dict(silverbees) if silverbees is not None else None,
        "zerodha_silverbees": (
            _quote_dict(zerodha_silverbees)
            if zerodha_silverbees is not None
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
            and not payload["silverbees"]
            and not payload["zerodha_silverbees"]
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


@app.get("/login", response_class=FileResponse, response_model=None, include_in_schema=False)
async def login_page(request: Request) -> FileResponse | RedirectResponse:
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return FileResponse(_AUTH_FILE, media_type="text/html")


@app.get("/auth/google")
async def google_login(request: Request) -> RedirectResponse:
    if not _google_client_id or not _google_client_secret:
        return RedirectResponse("/login?google_error=not_configured", status_code=303)

    try:
        return await oauth.google.authorize_redirect(request, _google_redirect_uri())
    except (OAuthError, httpx.HTTPError):
        _logger.exception("Could not start Google sign-in.")
        return RedirectResponse("/login?google_error=unavailable", status_code=303)


@app.get("/auth/google/callback")
async def google_callback(request: Request) -> RedirectResponse:
    if not _google_client_id or not _google_client_secret:
        return RedirectResponse("/login?google_error=not_configured", status_code=303)

    try:
        token = await oauth.google.authorize_access_token(request)
    except (OAuthError, httpx.HTTPError):
        _logger.exception("Google sign-in callback failed.")
        return RedirectResponse("/login?google_error=failed", status_code=303)

    userinfo = token.get("userinfo")
    if not isinstance(userinfo, dict):
        return RedirectResponse("/login?google_error=invalid_profile", status_code=303)
    try:
        identity = GoogleIdentity.model_validate(userinfo)
    except ValueError:
        return RedirectResponse("/login?google_error=invalid_profile", status_code=303)
    if not identity.email_verified:
        return RedirectResponse("/login?google_error=unverified_email", status_code=303)

    email = _normalized_email(identity.email)
    user = await asyncio.to_thread(get_user_by_email, email)
    if user is None:
        password_hash = await asyncio.to_thread(
            _password_hash.hash,
            secrets.token_urlsafe(48),
        )
        user = await asyncio.to_thread(
            create_google_user,
            email,
            password_hash,
            (identity.given_name or "")[:100],
            (identity.family_name or "")[:100],
        )

    request.session.clear()
    request.session.update({"user_id": user.id, "email": user.email})
    return RedirectResponse("/", status_code=303)


@app.get("/register", response_class=FileResponse, response_model=None, include_in_schema=False)
async def registration_page(request: Request) -> FileResponse | RedirectResponse:
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return FileResponse(_AUTH_FILE, media_type="text/html")


@app.get(
    "/forgot-password",
    response_class=FileResponse,
    response_model=None,
    include_in_schema=False,
)
async def forgot_password_page(request: Request) -> FileResponse | RedirectResponse:
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return FileResponse(_AUTH_FILE, media_type="text/html")


@app.get(
    "/reset-password",
    response_class=FileResponse,
    response_model=None,
    include_in_schema=False,
)
async def reset_password_page(request: Request) -> FileResponse | RedirectResponse:
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return FileResponse(_AUTH_FILE, media_type="text/html")


@app.post("/auth/register")
async def register_account(
    credentials: RegistrationCredentials,
    request: Request,
) -> dict[str, str]:
    if credentials.password != credentials.password_confirmation:
        raise HTTPException(status_code=422, detail="Passwords do not match.")

    email = _normalized_email(credentials.email)
    password_hash = await asyncio.to_thread(_password_hash.hash, credentials.password)
    try:
        user = await asyncio.to_thread(
            create_user,
            email,
            password_hash,
            credentials.first_name,
            credentials.last_name,
            credentials.mobile_number,
        )
    except IntegrityError as error:
        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists.",
        ) from error

    request.session.clear()
    request.session.update({"user_id": user.id, "email": user.email})
    return {"email": user.email}


@app.post("/auth/forgot-password", status_code=202)
async def request_password_reset(
    credentials: PasswordResetRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    _password_reset_email_settings()
    email = _normalized_email(credentials.email)
    user = await asyncio.to_thread(get_user_by_email, email)
    if user is not None:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=30)
        should_send = await asyncio.to_thread(
            create_password_reset_token,
            user.id,
            token_hash,
            expires_at,
        )
        if should_send:
            base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
            reset_url = f"{base_url}/reset-password#{urlencode({'token': token})}"
            background_tasks.add_task(
                _deliver_password_reset_email,
                user.email,
                reset_url,
                token_hash,
            )

    return {
        "message": "If an account exists for that email, a password reset link will be sent."
    }


@app.post("/auth/reset-password")
async def complete_password_reset(
    credentials: PasswordResetCredentials,
) -> dict[str, str]:
    if credentials.password != credentials.password_confirmation:
        raise HTTPException(status_code=422, detail="Passwords do not match.")

    token_hash = hashlib.sha256(credentials.token.encode("utf-8")).hexdigest()
    password_hash = await asyncio.to_thread(_password_hash.hash, credentials.password)
    reset = await asyncio.to_thread(reset_user_password, token_hash, password_hash)
    if not reset:
        raise HTTPException(
            status_code=400,
            detail="This password reset link is invalid or has expired. Request a new link.",
        )
    return {"message": "Password reset successfully. You can now sign in."}


@app.post("/auth/login")
async def authenticate_account(
    credentials: AccountCredentials,
    request: Request,
) -> dict[str, str]:
    email = _normalized_email(credentials.email)
    user = await asyncio.to_thread(get_user_by_email, email)
    password_matches = await asyncio.to_thread(
        _password_hash.verify,
        credentials.password,
        user.password_hash if user else _dummy_password_hash,
    )
    if user is None or not password_matches:
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    request.session.clear()
    request.session.update({"user_id": user.id, "email": user.email})
    return {"email": user.email}


@app.post("/auth/logout")
async def logout(request: Request) -> dict[str, bool]:
    request.session.clear()
    return {"logged_out": True}


@app.get("/", response_class=FileResponse, response_model=None, include_in_schema=False)
async def dashboard(request: Request) -> FileResponse | RedirectResponse:
    if not request.session.get("user_id"):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(_DASHBOARD_FILE, media_type="text/html")


@app.get("/api/prices")
async def prices(_: User = Depends(require_user)) -> dict[str, Any]:
    return await _refresh_prices()


@app.post("/api/refresh")
async def refresh_prices(_: User = Depends(require_user)) -> dict[str, Any]:
    return await _refresh_prices(force=True)


@app.get("/api/news")
async def stock_news(_: User = Depends(require_user)) -> dict[str, Any]:
    try:
        market_items, war_market_items = await asyncio.gather(
            fetch_rss(),
            fetch_rss([_WAR_MARKET_NEWS_FEED]),
        )
        articles = merge_filter_sort(
            market_items,
            lookback_minutes=7 * 24 * 60,
            limit=12,
        )
        war_articles = merge_filter_sort(
            war_market_items,
            lookback_minutes=7 * 24 * 60,
            limit=8,
        )
        analyzed_articles, stock_sentiments = await asyncio.to_thread(
            _analyze_news,
            articles,
        )
        analyzed_war_articles = await asyncio.to_thread(
            _analyze_gold_news,
            war_articles,
        )
        for article in analyzed_war_articles:
            article["category"] = "War & geopolitical risk"
        await asyncio.gather(
            asyncio.to_thread(
                store_news_articles,
                analyzed_articles,
                category="market",
            ),
            asyncio.to_thread(
                store_news_articles,
                analyzed_war_articles,
                category="war",
            ),
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
        "war_articles": analyzed_war_articles,
        "stock_sentiments": stock_sentiments,
    }


@app.get("/api/gold-news")
async def gold_news(_: User = Depends(require_user)) -> dict[str, Any]:
    try:
        items = await fetch_rss([_GOLD_NEWS_FEED])
        articles = merge_filter_sort(
            items,
            lookback_minutes=7 * 24 * 60,
            limit=8,
        )
        gold_articles = [
            article
            for article in articles
            if any(
                term in f"{article.title}\n{article.summary or ''}".casefold()
                for term in ("gold", "bullion", "precious metal")
            )
        ]
        analyzed_articles = await asyncio.to_thread(
            _analyze_gold_news,
            gold_articles,
        )
        await asyncio.to_thread(
            store_news_articles,
            analyzed_articles,
            category="gold",
        )
    except Exception as error:
        _logger.exception("Could not fetch gold-impacting news")
        raise HTTPException(
            status_code=503,
            detail=f"Gold-impacting news or sentiment analysis is temporarily unavailable: {error}",
        ) from error

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "articles": analyzed_articles,
    }


@app.get("/api/gold-outlook")
async def gold_outlook(_: User = Depends(require_user)) -> dict[str, Any]:
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
