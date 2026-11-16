"""MySQL persistence for users and normalized news articles."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from sqlalchemy import (
    DateTime,
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


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed
