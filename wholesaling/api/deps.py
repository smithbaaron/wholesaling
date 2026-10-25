"""FastAPI dependencies for the operator surface: settings, DB session, Temporal gateway,
Jinja2 templates and the static-files mount.

Everything is reached through ``request.app.state`` (set in ``create_app``'s lifespan) so tests
build the app with their own settings, an in-memory SQLite engine and the mock gateway.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wholesaling.config.settings import Settings
from wholesaling.db.engine import make_session_factory
from wholesaling.integrations.temporal.interface import TemporalGateway

API_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = API_DIR / "templates"
STATIC_DIR = API_DIR / "static"


def get_settings_dep(request: Request) -> Settings:
    settings = request.app.state.settings
    assert isinstance(settings, Settings)
    return settings


def get_temporal_dep(request: Request) -> TemporalGateway:
    gateway: TemporalGateway = request.app.state.temporal
    return gateway


def get_session_factory_dep(request: Request) -> async_sessionmaker[AsyncSession]:
    """One session factory per app, created lazily from ``app.state.engine``."""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        factory = make_session_factory(request.app.state.engine)
        request.app.state.session_factory = factory
    assert isinstance(factory, async_sessionmaker)
    return factory


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """A request-scoped session. Routes commit explicitly; anything uncommitted rolls back."""
    factory = get_session_factory_dep(request)
    async with factory() as session:
        yield session


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(max(seconds, 0))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86_400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86_400}d {(s % 86_400) // 3600}h"


def _fmt_pct(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value * 100:.{digits}f}%"


def _fmt_money(value: Any) -> str:
    if value is None:
        return "-"
    return f"${float(value):,.2f}"


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.strftime("%Y-%m-%d %H:%M UTC")


def build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["age"] = _fmt_age
    templates.env.filters["pct"] = _fmt_pct
    templates.env.filters["money"] = _fmt_money
    templates.env.filters["dt"] = _fmt_dt
    return templates


def get_templates(request: Request) -> Jinja2Templates:
    templates = getattr(request.app.state, "templates", None)
    if templates is None:
        templates = build_templates()
        request.app.state.templates = templates
    assert isinstance(templates, Jinja2Templates)
    return templates


def mount_static(app: FastAPI) -> None:
    """Serve ``wholesaling/api/static`` (one CSS file + vendored htmx) at ``/static``."""
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def wants_html(request: Request) -> bool:
    """True for a browser / HTMX request, False for an API client asking for JSON."""
    if is_htmx(request):
        return True
    accept = request.headers.get("accept", "")
    return "text/html" in accept and "application/json" not in accept.split(",")[0]


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
TemporalDep = Annotated[TemporalGateway, Depends(get_temporal_dep)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
TemplatesDep = Annotated[Jinja2Templates, Depends(get_templates)]

__all__ = [
    "STATIC_DIR",
    "TEMPLATES_DIR",
    "SessionDep",
    "SettingsDep",
    "TemplatesDep",
    "TemporalDep",
    "build_templates",
    "get_session",
    "get_session_factory_dep",
    "get_settings_dep",
    "get_templates",
    "get_temporal_dep",
    "is_htmx",
    "mount_static",
    "wants_html",
]
