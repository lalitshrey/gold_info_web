"""MySQL persistence for users, news, and stock recommendations."""

from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import (
    DateTime,
    Date,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    first_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        default="",
        server_default="",
    )
    last_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        default="",
        server_default="",
    )
    mobile_number: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="",
        server_default="",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NewsLink(Base):
    __tablename__ = "news_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)


class NewsArticle(Base):
    __tablename__ = "news_articles"
    __table_args__ = (
        UniqueConstraint("link_id", "category", name="uq_news_article_link_category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    link_id: Mapped[int] = mapped_column(
        ForeignKey("news_links.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str] = mapped_column(String(500), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sentiment_label: Mapped[str | None] = mapped_column(String(16))
    sentiment_score: Mapped[float | None] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class StockRecommendation(Base):
    __tablename__ = "stock_recommendations"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "symbol",
            "recommendation_date",
            name="uq_stock_recommendation_user_symbol_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    recommendation_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    recommended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_price: Mapped[float] = mapped_column(Float, nullable=False)
    price_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    atr_14: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    target: Mapped[float] = mapped_column(Float, nullable=False)
    holding_period_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open", index=True)
    outcome_date: Mapped[date | None] = mapped_column(Date)
    outcome_price: Mapped[float | None] = mapped_column(Float)
    checked_sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_checked_date: Mapped[date | None] = mapped_column(Date)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


_engine = None
_session_factory: sessionmaker[Session] | None = None


def initialize_database(database_url: str) -> None:
    """Create database tables and configure the shared session factory."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()

    connect_args = {"charset": "utf8mb4"} if database_url.startswith("mysql+") else {}
    _engine = create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
    Base.metadata.create_all(_engine)
    _ensure_user_profile_columns(_engine)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)


def _ensure_user_profile_columns(engine: Any) -> None:
    existing_columns = {column["name"] for column in inspect(engine).get_columns("users")}
    profile_columns = {
        "first_name": "VARCHAR(100) NOT NULL DEFAULT ''",
        "last_name": "VARCHAR(100) NOT NULL DEFAULT ''",
        "mobile_number": "VARCHAR(32) NOT NULL DEFAULT ''",
    }
    with engine.begin() as connection:
        for column_name, definition in profile_columns.items():
            if column_name not in existing_columns:
                connection.execute(
                    text(f"ALTER TABLE users ADD COLUMN {column_name} {definition}")
                )


def close_database() -> None:
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def _session() -> Session:
    if _session_factory is None:
        raise RuntimeError("Database is not initialized; configure DATABASE_URL and restart the app.")
    return _session_factory()


def get_user_by_email(email: str) -> User | None:
    with _session() as session:
        return session.scalar(select(User).where(User.email == email))


def get_user_by_id(user_id: int) -> User | None:
    with _session() as session:
        return session.get(User, user_id)


def create_user(
    email: str,
    password_hash: str,
    first_name: str,
    last_name: str,
    mobile_number: str,
) -> User:
    with _session() as session:
        user = User(
            email=email,
            password_hash=password_hash,
            first_name=first_name,
            last_name=last_name,
            mobile_number=mobile_number,
        )
        session.add(user)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise
        session.refresh(user)
        return user


def create_google_user(
    email: str,
    password_hash: str,
    first_name: str,
    last_name: str,
) -> User:
    with _session() as session:
        user = User(
            email=email,
            password_hash=password_hash,
            first_name=first_name or "",
            last_name=last_name or "",
            mobile_number="",
        )
        session.add(user)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing_user = session.scalar(select(User).where(User.email == email))
            if existing_user is None:
                raise
            return existing_user
        session.refresh(user)
        return user


def create_password_reset_token(
    user_id: int,
    token_hash: str,
    expires_at: datetime,
) -> bool:
    now = _utcnow()
    with _session() as session:
        latest_token = session.scalar(
            select(PasswordResetToken)
            .where(PasswordResetToken.user_id == user_id)
            .order_by(PasswordResetToken.created_at.desc())
            .limit(1)
        )
        if latest_token is not None and (now - latest_token.created_at).total_seconds() < 60:
            return False

        for token in session.scalars(
            select(PasswordResetToken).where(
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.used_at.is_(None),
            )
        ):
            token.used_at = now

        session.add(
            PasswordResetToken(
                user_id=user_id,
                token_hash=token_hash,
                expires_at=expires_at,
            )
        )
        session.commit()
        return True


def reset_user_password(token_hash: str, password_hash: str) -> bool:
    now = _utcnow()
    with _session() as session:
        token = session.scalar(
            select(PasswordResetToken)
            .where(PasswordResetToken.token_hash == token_hash)
            .with_for_update()
        )
        if token is None or token.used_at is not None or token.expires_at <= now:
            return False

        user = session.get(User, token.user_id)
        if user is None:
            return False

        user.password_hash = password_hash
        token.used_at = now
        session.commit()
        return True


def revoke_password_reset_token(token_hash: str) -> None:
    with _session() as session:
        token = session.scalar(
            select(PasswordResetToken).where(
                PasswordResetToken.token_hash == token_hash,
                PasswordResetToken.used_at.is_(None),
            )
        )
        if token is not None:
            token.used_at = _utcnow()
            session.commit()


def store_news_articles(
    articles: list[dict[str, Any]],
    *,
    category: str,
) -> None:
    """Upsert article metadata and a normalized URL row for every article."""
    if not articles:
        return

    now = _utcnow()
    with _session() as session:
        for article in articles:
            url = str(article["url"])
            url_hash = sha256(url.encode("utf-8")).hexdigest()
            link = session.scalar(select(NewsLink).where(NewsLink.url_hash == url_hash))
            if link is None:
                try:
                    with session.begin_nested():
                        link = NewsLink(url=url, url_hash=url_hash)
                        session.add(link)
                        session.flush()
                except IntegrityError:
                    link = session.scalar(
                        select(NewsLink).where(NewsLink.url_hash == url_hash)
                    )
                    if link is None:
                        raise

            source = article.get("source") or {}
            existing = session.scalar(
                select(NewsArticle).where(
                    NewsArticle.link_id == link.id,
                    NewsArticle.category == category,
                )
            )
            values = {
                "title": str(article["title"]),
                "source_name": str(source.get("name") or "Market news")[:500],
                "source_url": source.get("url"),
                "summary": article.get("summary"),
                "published_at": _parse_datetime(article.get("published_at")),
                "sentiment_label": (article.get("sentiment") or {}).get("label"),
                "sentiment_score": (article.get("sentiment") or {}).get("compound"),
                "fetched_at": now,
            }
            if existing is None:
                session.add(NewsArticle(link_id=link.id, category=category, **values))
            else:
                for field, value in values.items():
                    setattr(existing, field, value)
        session.commit()


def save_stock_recommendation(
    user_id: int,
    level: dict[str, Any],
    *,
    create_if_missing: bool = True,
) -> dict[str, Any] | None:
    """Save the day's recommendation and advance existing outcome tracking."""
    india_today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    now = _utcnow()
    price_as_of = _parse_datetime(level["price_as_of"])
    if price_as_of is None:
        raise ValueError("Stock recommendation is missing its price timestamp.")
    recommendation_values = {
        "name": str(level["name"])[:255],
        "recommended_at": now,
        "current_price": float(level["current_price"]),
        "price_as_of": price_as_of,
        "atr_14": float(level["atr_14"]),
        "stop_loss": float(level["stop_loss"]),
        "target": float(level["target"]),
        "holding_period_days": 30,
        "source": str(level["source"])[:100],
    }

    with _session() as session:
        recommendation = session.scalar(
            select(StockRecommendation).where(
                StockRecommendation.user_id == user_id,
                StockRecommendation.symbol == level["symbol"],
                StockRecommendation.recommendation_date == india_today,
            )
        )
        if recommendation is None and create_if_missing:
            try:
                with session.begin_nested():
                    recommendation = StockRecommendation(
                        user_id=user_id,
                        symbol=str(level["symbol"]),
                        recommendation_date=india_today,
                        **recommendation_values,
                    )
                    session.add(recommendation)
                    session.flush()
            except IntegrityError:
                recommendation = session.scalar(
                    select(StockRecommendation).where(
                        StockRecommendation.user_id == user_id,
                        StockRecommendation.symbol == level["symbol"],
                        StockRecommendation.recommendation_date == india_today,
                    )
                )
                if recommendation is None:
                    raise

        observations = sorted(level.get("daily_bars", []), key=lambda bar: bar["date"])
        open_recommendations = session.scalars(
            select(StockRecommendation).where(
                StockRecommendation.user_id == user_id,
                StockRecommendation.symbol == level["symbol"],
                StockRecommendation.status == "open",
            )
        )
        for open_recommendation in open_recommendations:
            for bar in observations:
                bar_date = date.fromisoformat(bar["date"])
                if (
                    bar_date <= open_recommendation.recommendation_date
                    or bar_date >= india_today
                    or (
                        open_recommendation.last_checked_date is not None
                        and bar_date <= open_recommendation.last_checked_date
                    )
                ):
                    continue

                open_recommendation.checked_sessions += 1
                open_recommendation.last_checked_date = bar_date
                stop_reached = float(bar["low"]) <= open_recommendation.stop_loss
                target_reached = float(bar["high"]) >= open_recommendation.target
                if stop_reached and target_reached:
                    open_recommendation.status = "both_hit_same_day"
                    open_recommendation.outcome_date = bar_date
                elif stop_reached:
                    open_recommendation.status = "stop_loss_hit"
                    open_recommendation.outcome_date = bar_date
                    open_recommendation.outcome_price = open_recommendation.stop_loss
                elif target_reached:
                    open_recommendation.status = "target_hit"
                    open_recommendation.outcome_date = bar_date
                    open_recommendation.outcome_price = open_recommendation.target
                elif open_recommendation.checked_sessions >= open_recommendation.holding_period_days:
                    open_recommendation.status = "expired"
                    open_recommendation.outcome_date = bar_date
                if open_recommendation.status != "open":
                    break
            open_recommendation.last_checked_at = now

        session.commit()
        if recommendation is None:
            return None
        session.refresh(recommendation)
        return _stock_recommendation_dict(recommendation)


def list_stock_recommendations(user_id: int, limit: int = 200) -> list[dict[str, Any]]:
    with _session() as session:
        records = session.scalars(
            select(StockRecommendation)
            .where(StockRecommendation.user_id == user_id)
            .order_by(
                StockRecommendation.recommendation_date.desc(),
                StockRecommendation.symbol,
            )
            .limit(limit)
        )
        return [_stock_recommendation_dict(record) for record in records]


def _stock_recommendation_dict(record: StockRecommendation) -> dict[str, Any]:
    return {
        "id": record.id,
        "symbol": record.symbol,
        "name": record.name,
        "recommendation_date": record.recommendation_date.isoformat(),
        "recommended_at": record.recommended_at.isoformat(),
        "current_price": record.current_price,
        "price_as_of": record.price_as_of.isoformat(),
        "atr_14": record.atr_14,
        "stop_loss": record.stop_loss,
        "target": record.target,
        "holding_period_days": record.holding_period_days,
        "source": record.source,
        "status": record.status,
        "outcome_date": (
            record.outcome_date.isoformat() if record.outcome_date is not None else None
        ),
        "outcome_price": record.outcome_price,
        "checked_sessions": record.checked_sessions,
        "last_checked_date": (
            record.last_checked_date.isoformat()
            if record.last_checked_date is not None
            else None
        ),
    }


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed
