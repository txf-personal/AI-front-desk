"""File-backed shop and the built desk. Used by start.sh and Docker, not by pytest fixtures."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import create_session_factory, create_sqlite_engine, init_db
from app.main import create_app
from app.seed import seed


def database_url() -> str:
    configured = os.environ.get("NORTHSIDE_DB", "").strip()
    if configured.startswith("sqlite:"):
        return configured
    path = Path(configured) if configured else Path.cwd() / "data" / "northside.db"
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return "sqlite:///" + path.as_posix()


def desk_dist() -> Path | None:
    configured = os.environ.get("DESK_DIST", "").strip()
    candidates = [Path(configured)] if configured else []
    here = Path(__file__).resolve()
    candidates.extend(
        [
            here.parents[2] / "frontend" / "dist",
            Path.cwd() / "frontend" / "dist",
        ]
    )
    for path in candidates:
        if (path / "index.html").is_file():
            return path
    return None


def create_runtime_app() -> FastAPI:
    """Open the shop file, seed it once, and serve the built desk beside the API."""
    engine = create_sqlite_engine(database_url())
    init_db(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        seed(session)
    app = create_app(factory)
    dist = desk_dist()
    if dist is not None:
        app.mount("/", StaticFiles(directory=dist, html=True), name="desk")
    return app
