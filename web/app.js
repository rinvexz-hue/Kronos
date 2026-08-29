"use strict";

// Kronos Market Desk — vanilla JS frontend. No build step, no framework.
// Polls GET /api/snapshot every 30s. All labels/number formatting are
// Dutch (NL); identifiers/comments stay in English.

const POLL_INTERVAL_MS = 30000;

const REGIME_LABELS_NL = {
  trend_up: "Stijgende trend",
  trend_down: "Dalende trend",
  range: "Zijwaarts",
  unknown: "Onbekend",
};

const VOL_REGIME_LABELS_NL = {
  low: "lage volatiliteit",
  normal: "normale volatiliteit",
  high: "hoge volatiliteit",
};

const LEVEL_KIND_LABELS_NL = {
  swing_high: "Swing high",
  swing_low: "Swing low",
  pdh: "Vorige dag hoog",
  pdl: "Vorige dag laag",
  ma_cluster: "MA-cluster",
  round_number: "Rond getal",
};

const DIRECTION_LABELS_NL = {
  long: "Long",
  short: "Short",
};

/** @type {SnapshotDTO | null} */
let latestSnapshot = null;
/** @type {string | null} */
let selectedSymbol = null;

function fmtNumber(value, decimals) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("nl-NL", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value);
}

function fmtPercent(value, decimals = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return (
    new Intl.NumberFormat("nl-NL", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    }).format(value * 100) + "%"
  );
}

function fmtSignedPercent(value, decimals = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return sign + fmtPercentPoints(value, decimals);
}

function fmtPercentPoints(value, decimals) {
  return (
    new Intl.NumberFormat("nl-NL", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    }).format(value) + "%"
  );
}

function fmtDateTime(isoString) {
  if (!isoString) return "—";
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return "—";
  // Explicit timeZone: the API always returns UTC-aware ISO timestamps: the
  // presentation layer is the ONLY place that should convert to local time,
  // and it must do so to a fixed zone (Europe/Amsterdam), not whatever
  // timezone the viewing browser/OS happens to be set to (a laptop with its
  // OS clock set to another region, a remote/CI preview, etc. would
  // otherwise silently mislabel every displayed timestamp).
  return new Intl.DateTimeFormat("nl-NL", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Europe/Amsterdam",
  }).format(date);
}

function changeClass(value) {
  if (value === null || value === undefined) return "neutral";
  if (value > 0) return "up";
  if (value < 0) return "down";
  return "neutral";
}

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else {
      node.setAttribute(key, value);
    }
  }
  for (const child of children || []) {
    if (child) node.appendChild(child);
  }
  return node;
}

function svgEl(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    node.setAttribute(key, value);
  }
  return node;
}

