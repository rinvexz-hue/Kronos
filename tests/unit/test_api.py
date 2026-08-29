"""Tests for `kmd.api`. Never touches the model or a real data store —
`create_app` is exercised with a plain callable snapshot provider, exactly
the seam the module docstring promises ("never touches the model at
request time").
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kmd.api import ReadinessState, SnapshotFileStore, create_app
from kmd.dto import (
    AssetSnapshot,
    CalibrationStats,
    DataSourceStatus,
    ForecastMetrics,
    Regime,
    SnapshotDTO,
)

BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _asset(symbol: str) -> AssetSnapshot:
    return AssetSnapshot(
        display_symbol=symbol,
        group="crypto",
        decimals=2,
        price=100.0,
        change_1h_pct=None,
        change_24h_pct=None,
        change_7d_pct=None,
        sparkline=[99.0, 100.0],
        regime=Regime(label="trend_up", vol_regime="normal", reason="test"),
        levels=[],
        forecast=ForecastMetrics(
            p_up_24h=0.6,
            q10=95.0,
            q50=100.0,
            q90=105.0,
            p_vol_expansion=0.2,
            band_width_pct=0.1,
            n_paths=30,
            model_name="fake-model",
            generated_at_utc=BASE_TS,
            last_closed_bar_ts_utc=BASE_TS,
        ),
        calibration=CalibrationStats(
            n_observations=0, brier_score=None, mae_q50=None, band_coverage=None, sufficient_data=False
        ),
        setup=None,
        source_status=DataSourceStatus(
            source_name="binance",
            last_update_utc=BASE_TS,
            is_stale=False,
            error_count_last_hour=0,
            market_session_open=True,
        ),
    )


def _snapshot() -> SnapshotDTO:
    return SnapshotDTO(
        generated_at_utc=BASE_TS,
        correlation_id="test-correlation-id",
        assets=[_asset("BTC/USDT")],
    )


def test_healthz_never_needs_a_snapshot(tmp_path: Path) -> None:
    client = TestClient(create_app(lambda: None, web_dir=tmp_path))
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "ready": True, "detail": None}


def test_healthz_reflects_injected_readiness_state(tmp_path: Path) -> None:
    readiness = ReadinessState()  # defaults to not-ready, status="starting"
    client = TestClient(create_app(lambda: None, web_dir=tmp_path, readiness=readiness))

    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "starting", "ready": False, "detail": None}

    readiness.update(status="backfilling", ready=False)
    assert client.get("/healthz").json()["status"] == "backfilling"

    readiness.update(status="error", ready=False, detail="boom")
    body = client.get("/healthz").json()
    assert body["status"] == "error"
    assert body["ready"] is False
    assert body["detail"] == "boom"

    readiness.update(status="ok", ready=True)
    assert client.get("/healthz").json() == {"status": "ok", "ready": True, "detail": None}


def test_snapshot_endpoint_returns_503_when_none_yet(tmp_path: Path) -> None:
    client = TestClient(create_app(lambda: None, web_dir=tmp_path))
    resp = client.get("/api/snapshot")
    assert resp.status_code == 503


def test_snapshot_endpoint_returns_the_provided_dto(tmp_path: Path) -> None:
    dto = _snapshot()
    client = TestClient(create_app(lambda: dto, web_dir=tmp_path))
    resp = client.get("/api/snapshot")
    assert resp.status_code == 200
    body = resp.json()
    assert body["correlation_id"] == "test-correlation-id"
    assert body["assets"][0]["display_symbol"] == "BTC/USDT"


def test_asset_endpoint_returns_matching_symbol_with_slash(tmp_path: Path) -> None:
    dto = _snapshot()
    client = TestClient(create_app(lambda: dto, web_dir=tmp_path))
    resp = client.get("/api/asset/BTC/USDT")
    assert resp.status_code == 200
    assert resp.json()["display_symbol"] == "BTC/USDT"


def test_asset_endpoint_404_for_unknown_symbol(tmp_path: Path) -> None:
    dto = _snapshot()
    client = TestClient(create_app(lambda: dto, web_dir=tmp_path))
    resp = client.get("/api/asset/DOES-NOT-EXIST")
    assert resp.status_code == 404


def test_asset_endpoint_503_when_no_snapshot_yet(tmp_path: Path) -> None:
    client = TestClient(create_app(lambda: None, web_dir=tmp_path))
    resp = client.get("/api/asset/BTC/USDT")
    assert resp.status_code == 503


def test_snapshot_file_store_roundtrip(tmp_path: Path) -> None:
    store = SnapshotFileStore(tmp_path / "snapshot.json")
    assert store.load() is None
    dto = _snapshot()
    store.save(dto)
    loaded = store.load()
    assert loaded == dto


@pytest.mark.parametrize("path", ["/index.html"])
def test_static_web_files_are_served_when_present(tmp_path: Path, path: str) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>kmd</title>", encoding="utf-8")
    client = TestClient(create_app(lambda: None, web_dir=tmp_path))
    resp = client.get(path)
    assert resp.status_code == 200
    assert "kmd" in resp.text
