"""FastAPI application.

Serves the latest `SnapshotDTO` and nothing else model-related — inference
only ever happens in the scheduler's refresh cycle (`kmd/scheduler.py`).
Every request handler here is a cheap read: parse a small JSON file (or
read a value already held in memory) and validate/return it. This file
never imports `kmd.forecast.engine` or touches Kronos.

The scheduler and the API can run as the same process (typical local
usage: `uvicorn kmd.api:app`, with the scheduler started in the FastAPI
lifespan) or as two processes sharing the snapshot file on disk — either
way, `SnapshotFileStore` is the single hand-off point, so an API-only
restart immediately has the last-known snapshot to serve without waiting
for the next scheduled refresh.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from kmd.config import REPO_ROOT, Settings
from kmd.snapshot import SnapshotDTO

WEB_DIR = REPO_ROOT / "web"

SnapshotProvider = Callable[[], SnapshotDTO | None]


class SnapshotFileStore:
    """Reads/writes the latest snapshot as a single JSON file. Used both
    as the scheduler's `snapshot_sink` and the API's `SnapshotProvider`.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def save(self, snapshot: SnapshotDTO) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(snapshot.model_dump_json(), encoding="utf-8")
        tmp_path.replace(self._path)  # atomic on POSIX: no reader ever sees a partial write

    def load(self) -> SnapshotDTO | None:
        if not self._path.exists():
            return None
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return SnapshotDTO.model_validate(raw)


def create_app(snapshot_provider: SnapshotProvider, web_dir: Path = WEB_DIR) -> FastAPI:
    app = FastAPI(title="Kronos Market Desk")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/snapshot")
    def get_snapshot() -> dict[str, object]:
        snapshot = snapshot_provider()
        if snapshot is None:
            raise HTTPException(status_code=503, detail="snapshot not yet available")
        return snapshot.model_dump(mode="json")

    @app.get("/api/asset/{symbol:path}")
    def get_asset(symbol: str) -> dict[str, object]:
        snapshot = snapshot_provider()
        if snapshot is None:
            raise HTTPException(status_code=503, detail="snapshot not yet available")
        for asset in snapshot.assets:
            if asset.display_symbol == symbol:
                return asset.model_dump(mode="json")
        raise HTTPException(status_code=404, detail=f"unknown symbol {symbol!r}")

    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

    return app


def _default_provider() -> SnapshotDTO | None:
    settings = Settings()
    store = SnapshotFileStore(settings.db_path.parent / "snapshot.json")
    return store.load()


app = create_app(_default_provider)