function buildSparklinePath(values, width, height, padding = 2) {
  if (!values || values.length < 2) return "";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const stepX = (width - 2 * padding) / (values.length - 1);
  return values
    .map((v, i) => {
      const x = padding + i * stepX;
      const y = height - padding - ((v - min) / span) * (height - 2 * padding);
      return `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
}

function renderTileSparkline(values) {
  const width = 200;
  const height = 32;
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, class: "tile-sparkline" });
  const path = buildSparklinePath(values, width, height);
  if (path) {
    const lastUp = values[values.length - 1] >= values[0];
    svg.appendChild(
      svgEl("path", {
        d: path,
        fill: "none",
        stroke: lastUp ? "var(--up)" : "var(--down)",
        "stroke-width": "1.5",
      })
    );
  }
  return svg;
}

function renderTile(asset) {
  const badge = el("span", { class: `badge ${asset.regime.label}`, text: REGIME_LABELS_NL[asset.regime.label] || asset.regime.label });

  const header = el("div", { class: "tile-header" }, [
    el("span", { class: "tile-symbol", text: asset.display_symbol }),
    el("span", { class: "tile-group", text: asset.group }),
  ]);

  const changes = el("div", { class: "tile-changes" }, [
    el("span", { class: changeClass(asset.change_1h_pct), text: `1u ${fmtSignedPercent(asset.change_1h_pct)}` }),
    el("span", { class: changeClass(asset.change_24h_pct), text: `24u ${fmtSignedPercent(asset.change_24h_pct)}` }),
    el("span", { class: changeClass(asset.change_7d_pct), text: `7d ${fmtSignedPercent(asset.change_7d_pct)}` }),
  ]);

  const metrics = el("div", { class: "tile-metrics" }, [
    el("span", { text: `P(stijging) ${fmtPercent(asset.forecast.p_up_24h)}` }),
    el("span", { text: `band ${fmtPercent(asset.forecast.band_width_pct)}` }),
  ]);

  const tile = el(
    "button",
    {
      class: "tile",
      type: "button",
      onclick: () => showDetail(asset.display_symbol),
      "aria-label": `Details voor ${asset.display_symbol}`,
    },
    [
      header,
      el("div", { class: "tile-price", text: fmtNumber(asset.price, asset.decimals) }),
      changes,
      badge,
      renderTileSparkline(asset.sparkline),
      metrics,
    ]
  );
  return tile;
}

function renderGrid(snapshot) {
  const grid = document.getElementById("tile-grid");
  grid.innerHTML = "";
  for (const asset of snapshot.assets) {
    grid.appendChild(renderTile(asset));
  }
  document.getElementById("empty-state").hidden = snapshot.assets.length > 0;
}

function renderStatusBar(snapshot) {
  const bar = document.getElementById("status-bar");
  bar.innerHTML = "";
  const bySource = new Map();
  for (const asset of snapshot.assets) {
    const status = asset.source_status;
    if (!bySource.has(status.source_name)) bySource.set(status.source_name, status);
  }
  for (const status of bySource.values()) {
    const sessionLabel =
      status.market_session_open === null || status.market_session_open === undefined
        ? ""
        : status.market_session_open
          ? " · open"
          : " · gesloten";
    const chip = el("span", { class: `status-chip${status.is_stale ? " stale" : ""}` }, [
      el("span", { class: "dot" }),
      el("span", {
        text: `${status.source_name} · ${fmtDateTime(status.last_update_utc)}${sessionLabel}${
          status.error_count_last_hour > 0 ? ` · ${status.error_count_last_hour} fout(en)` : ""
        }`,
      }),
    ]);
    bar.appendChild(chip);
  }
}

function renderFanChart(asset) {
  const width = 320;
  const height = 180;
  const padding = 24;
  const history = asset.sparkline || [];
  const forecast = asset.forecast;
  const allValues = [...history, forecast.q10, forecast.q50, forecast.q90];
  const min = Math.min(...allValues);
  const max = Math.max(...allValues);
  const span = max - min || 1;

  const historyStepX = history.length > 1 ? (width * 0.65 - padding) / (history.length - 1) : 0;
  const xAt = (i) => padding + i * historyStepX;
  const yAt = (v) => height - padding - ((v - min) / span) * (height - 2 * padding);
  const horizonX = width * 0.65 + 20;

  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}` });

  // history line
  if (history.length > 1) {
    const historyPath = history
      .map((v, i) => `${i === 0 ? "M" : "L"}${xAt(i).toFixed(2)},${yAt(v).toFixed(2)}`)
      .join(" ");
    svg.appendChild(svgEl("path", { d: historyPath, fill: "none", stroke: "var(--text)", "stroke-width": "1.75" }));
  }

  const lastPoint = history.length ? [xAt(history.length - 1), yAt(history[history.length - 1])] : [padding, yAt(forecast.q50)];

  // shaded fan band (q10..q90)
  const fanPath = [
    `M${lastPoint[0].toFixed(2)},${lastPoint[1].toFixed(2)}`,
    `L${horizonX.toFixed(2)},${yAt(forecast.q90).toFixed(2)}`,
    `L${horizonX.toFixed(2)},${yAt(forecast.q10).toFixed(2)}`,
    "Z",
  ].join(" ");
  svg.appendChild(svgEl("path", { d: fanPath, fill: "rgba(91,140,255,0.18)", stroke: "none" }));

  // q50 projection line
  svg.appendChild(
    svgEl("line", {
      x1: lastPoint[0].toFixed(2),
      y1: lastPoint[1].toFixed(2),
      x2: horizonX.toFixed(2),
      y2: yAt(forecast.q50).toFixed(2),
      stroke: "var(--accent)",
      "stroke-width": "2",
      "stroke-dasharray": "4 3",
    })
  );

  // markers
  for (const [value, color] of [
    [forecast.q10, "var(--down)"],
    [forecast.q50, "var(--accent)"],
    [forecast.q90, "var(--up)"],
  ]) {
    svg.appendChild(svgEl("circle", { cx: horizonX.toFixed(2), cy: yAt(value).toFixed(2), r: "3", fill: color }));
  }

  return svg;
}

function renderLevels(levels, decimals) {
  if (!levels.length) {
    return el("p", { class: "reason-text", text: "Geen niveaus met een traceerbare oorsprong gevonden." });
  }
  const rows = levels
    .slice()
    .sort((a, b) => b.price - a.price)
    .map((lvl) =>
      el("div", { class: "level-row" }, [
        el("span", { class: "level-price", text: fmtNumber(lvl.price, decimals) }),
        el("span", { class: "level-meta" }, [
          el("span", { class: "level-kind", text: LEVEL_KIND_LABELS_NL[lvl.kind] || lvl.kind }),
          el("br"),
          el("span", { text: lvl.reason }),
        ]),
      ])
    );
  return el("div", {}, rows);
}

function renderCalibration(calibration) {
  if (!calibration.sufficient_data) {
    return el("p", {
      class: "calibration-insufficient",
      text: `Onvoldoende data voor kalibratie (${calibration.n_observations} van minimaal 30 metingen).`,
    });
  }
  const stat = (label, value) => el("div", { class: "calibration-stat" }, [
    el("div", { class: "value", text: value }),
    el("div", { class: "label", text: label }),
  ]);
  return el("div", { class: "calibration-grid" }, [
    stat("Brier-score", fmtNumber(calibration.brier_score, 3)),
    stat("MAE (q50)", fmtNumber(calibration.mae_q50, 2)),
    stat("Banddekking", fmtPercent(calibration.band_coverage)),
  ]);
}

function renderSetup(setup, decimals) {
  if (!setup) {
    return el("p", { class: "no-setup", text: "Geen setup: risico/rendement onder de drempel of geen geldig niveau." });
  }
  const row = (label, value) => el("div", {}, [
    el("div", { class: "label", style: "color:var(--text-dim);font-size:0.7rem;", text: label }),
    el("div", { text: value }),
  ]);
  return el("div", { class: "setup-card" }, [
    row("Richting", DIRECTION_LABELS_NL[setup.direction] || setup.direction),
    row("R:R", fmtNumber(setup.rr, 1)),
    row("Entry", fmtNumber(setup.entry, decimals)),
    row("Invalidatie", fmtNumber(setup.invalidation, decimals)),
    row("Target", fmtNumber(setup.target, decimals)),
    row("Risico", fmtPercentPoints(setup.risk_pct, 1)),
  ]);
}

