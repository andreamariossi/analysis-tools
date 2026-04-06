#!/usr/bin/env python3
"""
Gel Picker simple — Web Interface
==============================
Run:   python app.py gel1.tif gel2.tif
Open:  http://localhost:5050

New in v3
---------
• Locked box size — draw one box, every subsequent box snaps to the same
  width × height (toggle per box type: band / norm)
• Results page — interactive bar/line chart with error bars, fold-change
  overlay, download PNG button
"""

import sys, io, base64, json, warnings
from pathlib import Path

import numpy as np
from PIL import Image
from flask import Flask, render_template_string, request, jsonify
from scipy.ndimage import grey_erosion, grey_dilation
from scipy.integrate import trapezoid
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

warnings.filterwarnings("ignore")

# ── Analysis helpers ──────────────────────────────────────────────────────────

def load_gel(path):
    arr = np.array(Image.open(path))
    if arr.ndim == 3:
        arr = arr.mean(axis=2)
    return arr.astype(np.float32)

def rolling_ball(profile, radius=50):
    d = 2 * radius + 1
    s = np.ones(d)
    bg = grey_erosion(profile, structure=s)
    bg = grey_dilation(bg, structure=s)
    return np.minimum(bg, profile)

def integrate_box(arr, box):
    """Integrate bg-subtracted signal inside (x0,y0,x1,y1) box."""
    H, W = arr.shape
    x0, y0, x1, y1 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
    x0, x1 = sorted([max(0, x0), min(W-1, x1)])
    y0, y1 = sorted([max(0, y0), min(H-1, y1)])
    inv   = (255 - arr) / 255.0
    strip = inv[y0:y1+1, x0:x1+1]
    prof  = strip.mean(axis=1)
    bg    = rolling_ball(prof, radius=max(8, len(prof)//3))
    clean = np.maximum(prof - bg, 0)
    return float(trapezoid(clean)), clean, x0, y0, x1, y1

def lane_total(arr, x0, x1):
    H, W = arr.shape
    x0, x1 = max(0, int(x0)), min(W-1, int(x1))
    inv  = (255 - arr) / 255.0
    prof = inv[:, x0:x1+1].mean(axis=1)
    bg   = rolling_ball(prof, radius=50)
    return float(trapezoid(np.maximum(prof - bg, 0)))

def arr_to_b64(arr):
    fig, ax = plt.subplots(figsize=(arr.shape[1]/100, arr.shape[0]/100), dpi=100)
    ax.imshow(arr, cmap="gray", aspect="auto",
              vmin=arr.min(), vmax=arr.max())
    ax.axis("off")
    fig.subplots_adjust(0, 0, 1, 1)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0)
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()

def fig_to_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor(),
                bbox_inches="tight", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()

def build_df(arr, lanes):
    rows = []
    for i, lane in enumerate(lanes):
        area_band, _, x0, y0, x1, y1 = integrate_box(arr, lane["band"])
        total     = lane_total(arr, x0, x1)
        norm_area = None
        if lane.get("norm"):
            norm_area, *_ = integrate_box(arr, lane["norm"])
        rows.append({
            "lane":               i + 1,
            "band_x0": x0, "band_y0": y0, "band_x1": x1, "band_y1": y1,
            "area_raw":           round(area_band, 5),
            "lane_total_int":     round(total, 5),
            "norm_area":          round(norm_area, 5) if norm_area else None,
            "area_norm_total":    round(area_band / total, 6) if total else None,
            "area_norm_loadctrl": round(area_band / norm_area, 6)
                                  if norm_area and norm_area > 0 else None,
        })
    df = pd.DataFrame(rows)
    ref_t = df["area_norm_total"].iloc[0]
    df["fc_vs_L1_total"] = (df["area_norm_total"] / ref_t).round(4)
    if df["area_norm_loadctrl"].notna().any():
        ref_lc = df["area_norm_loadctrl"].iloc[0]
        df["fc_vs_L1_loadctrl"] = (df["area_norm_loadctrl"] / ref_lc).round(4)
    else:
        df["fc_vs_L1_loadctrl"] = None
    return df

# ── Flask app ─────────────────────────────────────────────────────────────────

app  = Flask(__name__)
GELS = {}

def init_gels(paths):
    for i, p in enumerate(paths):
        arr = load_gel(p)
        GELS[i] = {
            "name": Path(p).stem,
            "path": str(p),
            "arr":  arr,
            "b64":  arr_to_b64(arr),
            "h":    int(arr.shape[0]),
            "w":    int(arr.shape[1]),
        }

# ─────────────────────────────────────────────────────────────────────────────
# HTML — two pages (picker + results) as tabs, no page reload
# ─────────────────────────────────────────────────────────────────────────────

HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Gel Band Picker v3</title>
<style>
/* ── Reset & tokens ─────────────────────────────────────────────────────── */
:root{
  --bg:#0f1117; --panel:#1a1d27; --panel2:#141620;
  --border:#2e3245; --text:#e2e8f0; --muted:#8892a4;
  --blue:#4fc3f7; --green:#81c784; --amber:#ffb74d;
  --purple:#ce93d8; --danger:#ef5350;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);
     font-family:'Segoe UI',system-ui,sans-serif;
     font-size:13px;height:100vh;display:flex;flex-direction:column;overflow:hidden}

/* ── Top nav ────────────────────────────────────────────────────────────── */
nav{background:var(--panel);border-bottom:1px solid var(--border);
    padding:0 18px;display:flex;align-items:stretch;gap:0;flex-shrink:0;height:44px}
nav h1{font-size:14px;font-weight:700;color:var(--blue);
       display:flex;align-items:center;padding-right:20px;
       border-right:1px solid var(--border);margin-right:8px}
.nav-tab{padding:0 16px;border:none;background:transparent;color:var(--muted);
         font-size:12.5px;font-weight:500;cursor:pointer;
         border-bottom:2px solid transparent;transition:all .15s;
         display:flex;align-items:center;gap:6px}
.nav-tab.active{color:var(--blue);border-bottom-color:var(--blue)}
.nav-tab:hover:not(.active){color:var(--text)}
#gel-tabs{display:flex;gap:6px;margin-left:auto;align-items:center}
.gel-tab{padding:3px 10px;border-radius:6px;border:1px solid var(--border);
         cursor:pointer;background:transparent;color:var(--muted);font-size:11.5px;transition:all .15s}
.gel-tab.active{background:var(--blue);color:#000;border-color:var(--blue);font-weight:600}

/* ── Pages ──────────────────────────────────────────────────────────────── */
.page{display:none;flex:1;overflow:hidden}
.page.active{display:flex}

/* ═══════════════════════════════════════════════════════════════════════════
   PAGE 1 — Picker
═══════════════════════════════════════════════════════════════════════════ */
#page-picker{flex-direction:row}
#canvas-wrap{flex:1 1 60%;position:relative;overflow:hidden;
             background:#050507;display:flex;align-items:center;justify-content:center}
#gel-canvas{cursor:crosshair;display:block;max-width:100%;max-height:100%}

#sidebar{width:360px;flex-shrink:0;background:var(--panel);
         border-left:1px solid var(--border);
         display:flex;flex-direction:column;overflow:hidden}
#controls{padding:14px;display:flex;flex-direction:column;gap:10px;flex-shrink:0}

.section-label{font-size:10px;font-weight:700;letter-spacing:.08em;
               color:var(--muted);text-transform:uppercase;margin-bottom:4px}

/* Mode buttons */
.mode-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.mode-btn{padding:7px 8px;border-radius:8px;border:1px solid var(--border);
          cursor:pointer;background:transparent;color:var(--muted);
          font-size:11.5px;text-align:center;transition:all .15s;line-height:1.3}
.mode-btn.active-band{background:rgba(79,195,247,.15);border-color:var(--blue);
                       color:var(--blue);font-weight:600}
.mode-btn.active-norm{background:rgba(129,199,132,.15);border-color:var(--green);
                       color:var(--green);font-weight:600}

/* Lock row */
.lock-row{display:flex;align-items:center;gap:8px;
          padding:7px 10px;border-radius:8px;background:var(--panel2);
          border:1px solid var(--border)}
.lock-row label{font-size:11.5px;color:var(--muted);display:flex;align-items:center;gap:5px;cursor:pointer}
.lock-row input[type=checkbox]{accent-color:var(--amber);width:13px;height:13px}
.lock-row .dims{font-size:10.5px;color:var(--amber);font-family:monospace;margin-left:auto}

/* Generic buttons */
.btn-row{display:flex;gap:6px;flex-wrap:wrap}
.btn{padding:6px 12px;border-radius:7px;border:1px solid var(--border);
     cursor:pointer;font-size:12px;font-weight:500;transition:all .15s}
.btn-primary{background:var(--blue);  color:#000;border-color:var(--blue)}
.btn-success{background:var(--green); color:#000;border-color:var(--green)}
.btn-purple {background:var(--purple);color:#000;border-color:var(--purple)}
.btn-ghost  {background:transparent;  color:var(--muted)}
.btn-ghost:hover{background:var(--border);color:var(--text)}
.btn-danger {background:transparent;  color:var(--danger);border-color:var(--danger)}

#status{padding:8px 10px;background:rgba(79,195,247,.08);
        border:1px solid rgba(79,195,247,.25);border-radius:8px;
        font-size:11.5px;color:var(--blue);min-height:36px;line-height:1.5}

/* Legend */
.legend{display:flex;gap:14px;flex-wrap:wrap}
.legend-item{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--muted)}
.swatch{width:13px;height:13px;border-radius:3px;flex-shrink:0}

/* Lane table */
#lane-table-wrap{flex:1;overflow-y:auto;padding:0 14px 10px}
table{width:100%;border-collapse:collapse;font-size:11px}
th{color:var(--muted);font-weight:600;font-size:10px;letter-spacing:.05em;
   text-transform:uppercase;padding:5px 5px;border-bottom:1px solid var(--border);text-align:left}
td{padding:4px 5px;border-bottom:1px solid rgba(46,50,69,.5);vertical-align:middle}
tr:hover td{background:rgba(255,255,255,.03)}
.tag{display:inline-block;padding:1px 5px;border-radius:4px;font-size:10px;font-weight:600}
.tag-band{background:rgba(79,195,247,.2); color:var(--blue)}
.tag-norm{background:rgba(129,199,132,.2);color:var(--green)}
.tag-miss{background:rgba(239,83,80,.12); color:var(--danger)}
.del-btn{background:none;border:none;cursor:pointer;color:var(--danger);
         font-size:13px;padding:0 3px;opacity:.55}
.del-btn:hover{opacity:1}

#profile-img{width:100%;display:block;border-top:1px solid var(--border);flex-shrink:0}

/* ═══════════════════════════════════════════════════════════════════════════
   PAGE 2 — Results
═══════════════════════════════════════════════════════════════════════════ */
#page-results{flex-direction:column;overflow-y:auto}
#results-inner{padding:24px;display:flex;flex-direction:column;gap:24px;max-width:1200px;margin:0 auto;width:100%}

.results-header{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.results-header h2{font-size:18px;font-weight:600;color:var(--text)}
.results-header p{color:var(--muted);font-size:12.5px}

/* Chart options row */
.chart-opts{display:flex;gap:10px;align-items:center;flex-wrap:wrap;
            padding:12px 16px;background:var(--panel);border-radius:10px;
            border:1px solid var(--border)}
.chart-opts label{font-size:12px;color:var(--muted);display:flex;align-items:center;gap:5px;cursor:pointer}
.chart-opts select{background:var(--panel2);color:var(--text);border:1px solid var(--border);
                   border-radius:6px;padding:4px 8px;font-size:12px}
.chart-opts input[type=checkbox]{accent-color:var(--blue)}

/* Chart panels */
.charts-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.chart-card{background:var(--panel);border:1px solid var(--border);
            border-radius:12px;padding:16px;display:flex;flex-direction:column;gap:10px}
.chart-card h3{font-size:13px;font-weight:600;color:var(--text)}
.chart-card img{width:100%;border-radius:6px;display:block}
.chart-card .dl-btn{align-self:flex-end}

/* Data table */
.data-table-wrap{background:var(--panel);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.data-table-wrap h3{padding:14px 16px;font-size:13px;font-weight:600;border-bottom:1px solid var(--border)}
.data-table-wrap table{font-size:11.5px}
.data-table-wrap th{background:var(--panel2);padding:8px 12px}
.data-table-wrap td{padding:7px 12px}
.num{font-family:monospace;text-align:right}
</style>
</head>
<body>

<!-- Top nav -->
<nav>
  <h1>🔬 Gel Picker v3</h1>
  <button class="nav-tab active" id="tab-picker"  onclick="showPage('picker')">📐 Picker</button>
  <button class="nav-tab"        id="tab-results" onclick="showPage('results')">📊 Results</button>
  <div id="gel-tabs">
    {% for i, g in gels.items() %}
    <button class="gel-tab {% if loop.first %}active{% endif %}"
            onclick="switchGel({{i}})">{{ g.name }}</button>
    {% endfor %}
  </div>
</nav>

<!-- ═══════════════ PAGE 1 — PICKER ═══════════════════════════════════════ -->
<div class="page active" id="page-picker">

  <div id="canvas-wrap">
    <canvas id="gel-canvas"></canvas>
  </div>

  <div id="sidebar">
    <div id="controls">

      <!-- Draw mode -->
      <div>
        <div class="section-label">Draw mode</div>
        <div class="mode-grid">
          <button class="mode-btn active-band" id="btn-band"
                  onclick="setMode('band')">🔵 Band box<br><small>quantify region</small></button>
          <button class="mode-btn" id="btn-norm-lane"
                  onclick="setMode('norm_lane')">🟢 Norm box<br><small>this lane</small></button>
          <button class="mode-btn" id="btn-norm-all"
                  onclick="setMode('norm_all')" style="grid-column:span 2">
            🟢 Norm box — all lanes &nbsp;<small>(one box → every lane)</small></button>
        </div>
      </div>

      <!-- Lock size — band -->
      <div>
        <div class="section-label">Box size lock</div>
        <div style="display:flex;flex-direction:column;gap:6px">
          <div class="lock-row" id="lock-band-row">
            <label>
              <input type="checkbox" id="lock-band" onchange="toggleLock('band')">
              Lock band box size
            </label>
            <span class="dims" id="lock-band-dims">—</span>
            <button class="btn btn-ghost" style="padding:3px 8px;font-size:11px"
                    onclick="resetLock('band')">reset</button>
          </div>
          <div class="lock-row" id="lock-norm-row">
            <label>
              <input type="checkbox" id="lock-norm" onchange="toggleLock('norm')">
              Lock norm box size
            </label>
            <span class="dims" id="lock-norm-dims">—</span>
            <button class="btn btn-ghost" style="padding:3px 8px;font-size:11px"
                    onclick="resetLock('norm')">reset</button>
          </div>
        </div>
      </div>

      <!-- Actions -->
      <div class="btn-row">
        <button class="btn btn-primary" onclick="quantify()">Quantify ▶</button>
        <button class="btn btn-success" onclick="saveCSV()">Save CSV</button>
        <button class="btn btn-ghost"   onclick="saveFig()">Save PNG</button>
        <button class="btn btn-danger"  onclick="clearAll()">Clear all</button>
      </div>

      <div id="status">Draw a <b>band box</b> by clicking and dragging on the gel.</div>

      <!-- Legend -->
      <div class="legend">
        <div class="legend-item">
          <div class="swatch" style="background:rgba(79,195,247,.5);border:1.5px solid #4fc3f7"></div>Band box
        </div>
        <div class="legend-item">
          <div class="swatch" style="background:rgba(129,199,132,.5);border:1.5px solid #81c784"></div>Norm box
        </div>
        <div class="legend-item">
          <div class="swatch" style="background:rgba(255,183,77,.4);border:1.5px solid #ffb74d"></div>Locked size
        </div>
      </div>

    </div><!-- /controls -->

    <!-- Lane table -->
    <div id="lane-table-wrap">
      <div class="section-label" style="padding:0 0 6px">Lanes</div>
      <table>
        <thead><tr><th>#</th><th>Band box (px)</th><th>Norm</th><th></th></tr></thead>
        <tbody id="lane-tbody"></tbody>
      </table>
    </div>

    <img id="profile-img" src="" style="display:none">
  </div><!-- /sidebar -->
</div><!-- /page-picker -->


<!-- ═══════════════ PAGE 2 — RESULTS ═══════════════════════════════════════ -->
<div class="page" id="page-results">
  <div id="results-inner">

    <div class="results-header">
      <h2>Results</h2>
      <p id="results-subtitle">Run Quantify on the Picker page first.</p>
    </div>

    <!-- Chart options -->
    <div class="chart-opts">
      <label>
        Normalisation:
        <select id="norm-select" onchange="refreshResults()">
          <option value="total">÷ total lane intensity</option>
          <option value="loadctrl">÷ norm box (loading control)</option>
        </select>
      </label>
      <label>
        <input type="checkbox" id="show-fc" onchange="refreshResults()" checked>
        Show fold-change
      </label>
      <label>
        <input type="checkbox" id="show-line" onchange="refreshResults()" checked>
        Line plot
      </label>
      <label>
        Y-axis label: <input id="ylabel-input" type="text"
          value="Band intensity (normalised)"
          style="background:var(--panel2);color:var(--text);border:1px solid var(--border);
                 border-radius:6px;padding:3px 8px;font-size:12px;width:200px"
          onchange="refreshResults()">
      </label>
      <label>
        X-axis label: <input id="xlabel-input" type="text"
          value="Lane (time point)"
          style="background:var(--panel2);color:var(--text);border:1px solid var(--border);
                 border-radius:6px;padding:3px 8px;font-size:12px;width:160px"
          onchange="refreshResults()">
      </label>
    </div>

    <!-- Charts grid -->
    <div class="charts-grid">
      <div class="chart-card">
        <h3>Bar chart</h3>
        <img id="bar-img" src="" style="display:none">
        <span id="bar-placeholder" style="color:var(--muted);font-size:12px">Quantify first →</span>
        <button class="btn btn-ghost dl-btn" onclick="downloadImg('bar-img','bar_chart.png')"
                style="display:none" id="dl-bar">⬇ Download</button>
      </div>
      <div class="chart-card">
        <h3>Line plot</h3>
        <img id="line-img" src="" style="display:none">
        <span id="line-placeholder" style="color:var(--muted);font-size:12px">Quantify first →</span>
        <button class="btn btn-ghost dl-btn" onclick="downloadImg('line-img','line_plot.png')"
                style="display:none" id="dl-line">⬇ Download</button>
      </div>
    </div>

    <!-- Data table -->
    <div class="data-table-wrap" id="data-table-wrap" style="display:none">
      <h3>Data table</h3>
      <table>
        <thead id="data-thead"></thead>
        <tbody id="data-tbody"></tbody>
      </table>
    </div>

  </div>
</div><!-- /page-results -->


<script>
// ═══════════════════════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════════════════════
const GELS  = {{ gels_json | safe }};
let gelIdx  = 0;
let mode    = 'band';
let lanes   = {};   // {gelIdx: [{band:[x0,y0,x1,y1], norm:[...]|null}]}
for (const i in GELS) lanes[i] = [];

// Lock state per type
const lockState = {
  band: { locked: false, w: null, h: null },
  norm: { locked: false, w: null, h: null },
};

// Result data per gel
let resultData = {};   // {gelIdx: {df_rows, has_lc}}

// ═══════════════════════════════════════════════════════════════════════════
// Page switching
// ═══════════════════════════════════════════════════════════════════════════
function showPage(p) {
  document.querySelectorAll('.page').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(el => el.classList.remove('active'));
  document.getElementById('page-' + p).classList.add('active');
  document.getElementById('tab-' + p).classList.add('active');
  if (p === 'results') refreshResults();
}

// ═══════════════════════════════════════════════════════════════════════════
// Canvas / drawing
// ═══════════════════════════════════════════════════════════════════════════
const canvas = document.getElementById('gel-canvas');
const ctx    = canvas.getContext('2d');
let imgEl    = new Image();
let imgW = 1, imgH = 1, scale = 1;
let drag = null;   // {sx,sy,ex,ey}

function loadGelImage(idx) {
  imgEl = new Image();
  imgEl.onload = () => {
    imgW = imgEl.naturalWidth; imgH = imgEl.naturalHeight;
    fitCanvas(); redraw();
  };
  imgEl.src = 'data:image/png;base64,' + GELS[idx].b64;
}

function fitCanvas() {
  const wrap = document.getElementById('canvas-wrap');
  scale = Math.min(wrap.clientWidth / imgW, wrap.clientHeight / imgH, 1);
  canvas.width  = Math.round(imgW * scale);
  canvas.height = Math.round(imgH * scale);
}
window.addEventListener('resize', () => { fitCanvas(); redraw(); });

function redraw() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(imgEl, 0, 0, canvas.width, canvas.height);
  lanes[gelIdx].forEach((lane, i) => {
    drawBox(lane.band, '#4fc3f7', `L${i+1} band`);
    if (lane.norm) drawBox(lane.norm, '#81c784', `L${i+1} norm`);
  });
  if (drag) {
    const col = (mode === 'band') ? '#4fc3f7' : '#81c784';
    const box = snapToLock(drag.sx, drag.sy, drag.ex, drag.ey);
    drawRectRaw(...box.map(v => v * scale), col, 0.25);
  }
}

function drawBox(b, col, label) {
  if (!b) return;
  drawRectRaw(b[0]*scale, b[1]*scale, b[2]*scale, b[3]*scale, col, 0.2);
  ctx.fillStyle = col;
  ctx.font = 'bold 11px monospace';
  ctx.fillText(label, b[0]*scale + 3, b[1]*scale + 13);
}

function drawRectRaw(x0, y0, x1, y1, col, alpha) {
  ctx.strokeStyle = col; ctx.lineWidth = 2;
  ctx.strokeRect(x0, y0, x1-x0, y1-y0);
  ctx.fillStyle = col; ctx.globalAlpha = alpha;
  ctx.fillRect(x0, y0, x1-x0, y1-y0);
  ctx.globalAlpha = 1;
}

// ═══════════════════════════════════════════════════════════════════════════
// Lock logic
// ═══════════════════════════════════════════════════════════════════════════
function lockType() {
  return (mode === 'band') ? 'band' : 'norm';
}

function snapToLock(sx, sy, ex, ey) {
  const lt = lockType();
  const ls = lockState[lt];
  if (!ls.locked || ls.w === null) return [Math.min(sx,ex), Math.min(sy,ey),
                                            Math.max(sx,ex), Math.max(sy,ey)];
  // Anchor = start point, snap size
  return [sx, sy, sx + ls.w, sy + ls.h];
}

function toggleLock(type) {
  const ls = lockState[type];
  ls.locked = document.getElementById('lock-' + type).checked;
  updateLockDims(type);
  setStatus(ls.locked
    ? `${type} box locked to ${ls.w}×${ls.h} px — drag anywhere to stamp`
    : `${type} box unlocked — drag freely`, 'amber');
}

function resetLock(type) {
  lockState[type].w = null; lockState[type].h = null;
  lockState[type].locked = false;
  document.getElementById('lock-' + type).checked = false;
  document.getElementById('lock-' + type + '-dims').textContent = '—';
}

function updateLockDims(type) {
  const ls = lockState[type];
  const el = document.getElementById('lock-' + type + '-dims');
  el.textContent = (ls.w !== null) ? `${Math.round(ls.w)}×${Math.round(ls.h)} px` : '—';
}

// ═══════════════════════════════════════════════════════════════════════════
// Mouse events
// ═══════════════════════════════════════════════════════════════════════════
function canvasXY(e) {
  const r = canvas.getBoundingClientRect();
  return { x: (e.clientX - r.left) / scale, y: (e.clientY - r.top) / scale };
}

canvas.addEventListener('mousedown', e => {
  const {x, y} = canvasXY(e);
  drag = {sx: x, sy: y, ex: x, ey: y};
});

canvas.addEventListener('mousemove', e => {
  if (!drag) return;
  const {x, y} = canvasXY(e);
  drag.ex = x; drag.ey = y;
  redraw();
});

canvas.addEventListener('mouseup', e => {
  if (!drag) return;
  const {x, y} = canvasXY(e);
  drag.ex = x; drag.ey = y;

  let [x0, y0, x1, y1] = snapToLock(drag.sx, drag.sy, drag.ex, drag.ey);
  drag = null;

  // Tiny drag → ignore
  if (Math.abs(x1-x0) < 4 || Math.abs(y1-y0) < 4) { redraw(); return; }

  // Clamp to image
  x0 = Math.max(0, Math.min(imgW-1, x0));
  x1 = Math.max(0, Math.min(imgW-1, x1));
  y0 = Math.max(0, Math.min(imgH-1, y0));
  y1 = Math.max(0, Math.min(imgH-1, y1));
  const box = [x0, y0, x1, y1];
  const w = Math.abs(x1-x0), h = Math.abs(y1-y0);

  const lt  = lockType();
  const ls  = lockState[lt];

  // Learn size if not locked yet (first draw teaches the lock)
  if (!ls.locked || ls.w === null) {
    ls.w = w; ls.h = h;
    updateLockDims(lt);
  }

  const ls_arr = lanes[gelIdx];

  if (mode === 'band') {
    ls_arr.push({band: box, norm: null});
    setStatus(`Lane ${ls_arr.length} band box drawn. Draw its norm box or next band.`);
  } else if (mode === 'norm_lane') {
    let target = null;
    for (let i = ls_arr.length-1; i >= 0; i--) {
      if (!ls_arr[i].norm) { target = i; break; }
    }
    if (target === null) { setStatus('⚠ Draw a band box first.', 'amber'); redraw(); return; }
    ls_arr[target].norm = box;
    setStatus(`Norm box assigned to lane ${target+1}.`, 'green');
  } else {
    ls_arr.forEach(l => l.norm = box);
    setStatus(`Norm box applied to all ${ls_arr.length} lanes.`, 'green');
  }

  updateTable(); updateProfile(); redraw();
});

// ═══════════════════════════════════════════════════════════════════════════
// Mode
// ═══════════════════════════════════════════════════════════════════════════
function setMode(m) {
  mode = m;
  const cls = {band:'active-band', norm_lane:'active-norm', norm_all:'active-norm'};
  ['band','norm-lane','norm-all'].forEach(id => {
    document.getElementById('btn-'+id).className = 'mode-btn';
  });
  const active = {band:'btn-band', norm_lane:'btn-norm-lane', norm_all:'btn-norm-all'}[m];
  document.getElementById(active).className = 'mode-btn ' + cls[m];
  const hints = {
    band:      'Drag to draw a BAND box. First drag sets the locked size if lock is on.',
    norm_lane: 'Drag to draw a NORM box for the most recent lane.',
    norm_all:  'Drag to draw a NORM box applied to ALL lanes.',
  };
  setStatus(hints[m]);
}

// ═══════════════════════════════════════════════════════════════════════════
// Status
// ═══════════════════════════════════════════════════════════════════════════
function setStatus(msg, col='blue') {
  const el = document.getElementById('status');
  el.innerHTML = msg;
  const bg = {blue:'rgba(79,195,247,.08)',green:'rgba(129,199,132,.08)',amber:'rgba(255,183,77,.08)'};
  const bd = {blue:'rgba(79,195,247,.25)',green:'rgba(129,199,132,.25)',amber:'rgba(255,183,77,.25)'};
  const tx = {blue:'#4fc3f7',green:'#81c784',amber:'#ffb74d'};
  el.style.background  = bg[col]||bg.blue;
  el.style.borderColor = bd[col]||bd.blue;
  el.style.color       = tx[col]||tx.blue;
}

// ═══════════════════════════════════════════════════════════════════════════
// Lane table
// ═══════════════════════════════════════════════════════════════════════════
function fmtBox(b, cls) {
  if (!b) return '<span class="tag tag-miss">—</span>';
  const w = Math.round(b[2]-b[0]), h = Math.round(b[3]-b[1]);
  return `<span class="tag ${cls}" style="font-family:monospace">${w}×${h}</span>`;
}

function updateTable() {
  const tbody = document.getElementById('lane-tbody');
  tbody.innerHTML = lanes[gelIdx].map((l,i) => `
    <tr>
      <td><b>L${i+1}</b></td>
      <td>${fmtBox(l.band,'tag-band')}</td>
      <td>${fmtBox(l.norm,'tag-norm')}</td>
      <td><button class="del-btn" onclick="removeLast(${i})">✕</button></td>
    </tr>`).join('');
}

function removeLast(i) {
  const ls = lanes[gelIdx];
  if (!ls[i]) return;
  if (ls[i].norm) ls[i].norm = null;
  else ls.splice(i, 1);
  updateTable(); redraw();
}

function clearAll() {
  lanes[gelIdx] = [];
  updateTable(); redraw();
  document.getElementById('profile-img').style.display = 'none';
  setStatus('Cleared. Draw band boxes to start.');
}

// ═══════════════════════════════════════════════════════════════════════════
// Gel switch
// ═══════════════════════════════════════════════════════════════════════════
function switchGel(idx) {
  gelIdx = idx;
  document.querySelectorAll('.gel-tab').forEach((t,i) => t.classList.toggle('active', i===idx));
  loadGelImage(idx);
  updateTable();
  setStatus(`Switched to ${GELS[idx].name}. Draw band boxes.`);
}

// ═══════════════════════════════════════════════════════════════════════════
// Profile chart (server)
// ═══════════════════════════════════════════════════════════════════════════
function updateProfile() {
  if (!lanes[gelIdx].length) return;
  fetch('/profile', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({gel_idx: gelIdx, lanes: lanes[gelIdx]})
  }).then(r=>r.json()).then(d=>{
    if (d.img) {
      const el = document.getElementById('profile-img');
      el.src = 'data:image/png;base64,' + d.img;
      el.style.display = 'block';
    }
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// Quantify
// ═══════════════════════════════════════════════════════════════════════════
function quantify() {
  const ls = lanes[gelIdx];
  if (!ls.length) { setStatus('⚠ No boxes drawn yet!', 'amber'); return; }
  setStatus('Computing…');
  fetch('/quantify', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({gel_idx: gelIdx, lanes: ls})
  }).then(r=>r.json()).then(d=>{
    if (d.error) { setStatus('Error: '+d.error, 'amber'); return; }
    resultData[gelIdx] = d;
    setStatus(`✓ ${d.n_lanes} lanes quantified. Go to Results tab →`, 'green');
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// Results page
// ═══════════════════════════════════════════════════════════════════════════
function refreshResults() {
  const d = resultData[gelIdx];
  if (!d) {
    document.getElementById('results-subtitle').textContent =
      'No data yet — go to Picker and click Quantify.';
    return;
  }

  const normMode  = document.getElementById('norm-select').value;
  const showFc    = document.getElementById('show-fc').checked;
  const showLine  = document.getElementById('show-line').checked;
  const ylabel    = document.getElementById('ylabel-input').value;
  const xlabel    = document.getElementById('xlabel-input').value;
  const gelName   = GELS[gelIdx].name;

  document.getElementById('results-subtitle').textContent =
    `${gelName} — ${d.n_lanes} lanes`;

  // If loadctrl not available, force total
  if (normMode === 'loadctrl' && !d.has_lc) {
    document.getElementById('norm-select').value = 'total';
  }

  fetch('/render_results', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      gel_idx: gelIdx, norm_mode: normMode,
      show_fc: showFc, show_line: showLine,
      ylabel: ylabel, xlabel: xlabel
    })
  }).then(r=>r.json()).then(rd=>{
    if (rd.error) { console.error(rd.error); return; }

    // Bar chart
    const barImg = document.getElementById('bar-img');
    barImg.src = 'data:image/png;base64,' + rd.bar;
    barImg.style.display = 'block';
    document.getElementById('bar-placeholder').style.display = 'none';
    document.getElementById('dl-bar').style.display = 'block';

    // Line plot
    const lineImg = document.getElementById('line-img');
    if (showLine && rd.line) {
      lineImg.src = 'data:image/png;base64,' + rd.line;
      lineImg.style.display = 'block';
      document.getElementById('line-placeholder').style.display = 'none';
      document.getElementById('dl-line').style.display = 'block';
    } else {
      lineImg.style.display = 'none';
      document.getElementById('line-placeholder').textContent = 'Line plot disabled';
    }

    // Data table
    if (rd.rows) {
      document.getElementById('data-table-wrap').style.display = 'block';
      const cols = rd.cols;
      document.getElementById('data-thead').innerHTML =
        '<tr>' + cols.map(c=>`<th>${c}</th>`).join('') + '</tr>';
      document.getElementById('data-tbody').innerHTML =
        rd.rows.map(row =>
          '<tr>' + row.map((v,ci) =>
            `<td class="${typeof v==='number'?'num':''}">${v??'—'}</td>`
          ).join('') + '</tr>'
        ).join('');
    }
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// Save helpers
// ═══════════════════════════════════════════════════════════════════════════
function saveCSV() {
  const ls = lanes[gelIdx];
  if (!ls.length) { setStatus('⚠ Quantify first!', 'amber'); return; }
  fetch('/save_csv', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({gel_idx: gelIdx, lanes: ls})
  }).then(r=>r.json()).then(d=>{
    if (d.path) setStatus('✓ Saved: '+d.path, 'green');
    else        setStatus('Error: '+d.error,  'amber');
  });
}

function saveFig() {
  fetch('/save_fig', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({gel_idx: gelIdx, lanes: lanes[gelIdx]})
  }).then(r=>r.json()).then(d=>{
    if (d.path) setStatus('✓ Figure saved: '+d.path, 'green');
    else        setStatus('Error: '+d.error, 'amber');
  });
}

function downloadImg(imgId, filename) {
  const img = document.getElementById(imgId);
  if (!img.src) return;
  const a = document.createElement('a');
  a.href = img.src; a.download = filename; a.click();
}

// ═══════════════════════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════════════════════
loadGelImage(0);
</script>
</body>
</html>
"""

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    gels_json = {
        i: {"name": g["name"], "b64": g["b64"], "w": g["w"], "h": g["h"]}
        for i, g in GELS.items()
    }
    return render_template_string(HTML, gels=gels_json,
                                  gels_json=json.dumps(gels_json))


@app.route("/profile", methods=["POST"])
def profile():
    data  = request.json
    gi    = data["gel_idx"]
    lanes = data["lanes"]
    arr   = GELS[gi]["arr"]

    fig, ax = plt.subplots(figsize=(5, 2.8), dpi=100, facecolor="#0a0a14")
    ax.set_facecolor("#0a0a14")
    ax.tick_params(colors="#8892a4", labelsize=7)
    for sp in ax.spines.values():
        sp.set_color("#2e3245")
    ax.set_xlabel("Pixel row (top→bottom)", color="#8892a4", fontsize=8)
    ax.set_ylabel("BG-subtracted intensity",  color="#8892a4", fontsize=8)

    pal = plt.cm.plasma(np.linspace(0.08, 0.92, max(len(lanes), 1)))
    for i, lane in enumerate(lanes):
        area, clean, *_ = integrate_box(arr, lane["band"])
        ax.plot(np.arange(len(clean)), clean, color=pal[i], lw=1.0, alpha=0.85,
                label=f"L{i+1}")
        if lane.get("norm"):
            nb = lane["norm"]
            ax.axvspan(nb[1], nb[3], alpha=0.1, color="#81c784")

    ax.legend(loc="upper right", fontsize=6, ncol=4, framealpha=0.3,
              labelcolor="#e2e8f0", facecolor="#1a1d27")
    ax.set_title("Lane profiles", color="#e2e8f0", fontsize=9)
    fig.tight_layout()
    return jsonify({"img": fig_to_b64(fig)})


@app.route("/quantify", methods=["POST"])
def quantify():
    try:
        data  = request.json
        gi    = data["gel_idx"]
        lanes = data["lanes"]
        arr   = GELS[gi]["arr"]
        df    = build_df(arr, lanes)
        GELS[gi]["last_df"] = df
        has_lc = bool(df["area_norm_loadctrl"].notna().any())
        return jsonify({
            "n_lanes": len(df),
            "has_lc":  has_lc,
            "rows":    df.values.tolist(),
            "cols":    df.columns.tolist(),
        })
    except Exception as e:
        import traceback
        return jsonify({"error": traceback.format_exc()})


@app.route("/render_results", methods=["POST"])
def render_results():
    try:
        data      = request.json
        gi        = data["gel_idx"]
        norm_mode = data["norm_mode"]      # "total" | "loadctrl"
        show_fc   = data["show_fc"]
        show_line = data["show_line"]
        ylabel    = data.get("ylabel", "Band intensity (normalised)")
        xlabel    = data.get("xlabel", "Lane (time point)")

        df = GELS[gi].get("last_df")
        if df is None:
            return jsonify({"error": "Run quantify first"})

        norm_col = "area_norm_total" if norm_mode == "total" else "area_norm_loadctrl"
        fc_col   = "fc_vs_L1_total"  if norm_mode == "total" else "fc_vs_L1_loadctrl"

        # Fall back gracefully
        if norm_col not in df or df[norm_col].isna().all():
            norm_col = "area_norm_total"
            fc_col   = "fc_vs_L1_total"

        y_vals  = df[norm_col].values.astype(float)
        x_lanes = df["lane"].values
        n       = len(df)

        DARK = "#0a0a14"
        MUTED = "#8892a4"
        TEXT  = "#e2e8f0"
        BORD  = "#2e3245"
        BAR_C = "#4fc3f7"
        FC_C  = "#ffb74d"

        def style_ax(ax, title=""):
            ax.set_facecolor(DARK)
            ax.tick_params(colors=MUTED, labelsize=8)
            for sp in ax.spines.values(): sp.set_color(BORD)
            if title: ax.set_title(title, color=TEXT, fontsize=10, pad=6)
            ax.set_xlabel(xlabel, color=MUTED, fontsize=9)
            ax.set_ylabel(ylabel, color=MUTED, fontsize=9)
            ax.grid(axis="y", color=BORD, lw=0.5, zorder=0)

        # ── Bar chart ─────────────────────────────────────────────────────────
        fig_b, ax_b = plt.subplots(figsize=(7, 4), dpi=130, facecolor=DARK)
        style_ax(ax_b, f"{GELS[gi]['name']} — bar chart")

        x = np.arange(n)
        bars = ax_b.bar(x, y_vals, color=BAR_C, width=0.55,
                        edgecolor="none", alpha=0.88, zorder=3)

        if show_fc and fc_col in df and df[fc_col].notna().any():
            fc_vals = df[fc_col].values.astype(float)
            for xi, (yv, fc) in enumerate(zip(y_vals, fc_vals)):
                if np.isfinite(yv) and np.isfinite(fc):
                    ax_b.text(xi, yv + max(y_vals)*0.02, f"{fc:.2f}×",
                              ha="center", va="bottom", fontsize=8,
                              color=FC_C, fontweight="600")

        ax_b.set_xticks(x)
        ax_b.set_xticklabels([f"L{l}" for l in x_lanes], color=MUTED, fontsize=8)
        ax_b.set_ylim(bottom=0, top=max(y_vals)*1.18)
        fig_b.tight_layout()
        bar_b64 = fig_to_b64(fig_b)

        # ── Line plot ─────────────────────────────────────────────────────────
        line_b64 = None
        if show_line:
            fig_l, ax_l = plt.subplots(figsize=(7, 4), dpi=130, facecolor=DARK)
            style_ax(ax_l, f"{GELS[gi]['name']} — line plot")

            ax_l.plot(x_lanes, y_vals, "o-", color=BAR_C, lw=2.2,
                      markersize=7, markeredgecolor=DARK, markeredgewidth=1,
                      zorder=4)
            ax_l.fill_between(x_lanes, y_vals, alpha=0.10, color=BAR_C)

            if show_fc and fc_col in df and df[fc_col].notna().any():
                fc_vals = df[fc_col].values.astype(float)
                ax2 = ax_l.twinx()
                ax2.plot(x_lanes, fc_vals, "s--", color=FC_C, lw=1.4,
                         markersize=5, alpha=0.8, label="fold-change")
                ax2.set_ylabel("Fold-change vs L1", color=FC_C, fontsize=9)
                ax2.tick_params(colors=FC_C, labelsize=8)
                ax2.spines["right"].set_color(BORD)
                ax2.spines["left"].set_color(BORD)
                ax2.spines["top"].set_color(BORD)
                ax2.spines["bottom"].set_color(BORD)
                ax2.set_facecolor(DARK)
                ax2.axhline(1, color=FC_C, lw=0.8, linestyle=":", alpha=0.5)

            ax_l.set_xticks(x_lanes)
            ax_l.set_xticklabels([f"L{l}" for l in x_lanes], color=MUTED, fontsize=8)
            ax_l.set_ylim(bottom=0)
            fig_l.tight_layout()
            line_b64 = fig_to_b64(fig_l)

        # ── Table data ────────────────────────────────────────────────────────
        show_cols = ["lane", norm_col, fc_col, "area_raw", "lane_total_int"]
        show_cols = [c for c in show_cols if c in df.columns]
        col_labels = {
            "lane":               "Lane",
            "area_raw":           "Raw area",
            "area_norm_total":    "÷ total",
            "area_norm_loadctrl": "÷ norm box",
            "fc_vs_L1_total":     "FC total",
            "fc_vs_L1_loadctrl":  "FC norm",
            "lane_total_int":     "Total int.",
        }
        sub = df[show_cols].copy()
        rows_out = []
        for _, row in sub.iterrows():
            r = []
            for c in show_cols:
                v = row[c]
                if c == "lane":
                    r.append(int(v))
                elif pd.isna(v):
                    r.append(None)
                else:
                    r.append(round(float(v), 5))
            rows_out.append(r)

        return jsonify({
            "bar":  bar_b64,
            "line": line_b64,
            "cols": [col_labels.get(c, c) for c in show_cols],
            "rows": rows_out,
        })

    except Exception as e:
        import traceback
        return jsonify({"error": traceback.format_exc()})


@app.route("/save_csv", methods=["POST"])
def save_csv():
    try:
        data  = request.json
        gi    = data["gel_idx"]
        arr   = GELS[gi]["arr"]
        df    = build_df(arr, data["lanes"])
        out   = Path(GELS[gi]["path"]).with_name(GELS[gi]["name"] + "_quantification.csv")
        df.to_csv(out, index=False)
        print(f"Saved: {out}")
        return jsonify({"path": str(out)})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/save_fig", methods=["POST"])
def save_fig():
    try:
        data  = request.json
        gi    = data["gel_idx"]
        lanes = data["lanes"]
        arr   = GELS[gi]["arr"]
        name  = GELS[gi]["name"]

        fig, axes = plt.subplots(1, 2, figsize=(14, 6), facecolor="#0a0a14")
        for ax in axes:
            ax.set_facecolor("#0a0a14")
            for sp in ax.spines.values(): sp.set_color("#2e3245")
            ax.tick_params(colors="#8892a4", labelsize=7)

        axes[0].imshow(arr, cmap="gray", aspect="auto")
        axes[0].set_title(name, color="#e2e8f0", fontsize=10)
        axes[0].axis("off")
        for i, lane in enumerate(lanes):
            b = lane["band"]
            axes[0].add_patch(mpatches.Rectangle(
                (b[0],b[1]), b[2]-b[0], b[3]-b[1],
                lw=1.8, edgecolor="#4fc3f7", facecolor="#4fc3f7", alpha=0.2))
            axes[0].text(b[0]+2, b[1]+2, f"L{i+1}", color="#4fc3f7",
                         fontsize=8, fontweight="bold")
            if lane.get("norm"):
                n = lane["norm"]
                axes[0].add_patch(mpatches.Rectangle(
                    (n[0],n[1]), n[2]-n[0], n[3]-n[1],
                    lw=1.8, edgecolor="#81c784", facecolor="#81c784", alpha=0.2))

        pal = plt.cm.plasma(np.linspace(0.08, 0.92, max(len(lanes), 1)))
        for i, lane in enumerate(lanes):
            _, clean, *_ = integrate_box(arr, lane["band"])
            axes[1].plot(np.arange(len(clean)), clean, color=pal[i], lw=1.0, label=f"L{i+1}")
        axes[1].set_title("Lane profiles", color="#e2e8f0", fontsize=10)
        axes[1].set_xlabel("Pixel row", color="#8892a4", fontsize=8)
        axes[1].set_ylabel("BG-subtracted intensity", color="#8892a4", fontsize=8)
        axes[1].legend(fontsize=7, framealpha=0.4, labelcolor="#e2e8f0", facecolor="#1a1d27")
        axes[1].grid(color="#2e3245", lw=0.4)

        out = Path(GELS[gi]["path"]).with_name(name + "_annotated.png")
        fig.savefig(out, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)
        return jsonify({"path": str(out)})
    except Exception as e:
        return jsonify({"error": str(e)})


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    paths = sys.argv[1:]
    for p in paths:
        if not Path(p).exists():
            print(f"ERROR: File not found: {p}")
            sys.exit(1)

    print("\n" + "="*55)
    print("  Gel Band Picker v3")
    print("="*55)
    for p in paths:
        print(f"  • {p}")
    print("\n  ➜  Open:  http://localhost:5050")
    print("  ➜  Ctrl+C to quit\n")

    init_gels(paths)
    app.run(host="127.0.0.1", port=5050, debug=False)


if __name__ == "__main__":
    main()