function renderDetail(asset) {
  const container = document.getElementById("detail-content");
  container.innerHTML = "";

  container.appendChild(
    el("div", { class: "detail-header" }, [
      el("div", {}, [
        el("div", { class: "tile-symbol", text: asset.display_symbol }),
        el("span", { class: `badge ${asset.regime.label}`, text: REGIME_LABELS_NL[asset.regime.label] || asset.regime.label }),
      ]),
      el("div", { class: "detail-price", text: fmtNumber(asset.price, asset.decimals) }),
    ])
  );

  const changesSection = el("div", { class: "detail-section" }, [
    el("h3", { text: "Koersverandering" }),
    el("div", { class: "tile-changes" }, [
      el("span", { class: changeClass(asset.change_1h_pct), text: `1u ${fmtSignedPercent(asset.change_1h_pct)}` }),
      el("span", { class: changeClass(asset.change_24h_pct), text: `24u ${fmtSignedPercent(asset.change_24h_pct)}` }),
      el("span", { class: changeClass(asset.change_7d_pct), text: `7d ${fmtSignedPercent(asset.change_7d_pct)}` }),
    ]),
  ]);

  const forecast = asset.forecast;
  const chartSection = el("div", { class: "detail-section" }, [
    el("h3", { text: "Koershistorie en prognosewaaier" }),
    el("div", { class: "chart-wrap" }, [renderFanChart(asset)]),
    el("div", { class: "chart-legend" }, [
      el("span", {}, [el("span", { class: "swatch", style: "background:var(--down)" }), document.createTextNode(`q10 ${fmtNumber(forecast.q10, asset.decimals)}`)]),
      el("span", {}, [el("span", { class: "swatch", style: "background:var(--accent)" }), document.createTextNode(`q50 ${fmtNumber(forecast.q50, asset.decimals)}`)]),
      el("span", {}, [el("span", { class: "swatch", style: "background:var(--up)" }), document.createTextNode(`q90 ${fmtNumber(forecast.q90, asset.decimals)}`)]),
    ]),
    el("p", {
      class: "reason-text",
      text: `P(stijging) over horizon: ${fmtPercent(forecast.p_up_24h)} · bandbreedte: ${fmtPercent(
        forecast.band_width_pct
      )} · kans op volatiliteitstoename: ${fmtPercent(forecast.p_vol_expansion)}`,
    }),
    el("p", {
      class: "reason-text",
      text: `Model: ${forecast.model_name} · ${forecast.n_paths} Monte Carlo paden · gegenereerd ${fmtDateTime(
        forecast.generated_at_utc
      )} · laatst gesloten candle ${fmtDateTime(forecast.last_closed_bar_ts_utc)}`,
    }),
  ]);

  const regimeSection = el("div", { class: "detail-section" }, [
    el("h3", { text: "Regime" }),
    el("p", { class: "reason-text", text: `${REGIME_LABELS_NL[asset.regime.label]} · ${VOL_REGIME_LABELS_NL[asset.regime.vol_regime]}` }),
    el("p", { class: "reason-text", text: asset.regime.reason }),
  ]);

  const levelsSection = el("div", { class: "detail-section" }, [
    el("h3", { text: "Niveaus" }),
    renderLevels(asset.levels, asset.decimals),
  ]);

  const setupSection = el("div", { class: "detail-section" }, [
    el("h3", { text: "Setup" }),
    renderSetup(asset.setup, asset.decimals),
  ]);

  const calibrationSection = el("div", { class: "detail-section" }, [
    el("h3", { text: "Kalibratie" }),
    renderCalibration(asset.calibration),
  ]);

  const status = asset.source_status;
  const sourceSection = el("div", { class: "detail-section" }, [
    el("h3", { text: "Databron" }),
    el("div", { class: "source-row" }, [
      el("span", { text: status.source_name }),
      el("span", { text: status.is_stale ? "verouderd" : "actueel" }),
    ]),
    el("div", { class: "source-row" }, [
      el("span", { text: "Laatste update" }),
      el("span", { text: fmtDateTime(status.last_update_utc) }),
    ]),
    el("div", { class: "source-row" }, [
      el("span", { text: "Sessie" }),
      el("span", {
        text:
          status.market_session_open === null || status.market_session_open === undefined
            ? "onbekend"
            : status.market_session_open
              ? "open"
              : "gesloten",
      }),
    ]),
    el("div", { class: "source-row" }, [
      el("span", { text: "Fouten (laatste uur)" }),
      el("span", { text: String(status.error_count_last_hour) }),
    ]),
  ]);

  container.append(changesSection, chartSection, regimeSection, levelsSection, setupSection, calibrationSection, sourceSection);
}

function showDetail(symbol) {
  selectedSymbol = symbol;
  const asset = latestSnapshot?.assets.find((a) => a.display_symbol === symbol);
  if (!asset) return;
  renderDetail(asset);
  document.getElementById("grid-view").hidden = true;
  document.getElementById("detail-view").hidden = false;
  window.scrollTo(0, 0);
}

function showGrid() {
  selectedSymbol = null;
  document.getElementById("detail-view").hidden = true;
  document.getElementById("grid-view").hidden = false;
}

function applySnapshot(snapshot) {
  latestSnapshot = snapshot;
  document.getElementById("generated-at").textContent = `bijgewerkt ${fmtDateTime(snapshot.generated_at_utc)}`;
  document.getElementById("error-state").hidden = true;
  renderStatusBar(snapshot);
  renderGrid(snapshot);
  if (selectedSymbol) {
    const asset = snapshot.assets.find((a) => a.display_symbol === selectedSymbol);
    if (asset) renderDetail(asset);
    else showGrid();
  }
}

async function fetchSnapshot() {
  try {
    const resp = await fetch("/api/snapshot", { cache: "no-store" });
    if (resp.status === 503) {
      document.getElementById("empty-state").hidden = false;
      return;
    }
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const snapshot = await resp.json();
    applySnapshot(snapshot);
  } catch (err) {
    const errorState = document.getElementById("error-state");
    errorState.hidden = false;
    errorState.textContent = `Kan geen verbinding maken met de server: ${err.message}`;
  }
}

document.getElementById("back-button").addEventListener("click", showGrid);

fetchSnapshot();
setInterval(fetchSnapshot, POLL_INTERVAL_MS);
