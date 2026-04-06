#!/usr/bin/env python3
"""
Gel Picker v5
=============
Run:   python gel_picker_v5.py gel1.tif gel2.tif
Open:  http://localhost:5050

Layout (Picker page)
--------------------
┌─────────────────────────────┬──────────────────────────┐
│                             │  Lane profile + area      │
│       Gel image             │  curve (top-right)        │
│                             │                           │
├─────────────────────────────┴──────────────────────────┤
│  Mode buttons  │  Lock size  │  BG slider  │  Actions  │
└────────────────────────────────────────────────────────┘
                  Lane table + session info
New in v5
---------
• Area-under-curve shown as filled region in the profile panel
• Clicking a lane in the profile panel shows its area curve isolated
• Mode + lock controls moved below the gel image (toolbar row)
• Lane profile panel top-right (beside gel)
• Session info bar: date, analyst name, gel name, notes field
• Date/time stamped in CSV and on saved figures
"""

import sys, io, base64, json, warnings, datetime
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
    r = max(4, int(radius))
    d = 2 * r + 1
    s = np.ones(d)
    bg = grey_erosion(profile, structure=s)
    bg = grey_dilation(bg, structure=s)
    return np.minimum(bg, profile)

def check_saturation(arr, box, threshold=0.97):
    H, W = arr.shape
    x0,y0,x1,y1 = int(box[0]),int(box[1]),int(box[2]),int(box[3])
    x0,x1 = sorted([max(0,x0),min(W-1,x1)])
    y0,y1 = sorted([max(0,y0),min(H-1,y1)])
    patch = arr[y0:y1+1, x0:x1+1]
    return float(np.mean(patch >= threshold * 255))

def integrate_box(arr, box, rb_radius=50):
    H, W = arr.shape
    x0,y0,x1,y1 = int(box[0]),int(box[1]),int(box[2]),int(box[3])
    x0,x1 = sorted([max(0,x0),min(W-1,x1)])
    y0,y1 = sorted([max(0,y0),min(H-1,y1)])
    inv      = (255 - arr) / 255.0
    strip    = inv[y0:y1+1, x0:x1+1]
    prof_raw = strip.mean(axis=1)
    bg       = rolling_ball(prof_raw, radius=max(4, min(rb_radius, len(prof_raw)//2)))
    clean    = np.maximum(prof_raw - bg, 0)
    area     = float(trapezoid(clean))
    edge_n   = max(2, len(prof_raw)//6)
    bg_noise = np.std(np.concatenate([prof_raw[:edge_n], prof_raw[-edge_n:]]))
    snr      = float(clean.max() / bg_noise) if bg_noise > 1e-9 else 0.0
    return area, clean, prof_raw, bg, x0, y0, x1, y1, snr

def lane_total(arr, x0, x1, rb_radius=50):
    H, W = arr.shape
    x0,x1 = max(0,int(x0)), min(W-1,int(x1))
    inv  = (255 - arr) / 255.0
    prof = inv[:, x0:x1+1].mean(axis=1)
    bg   = rolling_ball(prof, radius=rb_radius)
    return float(trapezoid(np.maximum(prof - bg, 0)))

def arr_to_b64(arr):
    fig, ax = plt.subplots(figsize=(arr.shape[1]/100, arr.shape[0]/100), dpi=100)
    ax.imshow(arr, cmap="gray", aspect="auto", vmin=arr.min(), vmax=arr.max())
    ax.axis("off")
    fig.subplots_adjust(0,0,1,1)
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

def build_df(arr, lanes, rb_radius=50, analyst="", notes=""):
    rows = []
    ts   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for i, lane in enumerate(lanes):
        area_band, clean, prof_raw, bg, x0,y0,x1,y1, snr = \
            integrate_box(arr, lane["band"], rb_radius)
        total    = lane_total(arr, x0, x1, rb_radius)
        sat_frac = check_saturation(arr, lane["band"])
        norm_area = None
        if lane.get("norm"):
            norm_area, *_ = integrate_box(arr, lane["norm"], rb_radius)
        rows.append({
            "lane":               i + 1,
            "timestamp":          ts,
            "analyst":            analyst,
            "notes":              notes,
            "band_x0": x0, "band_y0": y0, "band_x1": x1, "band_y1": y1,
            "rb_radius":          rb_radius,
            "area_raw":           round(area_band, 5),
            "snr":                round(snr, 2),
            "saturation_frac":    round(sat_frac, 4),
            "saturation_warn":    sat_frac > 0.02,
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

# ── Flask ─────────────────────────────────────────────────────────────────────
app  = Flask(__name__)
GELS = {}

def init_gels(paths):
    for i, p in enumerate(paths):
        arr = load_gel(p)
        GELS[i] = {
            "name": Path(p).stem, "path": str(p),
            "arr": arr, "b64": arr_to_b64(arr),
            "h": int(arr.shape[0]), "w": int(arr.shape[1]),
            "last_df": None,
        }

# ─────────────────────────────────────────────────────────────────────────────
HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Gel Picker v5</title>
<style>
:root{
  --bg:#0f1117; --panel:#1a1d27; --panel2:#141620;
  --border:#2e3245; --text:#e2e8f0; --muted:#8892a4;
  --blue:#4fc3f7; --green:#81c784; --amber:#ffb74d;
  --purple:#ce93d8; --danger:#ef5350; --teal:#4db6ac;
  --pink:#f48fb1;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);
     font-family:'Segoe UI',system-ui,sans-serif;
     font-size:13px;height:100vh;display:flex;flex-direction:column;overflow:hidden}

/* ── Nav ─────────────────────────────────────────────────────────────────── */
nav{background:var(--panel);border-bottom:1px solid var(--border);
    padding:0 14px;display:flex;align-items:stretch;gap:0;flex-shrink:0;height:44px}
nav h1{font-size:13.5px;font-weight:700;color:var(--blue);
       display:flex;align-items:center;padding-right:16px;
       border-right:1px solid var(--border);margin-right:4px;white-space:nowrap;gap:6px}
.nav-tab{padding:0 14px;border:none;background:transparent;color:var(--muted);
         font-size:12px;font-weight:500;cursor:pointer;
         border-bottom:2px solid transparent;transition:all .15s;
         display:flex;align-items:center;gap:5px}
.nav-tab.active{color:var(--blue);border-bottom-color:var(--blue)}
.nav-tab:hover:not(.active){color:var(--text)}
#gel-tabs{display:flex;gap:5px;margin-left:auto;align-items:center}
.gel-tab{padding:3px 9px;border-radius:5px;border:1px solid var(--border);
         cursor:pointer;background:transparent;color:var(--muted);font-size:11px;transition:all .15s}
.gel-tab.active{background:var(--blue);color:#000;border-color:var(--blue);font-weight:600}

/* ── Session info bar ─────────────────────────────────────────────────────── */
#session-bar{background:var(--panel2);border-bottom:1px solid var(--border);
             padding:5px 14px;display:flex;align-items:center;gap:14px;flex-shrink:0;flex-wrap:wrap}
#session-bar .field{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--muted)}
#session-bar input,#session-bar textarea{
  background:var(--bg);color:var(--text);border:1px solid var(--border);
  border-radius:5px;padding:2px 7px;font-size:11px;font-family:inherit}
#session-bar input:focus,#session-bar textarea:focus{outline:none;border-color:var(--blue)}
#session-clock{font-size:11px;color:var(--teal);font-family:monospace;
               margin-left:auto;white-space:nowrap}

/* ── Pages ───────────────────────────────────────────────────────────────── */
.page{display:none;flex:1;flex-direction:column;overflow:hidden;min-height:0}
.page.active{display:flex}

/* ══ PICKER PAGE ══════════════════════════════════════════════════════════ */

/* Top area: gel left, panels right */
#picker-top{display:flex;flex:1;min-height:0;overflow:hidden}

/* Gel canvas */
#canvas-wrap{flex:1 1 58%;position:relative;overflow:hidden;
             background:#050507;display:flex;align-items:center;justify-content:center;min-height:0}
#gel-canvas{display:block;max-width:100%;max-height:100%;cursor:crosshair}

/* Right panels stack */
#right-panels{width:380px;flex-shrink:0;display:flex;flex-direction:column;
              border-left:1px solid var(--border);background:var(--panel2);min-height:0}

/* Profile panel */
#profile-panel{flex:1;min-height:0;position:relative;border-bottom:1px solid var(--border)}
#profile-canvas{width:100%;height:100%;display:block}
.panel-header{position:absolute;top:0;left:0;right:0;height:26px;
              background:rgba(26,29,39,0.92);display:flex;align-items:center;
              padding:0 10px;gap:8px;z-index:2;border-bottom:1px solid var(--border)}
.panel-header .plabel{font-size:10px;font-weight:700;letter-spacing:.07em;
                       text-transform:uppercase;color:var(--muted)}
.panel-header .plegend{display:flex;gap:10px;margin-left:auto}
.pleg{display:flex;align-items:center;gap:3px;font-size:9.5px;color:var(--muted)}
.pleg-sw{width:18px;height:2px;border-radius:1px}

/* Lane selector pills in profile panel */
#lane-pills{position:absolute;bottom:5px;left:8px;right:8px;
            display:flex;gap:4px;flex-wrap:wrap;z-index:2}
.lane-pill{padding:1px 7px;border-radius:10px;border:1px solid var(--border);
           background:var(--panel2);font-size:9.5px;cursor:pointer;
           color:var(--muted);transition:all .12s}
.lane-pill.active{color:#000;font-weight:600}
.lane-pill.all-pill{border-color:var(--muted)}
.lane-pill.all-pill.active{background:var(--muted);border-color:var(--muted)}

/* Lane table panel */
#table-panel{flex:0 0 auto;max-height:220px;overflow-y:auto;padding:8px 10px}
.section-label{font-size:9.5px;font-weight:700;letter-spacing:.08em;
               color:var(--muted);text-transform:uppercase;margin-bottom:4px}
table{width:100%;border-collapse:collapse;font-size:10.5px}
th{color:var(--muted);font-weight:600;font-size:9px;letter-spacing:.05em;
   text-transform:uppercase;padding:3px 4px;border-bottom:1px solid var(--border);text-align:left}
td{padding:3px 4px;border-bottom:1px solid rgba(46,50,69,.4);vertical-align:middle}
tr:hover td{background:rgba(255,255,255,.02)}
.tag{display:inline-block;padding:0px 4px;border-radius:3px;font-size:9px;font-weight:600}
.tag-band{background:rgba(79,195,247,.2); color:var(--blue)}
.tag-norm{background:rgba(129,199,132,.2);color:var(--green)}
.tag-miss{background:rgba(239,83,80,.12); color:var(--danger)}
.del-btn{background:none;border:none;cursor:pointer;color:var(--danger);font-size:12px;padding:0 2px;opacity:.5}
.del-btn:hover{opacity:1}
.snr-good{color:var(--green)} .snr-ok{color:var(--amber)} .snr-bad{color:var(--danger)}

/* ── Toolbar (below gel+panels) ───────────────────────────────────────────── */
#toolbar{background:var(--panel);border-top:1px solid var(--border);
         padding:7px 14px;display:flex;align-items:center;gap:10px;
         flex-shrink:0;flex-wrap:wrap}

/* Mode pills */
.mode-group{display:flex;gap:4px;align-items:center}
.mode-btn{padding:4px 10px;border-radius:20px;border:1px solid var(--border);
          cursor:pointer;background:transparent;color:var(--muted);
          font-size:11px;white-space:nowrap;transition:all .15s}
.mode-btn.active-band{background:rgba(79,195,247,.15);border-color:var(--blue);color:var(--blue);font-weight:600}
.mode-btn.active-norm{background:rgba(129,199,132,.15);border-color:var(--green);color:var(--green);font-weight:600}
.mode-btn.active-move{background:rgba(255,183,77,.15);border-color:var(--amber);color:var(--amber);font-weight:600}

/* Toolbar divider */
.tb-div{width:1px;height:22px;background:var(--border);flex-shrink:0}

/* Lock controls */
.lock-group{display:flex;gap:6px;align-items:center}
.lock-chip{display:flex;align-items:center;gap:4px;padding:3px 8px;
           border-radius:6px;border:1px solid var(--border);background:var(--panel2);
           font-size:11px;color:var(--muted)}
.lock-chip input[type=checkbox]{accent-color:var(--amber);width:11px;height:11px}
.lock-chip .dims{font-size:9.5px;color:var(--amber);font-family:monospace;margin-left:3px}
.lock-chip button{background:none;border:none;cursor:pointer;color:var(--muted);
                   font-size:9px;padding:0 2px;opacity:.7}
.lock-chip button:hover{opacity:1;color:var(--danger)}

/* BG slider */
.slider-chip{display:flex;align-items:center;gap:6px;padding:3px 8px;
             border-radius:6px;border:1px solid var(--border);background:var(--panel2)}
.slider-chip label{font-size:10.5px;color:var(--muted);white-space:nowrap}
.slider-chip input[type=range]{width:80px;accent-color:var(--teal)}
.slider-chip .val{font-size:10px;color:var(--teal);font-family:monospace;min-width:22px}

/* Action buttons */
.btn{padding:5px 11px;border-radius:7px;border:1px solid var(--border);
     cursor:pointer;font-size:11.5px;font-weight:500;transition:all .15s}
.btn-primary{background:var(--blue);  color:#000;border-color:var(--blue)}
.btn-success{background:var(--green); color:#000;border-color:var(--green)}
.btn-ghost  {background:transparent;  color:var(--muted)}
.btn-ghost:hover{background:var(--border);color:var(--text)}
.btn-danger {background:transparent;  color:var(--danger);border-color:var(--danger)}

/* Status */
#status{padding:0 10px;font-size:11px;color:var(--blue);white-space:nowrap;
        overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}

/* ══ RESULTS PAGE ══════════════════════════════════════════════════════════ */
#page-results{overflow-y:auto}
#results-inner{padding:18px;display:flex;flex-direction:column;gap:18px;
               max-width:1200px;margin:0 auto;width:100%}
.results-header{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.results-header h2{font-size:16px;font-weight:600}
.results-header p{color:var(--muted);font-size:11.5px}
.chart-opts{display:flex;gap:9px;align-items:center;flex-wrap:wrap;
            padding:10px 13px;background:var(--panel);border-radius:9px;border:1px solid var(--border)}
.chart-opts label{font-size:11.5px;color:var(--muted);display:flex;align-items:center;gap:4px;cursor:pointer}
.chart-opts select,.chart-opts input[type=text]{
  background:var(--panel2);color:var(--text);border:1px solid var(--border);
  border-radius:5px;padding:3px 7px;font-size:11.5px}
.chart-opts input[type=checkbox]{accent-color:var(--blue)}
.charts-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.chart-card{background:var(--panel);border:1px solid var(--border);
            border-radius:10px;padding:13px;display:flex;flex-direction:column;gap:8px}
.chart-card h3{font-size:12px;font-weight:600;color:var(--text)}
.chart-card img{width:100%;border-radius:5px;display:block}
.data-table-wrap{background:var(--panel);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.data-table-wrap h3{padding:11px 13px;font-size:12px;font-weight:600;border-bottom:1px solid var(--border)}
.data-table-wrap table{font-size:10.5px}
.data-table-wrap th{background:var(--panel2);padding:6px 10px}
.data-table-wrap td{padding:5px 10px}
.num{font-family:monospace;text-align:right}
.warn-cell{color:var(--amber)}
</style>
</head>
<body>

<!-- ── Nav ────────────────────────────────────────────────────────────────── -->
<nav>
  <h1>🔬 Gel Picker <span style="color:var(--muted);font-weight:400;font-size:11px">v5</span></h1>
  <button class="nav-tab active" id="tab-picker"  onclick="showPage('picker')">📐 Picker</button>
  <button class="nav-tab"        id="tab-results" onclick="showPage('results')">📊 Results</button>
  <div id="gel-tabs">
    {% for i, g in gels.items() %}
    <button class="gel-tab {% if loop.first %}active{% endif %}"
            onclick="switchGel({{i}})">{{ g.name }}</button>
    {% endfor %}
  </div>
</nav>

<!-- ── Session bar ────────────────────────────────────────────────────────── -->
<div id="session-bar">
  <div class="field">👤 <input id="inp-analyst" type="text" placeholder="Analyst name" style="width:130px"></div>
  <div class="field">🧪 <input id="inp-expt"   type="text" placeholder="Experiment / sample" style="width:160px"></div>
  <div class="field">📝 <input id="inp-notes"  type="text" placeholder="Notes (e.g. time points, conditions)" style="width:220px"></div>
  <div id="session-clock">—</div>
</div>

<!-- ══ PICKER ══════════════════════════════════════════════════════════════ -->
<div class="page active" id="page-picker">

  <!-- Top: gel + right panels -->
  <div id="picker-top">

    <!-- Gel canvas -->
    <div id="canvas-wrap">
      <canvas id="gel-canvas"></canvas>
    </div>

    <!-- Right: profile + table -->
    <div id="right-panels">

      <!-- Profile / area curve panel -->
      <div id="profile-panel">
        <div class="panel-header">
          <span class="plabel">Profiles</span>
          <!-- View toggle -->
          <div style="display:flex;gap:4px;margin-left:8px">
            <button class="mode-btn active-band" id="view-profile"
                    onclick="setView('profile')" style="padding:2px 8px;font-size:10px;border-radius:10px">Signal</button>
            <button class="mode-btn" id="view-area"
                    onclick="setView('area')" style="padding:2px 8px;font-size:10px;border-radius:10px">Area curve</button>
            <button class="mode-btn" id="view-both"
                    onclick="setView('both')" style="padding:2px 8px;font-size:10px;border-radius:10px">Both</button>
          </div>
          <div class="plegend">
            <div class="pleg"><div class="pleg-sw" style="background:#4fc3f7"></div>signal</div>
            <div class="pleg"><div class="pleg-sw" style="background:#ef5350;border-top:1px dashed #ef5350"></div>bg</div>
            <div class="pleg"><div class="pleg-sw" style="background:rgba(79,195,247,.35);height:8px;border-radius:2px"></div>area</div>
            <div class="pleg"><div class="pleg-sw" style="background:#81c784"></div>norm</div>
          </div>
        </div>
        <canvas id="profile-canvas"></canvas>
        <div id="lane-pills"></div>
      </div>

      <!-- Lane table -->
      <div id="table-panel">
        <div class="section-label">Lanes</div>
        <table>
          <thead><tr><th>#</th><th>Band</th><th>Norm</th><th>SNR</th><th>⚠</th><th></th></tr></thead>
          <tbody id="lane-tbody"></tbody>
        </table>
      </div>

    </div><!-- /right-panels -->
  </div><!-- /picker-top -->

  <!-- ── Toolbar ──────────────────────────────────────────────────────────── -->
  <div id="toolbar">

    <!-- Mode -->
    <div class="mode-group">
      <button class="mode-btn active-band" id="btn-band"     onclick="setMode('band')">🔵 Band box</button>
      <button class="mode-btn"             id="btn-norm-lane" onclick="setMode('norm_lane')">🟢 Norm (lane)</button>
      <button class="mode-btn"             id="btn-norm-all"  onclick="setMode('norm_all')">🟢 Norm (all)</button>
      <button class="mode-btn"             id="btn-move"      onclick="setMode('move')">🟡 Move / resize</button>
    </div>

    <div class="tb-div"></div>

    <!-- Lock -->
    <div class="lock-group">
      <span style="font-size:10px;color:var(--muted)">Lock:</span>
      <div class="lock-chip">
        <label><input type="checkbox" id="lock-band" onchange="toggleLock('band')">Band</label>
        <span class="dims" id="lock-band-dims">—</span>
        <button onclick="resetLock('band')" title="Reset">✕</button>
      </div>
      <div class="lock-chip">
        <label><input type="checkbox" id="lock-norm" onchange="toggleLock('norm')">Norm</label>
        <span class="dims" id="lock-norm-dims">—</span>
        <button onclick="resetLock('norm')" title="Reset">✕</button>
      </div>
    </div>

    <div class="tb-div"></div>

    <!-- BG slider -->
    <div class="slider-chip">
      <label>BG radius</label>
      <input type="range" id="rb-slider" min="5" max="150" value="50"
             oninput="onRbChange(this.value)">
      <span class="val" id="rb-val">50</span>
    </div>

    <div class="tb-div"></div>

    <!-- Actions -->
    <button class="btn btn-primary" onclick="quantify()">Quantify ▶</button>
    <button class="btn btn-success" onclick="saveCSV()">Save CSV</button>
    <button class="btn btn-ghost"   onclick="saveFig()">Save PNG</button>
    <button class="btn btn-danger"  onclick="clearAll()">Clear all</button>

    <div class="tb-div"></div>
    <div id="status">Draw a band box to begin.</div>

  </div><!-- /toolbar -->

</div><!-- /page-picker -->


<!-- ══ RESULTS ══════════════════════════════════════════════════════════════ -->
<div class="page" id="page-results">
  <div id="results-inner">
    <div class="results-header">
      <h2>Results</h2>
      <p id="results-subtitle">Quantify on the Picker page first.</p>
    </div>
    <div class="chart-opts">
      <label>Normalisation:
        <select id="norm-select" onchange="refreshResults()">
          <option value="total">÷ total lane intensity</option>
          <option value="loadctrl">÷ norm box (loading control)</option>
        </select>
      </label>
      <label><input type="checkbox" id="show-fc"   checked onchange="refreshResults()">Fold-change</label>
      <label><input type="checkbox" id="show-line" checked onchange="refreshResults()">Line plot</label>
      <label>Y: <input id="ylabel-input" type="text" value="Band intensity (normalised)" style="width:185px" onchange="refreshResults()"></label>
      <label>X: <input id="xlabel-input" type="text" value="Lane (time point)"           style="width:130px" onchange="refreshResults()"></label>
    </div>
    <div class="charts-grid">
      <div class="chart-card">
        <h3>Bar chart</h3>
        <img id="bar-img" src="" style="display:none">
        <span id="bar-ph" style="color:var(--muted);font-size:11px">Quantify first →</span>
        <button class="btn btn-ghost" id="dl-bar" style="display:none;align-self:flex-end;font-size:11px"
                onclick="dlImg('bar-img','bar_chart.png')">⬇ Download</button>
      </div>
      <div class="chart-card">
        <h3>Line plot</h3>
        <img id="line-img" src="" style="display:none">
        <span id="line-ph" style="color:var(--muted);font-size:11px">Quantify first →</span>
        <button class="btn btn-ghost" id="dl-line" style="display:none;align-self:flex-end;font-size:11px"
                onclick="dlImg('line-img','line_plot.png')">⬇ Download</button>
      </div>
    </div>
    <div class="data-table-wrap" id="data-table-wrap" style="display:none">
      <h3>Data table</h3>
      <table><thead id="data-thead"></thead><tbody id="data-tbody"></tbody></table>
    </div>
  </div>
</div>

<!-- ══════════════════════════════════════════════════════════════════════════
     JAVASCRIPT
══════════════════════════════════════════════════════════════════════════ -->
<script>
// ── State ─────────────────────────────────────────────────────────────────────
const GELS = {{ gels_json | safe }};
let gelIdx = 0, mode = 'band';
let lanes = {}; for (const i in GELS) lanes[i] = [];
let resultData = {};
let profileView = 'profile';  // 'profile' | 'area' | 'both'
let activeLane  = -1;         // -1 = all lanes shown

const lockState = {
  band: {locked:false, w:null, h:null},
  norm: {locked:false, w:null, h:null},
};
const rbRadius = {}; for (const i in GELS) rbRadius[i] = 50;

// ── Clock ─────────────────────────────────────────────────────────────────────
function updateClock() {
  const now = new Date();
  document.getElementById('session-clock').textContent =
    now.toISOString().replace('T',' ').slice(0,19) + ' UTC';
}
updateClock(); setInterval(updateClock, 1000);

// ── Page switch ───────────────────────────────────────────────────────────────
function showPage(p) {
  document.querySelectorAll('.page').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(el => el.classList.remove('active'));
  document.getElementById('page-'+p).classList.add('active');
  document.getElementById('tab-'+p).classList.add('active');
  if (p==='results') refreshResults();
  if (p==='picker')  { fitCanvas(); redraw(); }
}

// ── Canvas ────────────────────────────────────────────────────────────────────
const gelCanvas = document.getElementById('gel-canvas');
const gelCtx    = gelCanvas.getContext('2d');
let imgEl = new Image(), imgW=1, imgH=1, scale=1;

function loadGelImage(idx) {
  imgEl = new Image();
  imgEl.onload = () => { imgW=imgEl.naturalWidth; imgH=imgEl.naturalHeight; fitCanvas(); redraw(); drawProfiles(); };
  imgEl.src = 'data:image/png;base64,'+GELS[idx].b64;
}
function fitCanvas() {
  const wrap = document.getElementById('canvas-wrap');
  if (!wrap.clientWidth) return;
  scale = Math.min(wrap.clientWidth/imgW, wrap.clientHeight/imgH, 1);
  gelCanvas.width  = Math.round(imgW*scale);
  gelCanvas.height = Math.round(imgH*scale);
}
window.addEventListener('resize', ()=>{ fitCanvas(); redraw(); resizeProfileCanvas(); drawProfiles(); });

// ── Draw gel + boxes ──────────────────────────────────────────────────────────
const HANDLE = 8;
let selectedBox = null, drag = null;

function redraw() {
  gelCtx.clearRect(0,0,gelCanvas.width,gelCanvas.height);
  gelCtx.drawImage(imgEl,0,0,gelCanvas.width,gelCanvas.height);
  lanes[gelIdx].forEach((lane,i)=>{
    const isSel = selectedBox && selectedBox.laneIdx===i;
    drawBox(lane.band,'#4fc3f7',`L${i+1}`, isSel && selectedBox.type==='band');
    if (lane.norm) drawBox(lane.norm,'#81c784','norm', isSel && selectedBox.type==='norm');
  });
  if (drag && drag.type==='draw') {
    const col = (mode==='band')?'#4fc3f7':'#81c784';
    const [x0,y0,x1,y1] = snapToLock(drag.sx,drag.sy,drag.ex,drag.ey).map(v=>v*scale);
    drawRectRaw(x0,y0,x1,y1,col,0.25);
  }
}
function drawBox(b,col,label,selected) {
  if (!b) return;
  const [x0,y0,x1,y1]=b.map(v=>v*scale);
  gelCtx.strokeStyle=selected?'#ffb74d':col; gelCtx.lineWidth=selected?2.5:1.8;
  gelCtx.strokeRect(x0,y0,x1-x0,y1-y0);
  gelCtx.fillStyle=col; gelCtx.globalAlpha=0.18; gelCtx.fillRect(x0,y0,x1-x0,y1-y0);
  gelCtx.globalAlpha=1;
  gelCtx.fillStyle=selected?'#ffb74d':col; gelCtx.font='bold 11px monospace';
  gelCtx.fillText(label,x0+3,y0+13);
  if (selected) { gelCtx.fillStyle='#ffb74d'; gelCtx.fillRect(x1-HANDLE,y1-HANDLE,HANDLE,HANDLE); }
}
function drawRectRaw(x0,y0,x1,y1,col,alpha){
  gelCtx.strokeStyle=col; gelCtx.lineWidth=2;
  gelCtx.strokeRect(x0,y0,x1-x0,y1-y0);
  gelCtx.fillStyle=col; gelCtx.globalAlpha=alpha;
  gelCtx.fillRect(x0,y0,x1-x0,y1-y0); gelCtx.globalAlpha=1;
}

// ── Lock ──────────────────────────────────────────────────────────────────────
function lockType(){ return (mode==='band')?'band':'norm'; }
function snapToLock(sx,sy,ex,ey){
  const ls=lockState[lockType()];
  if (!ls.locked||ls.w===null) return [Math.min(sx,ex),Math.min(sy,ey),Math.max(sx,ex),Math.max(sy,ey)];
  return [sx,sy,sx+ls.w,sy+ls.h];
}
function toggleLock(type){ lockState[type].locked=document.getElementById('lock-'+type).checked; updateLockDims(type); }
function resetLock(type){ lockState[type].w=null; lockState[type].h=null; lockState[type].locked=false;
  document.getElementById('lock-'+type).checked=false; document.getElementById('lock-'+type+'-dims').textContent='—'; }
function updateLockDims(type){
  const ls=lockState[type];
  document.getElementById('lock-'+type+'-dims').textContent=ls.w!==null?`${Math.round(ls.w)}×${Math.round(ls.h)}px`:'—';
}

// ── Hit test ──────────────────────────────────────────────────────────────────
function hitTest(px,py){
  const ls=lanes[gelIdx];
  for (let i=ls.length-1;i>=0;i--){
    for (const type of ['norm','band']){
      const b=ls[i][type]; if (!b) continue;
      const [x0,y0,x1,y1]=b.map(v=>v*scale);
      if (px>=x1-HANDLE&&px<=x1&&py>=y1-HANDLE&&py<=y1) return {laneIdx:i,type,part:'handle'};
      if (px>=x0&&px<=x1&&py>=y0&&py<=y1) return {laneIdx:i,type,part:'body'};
    }
  }
  return null;
}

// ── Mouse ─────────────────────────────────────────────────────────────────────
function cXY(e){ const r=gelCanvas.getBoundingClientRect(); return {x:(e.clientX-r.left)/scale,y:(e.clientY-r.top)/scale}; }

gelCanvas.addEventListener('mousedown', e=>{
  const {x,y}=cXY(e); const px=x*scale,py=y*scale;
  if (mode==='move'){
    const hit=hitTest(px,py);
    if (hit){ selectedBox=hit; drag={type:'move',part:hit.part,ox:x,oy:y,origBox:[...lanes[gelIdx][hit.laneIdx][hit.type]]};
              gelCanvas.style.cursor=hit.part==='handle'?'se-resize':'grabbing'; }
    else { selectedBox=null; }
    redraw(); return;
  }
  drag={type:'draw',sx:x,sy:y,ex:x,ey:y};
});

gelCanvas.addEventListener('mousemove', e=>{
  const {x,y}=cXY(e);
  if (mode==='move'){
    if (!drag){ const hit=hitTest(x*scale,y*scale); gelCanvas.style.cursor=hit?(hit.part==='handle'?'se-resize':'grab'):'default'; return; }
    const dx=x-drag.ox, dy=y-drag.oy;
    const [ox0,oy0,ox1,oy1]=drag.origBox; const bw=ox1-ox0,bh=oy1-oy0;
    let nb = drag.part==='body' ? [ox0+dx,oy0+dy,ox1+dx,oy1+dy] : [ox0,oy0,Math.max(ox0+10,ox0+bw+dx),Math.max(oy0+6,oy0+bh+dy)];
    nb=nb.map((v,i)=>i%2===0?Math.max(0,Math.min(imgW-1,v)):Math.max(0,Math.min(imgH-1,v)));
    lanes[gelIdx][selectedBox.laneIdx][selectedBox.type]=nb;
    redraw(); return;
  }
  if (drag&&drag.type==='draw'){ drag.ex=x; drag.ey=y; redraw(); }
});

gelCanvas.addEventListener('mouseup', e=>{
  const {x,y}=cXY(e);
  if (mode==='move'){ if(drag){ gelCanvas.style.cursor='grab'; drag=null; updateTable(); drawProfiles(); } return; }
  if (!drag||drag.type!=='draw'){ drag=null; return; }
  drag.ex=x; drag.ey=y;
  let [x0,y0,x1,y1]=snapToLock(drag.sx,drag.sy,drag.ex,drag.ey); drag=null;
  if (Math.abs(x1-x0)<4||Math.abs(y1-y0)<4){ redraw(); return; }
  x0=Math.max(0,Math.min(imgW-1,x0)); x1=Math.max(0,Math.min(imgW-1,x1));
  y0=Math.max(0,Math.min(imgH-1,y0)); y1=Math.max(0,Math.min(imgH-1,y1));
  const box=[x0,y0,x1,y1], w=Math.abs(x1-x0), h=Math.abs(y1-y0);
  const lt=lockType(),ls=lockState[lt];
  if (!ls.locked||ls.w===null){ ls.w=w; ls.h=h; updateLockDims(lt); }
  const la=lanes[gelIdx];
  if (mode==='band'){ la.push({band:box,norm:null}); setStatus(`Lane ${la.length} band box drawn.`); }
  else if (mode==='norm_lane'){
    let t=null; for(let i=la.length-1;i>=0;i--){if(!la[i].norm){t=i;break;}}
    if(t===null){setStatus('⚠ Draw a band box first.','amber');redraw();return;}
    la[t].norm=box; setStatus(`Norm box → lane ${t+1}.`,'green');
  } else if (mode==='norm_all'){ la.forEach(l=>l.norm=box); setStatus('Norm box → all lanes.','green'); }
  updateTable(); drawProfiles(); redraw();
});

// ── Mode ──────────────────────────────────────────────────────────────────────
function setMode(m){
  mode=m;
  ['btn-band','btn-norm-lane','btn-norm-all','btn-move'].forEach(id=>document.getElementById(id).className='mode-btn');
  const map={band:'btn-band',norm_lane:'btn-norm-lane',norm_all:'btn-norm-all',move:'btn-move'};
  const cls={band:'active-band',norm_lane:'active-norm',norm_all:'active-norm',move:'active-move'};
  document.getElementById(map[m]).className='mode-btn '+cls[m];
  gelCanvas.style.cursor=m==='move'?'grab':'crosshair';
  if(m!=='move') selectedBox=null;
  redraw();
}

// ── Profile view toggle ────────────────────────────────────────────────────────
function setView(v){
  profileView=v;
  ['view-profile','view-area','view-both'].forEach(id=>document.getElementById(id).className='mode-btn');
  const cls={profile:'active-band',area:'active-norm',both:'active-move'};
  document.getElementById('view-'+v).className='mode-btn '+cls[v];
  drawProfiles();
}

// ── Lane pills ────────────────────────────────────────────────────────────────
function updatePills(){
  const ls=lanes[gelIdx];
  const wrap=document.getElementById('lane-pills');
  wrap.innerHTML='<span class="lane-pill all-pill'+(activeLane===-1?' active':'')+'" onclick="selectLane(-1)">All</span>'
    +ls.map((_,i)=>`<span class="lane-pill" id="pill-${i}" onclick="selectLane(${i})"
      style="border-color:${palette(i,ls.length)};${activeLane===i?'background:'+palette(i,ls.length)+';color:#000':''}">L${i+1}</span>`).join('');
}
function selectLane(i){ activeLane=i; updatePills(); drawProfiles(); }
function palette(i,n){ const cols=['#4fc3f7','#ce93d8','#ffb74d','#4db6ac','#f48fb1','#80cbc4','#ff8a65','#a5d6a7'];
  return cols[i%cols.length]; }

// ── BG slider ──────────────────────────────────────────────────────────────────
function onRbChange(v){ document.getElementById('rb-val').textContent=v; rbRadius[gelIdx]=parseInt(v); drawProfiles(); }

// ── Status ────────────────────────────────────────────────────────────────────
function setStatus(msg,col='blue'){
  const el=document.getElementById('status'); el.textContent=msg;
  const tx={blue:'#4fc3f7',green:'#81c784',amber:'#ffb74d'};
  el.style.color=tx[col]||tx.blue;
}

// ── Lane table ─────────────────────────────────────────────────────────────────
function fmtBox(b,cls){
  if(!b) return '<span class="tag tag-miss">—</span>';
  return `<span class="tag ${cls}">${Math.round(b[2]-b[0])}×${Math.round(b[3]-b[1])}</span>`;
}
function updateTable(){
  document.getElementById('lane-tbody').innerHTML=
    lanes[gelIdx].map((l,i)=>`<tr>
      <td><b>L${i+1}</b></td>
      <td>${fmtBox(l.band,'tag-band')}</td>
      <td>${fmtBox(l.norm,'tag-norm')}</td>
      <td><span id="snr-${i}">—</span></td>
      <td><span id="warn-${i}"></span></td>
      <td><button class="del-btn" onclick="removeLane(${i})">✕</button></td>
    </tr>`).join('');
  updatePills();
}
function removeLane(i){
  const ls=lanes[gelIdx]; if(!ls[i]) return;
  if(ls[i].norm) ls[i].norm=null; else ls.splice(i,1);
  if(selectedBox&&selectedBox.laneIdx===i) selectedBox=null;
  if(activeLane>=lanes[gelIdx].length) activeLane=-1;
  updateTable(); redraw(); drawProfiles();
}
function clearAll(){
  lanes[gelIdx]=[]; selectedBox=null; activeLane=-1;
  updateTable(); redraw(); drawProfiles(); setStatus('Cleared.');
}

// ── Profile canvas (inline, drawn in JS) ──────────────────────────────────────
const profCanvas=document.getElementById('profile-canvas');
const profCtx   =profCanvas.getContext('2d');

function resizeProfileCanvas(){
  const panel=document.getElementById('profile-panel');
  profCanvas.width =panel.clientWidth;
  profCanvas.height=panel.clientHeight;
}

function drawProfiles(){
  resizeProfileCanvas();
  const W=profCanvas.width, H=profCanvas.clientHeight;
  profCtx.clearRect(0,0,W,H);
  profCtx.fillStyle='#050507'; profCtx.fillRect(0,0,W,H);
  const ls=lanes[gelIdx]; if(!ls.length) return;

  fetch('/profile_data',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({gel_idx:gelIdx,lanes:ls,rb_radius:rbRadius[gelIdx]})
  }).then(r=>r.json()).then(d=>{
    if(!d.profiles) return;
    resizeProfileCanvas();
    const W=profCanvas.width, H=profCanvas.height;
    profCtx.clearRect(0,0,W,H);
    profCtx.fillStyle='#050507'; profCtx.fillRect(0,0,W,H);

    const pad={l:42,r:10,t:30,b:22};
    const plotW=W-pad.l-pad.r, plotH=H-pad.t-pad.b;

    // Filter to active lane if set
    const toShow = activeLane===-1 ? d.profiles : [d.profiles[activeLane]];
    const idxOffset = activeLane===-1 ? 0 : activeLane;

    // Global y range
    let yMax=0;
    toShow.forEach(p=>{ const mx=Math.max(...p.clean,...p.raw); if(mx>yMax) yMax=mx; });
    if(yMax<=0) yMax=1;

    const nPx=d.profiles[0].clean.length;
    const xS=plotW/nPx, yS=plotH/yMax;

    // Grid
    profCtx.strokeStyle='#2e3245'; profCtx.lineWidth=0.5;
    [0.25,0.5,0.75,1.0].forEach(f=>{
      const y=pad.t+plotH-f*plotH;
      profCtx.beginPath(); profCtx.moveTo(pad.l,y); profCtx.lineTo(pad.l+plotW,y); profCtx.stroke();
      profCtx.fillStyle='#8892a4'; profCtx.font='8px monospace';
      profCtx.fillText((f*yMax).toFixed(3),2,y+3);
    });
    profCtx.strokeStyle='#2e3245'; profCtx.lineWidth=1;
    profCtx.beginPath(); profCtx.moveTo(pad.l,pad.t+plotH); profCtx.lineTo(pad.l+plotW,pad.t+plotH); profCtx.stroke();

    toShow.forEach((p,si)=>{
      const i = activeLane===-1 ? si : activeLane;
      const col=palette(i, d.profiles.length);

      if (profileView==='profile'||profileView==='both'){
        // Background dashed
        profCtx.strokeStyle='rgba(239,83,80,0.4)'; profCtx.lineWidth=0.9; profCtx.setLineDash([3,3]);
        profCtx.beginPath();
        p.bg.forEach((v,xi)=>{ const cx=pad.l+xi*xS,cy=pad.t+plotH-v*yS; xi===0?profCtx.moveTo(cx,cy):profCtx.lineTo(cx,cy); });
        profCtx.stroke(); profCtx.setLineDash([]);

        // Clean signal line
        profCtx.strokeStyle=col; profCtx.lineWidth=1.5;
        profCtx.beginPath();
        p.clean.forEach((v,xi)=>{ const cx=pad.l+xi*xS,cy=pad.t+plotH-v*yS; xi===0?profCtx.moveTo(cx,cy):profCtx.lineTo(cx,cy); });
        profCtx.stroke();
      }

      if (profileView==='area'||profileView==='both'){
        // Filled area under clean signal
        profCtx.fillStyle=col.replace(')',',0.22)').replace('rgb','rgba').replace('#','rgba('+parseInt(col.slice(1,3),16)+','+parseInt(col.slice(3,5),16)+','+parseInt(col.slice(5,7),16)+',');
        // Simple alpha fill
        profCtx.beginPath();
        profCtx.moveTo(pad.l, pad.t+plotH);
        p.clean.forEach((v,xi)=>{ profCtx.lineTo(pad.l+xi*xS, pad.t+plotH-v*yS); });
        profCtx.lineTo(pad.l+(nPx-1)*xS, pad.t+plotH);
        profCtx.closePath();
        profCtx.globalAlpha=0.28; profCtx.fillStyle=col; profCtx.fill(); profCtx.globalAlpha=1;
        // Outline
        if (profileView==='area'){
          profCtx.strokeStyle=col; profCtx.lineWidth=1.5;
          profCtx.beginPath();
          p.clean.forEach((v,xi)=>{ const cx=pad.l+xi*xS,cy=pad.t+plotH-v*yS; xi===0?profCtx.moveTo(cx,cy):profCtx.lineTo(cx,cy); });
          profCtx.stroke();
        }
        // Area value annotation
        const areaLabel=`A=${p.area.toFixed(3)}`;
        const pkIdx=p.clean.indexOf(Math.max(...p.clean));
        profCtx.fillStyle=col; profCtx.font='bold 9px monospace';
        profCtx.fillText(areaLabel, pad.l+pkIdx*xS-20, pad.t+plotH-p.clean[pkIdx]*yS-12);
      }

      // Norm band region
      if(p.norm_y0!==null){
        const ny0=pad.t+p.norm_y0/nPx*plotH, ny1=pad.t+p.norm_y1/nPx*plotH;
        profCtx.fillStyle='rgba(129,199,132,0.10)'; profCtx.fillRect(pad.l,ny0,plotW,ny1-ny0);
        profCtx.strokeStyle='rgba(129,199,132,0.55)'; profCtx.lineWidth=1; profCtx.setLineDash([2,2]);
        profCtx.beginPath(); profCtx.moveTo(pad.l,ny0); profCtx.lineTo(pad.l+plotW,ny0);
        profCtx.moveTo(pad.l,ny1); profCtx.lineTo(pad.l+plotW,ny1); profCtx.stroke(); profCtx.setLineDash([]);
      }

      // Lane label at peak
      const pk=p.clean.indexOf(Math.max(...p.clean));
      profCtx.fillStyle=col; profCtx.font='bold 9px monospace';
      profCtx.fillText(`L${i+1}`, pad.l+pk*xS+2, pad.t+plotH-p.clean[pk]*yS-2);

      // Update table SNR + warn
      const snrEl=document.getElementById(`snr-${i}`);
      if(snrEl){ snrEl.textContent=p.snr.toFixed(1); snrEl.className=p.snr>10?'snr-good':p.snr>4?'snr-ok':'snr-bad'; }
      const wEl=document.getElementById(`warn-${i}`);
      if(wEl){ wEl.textContent=p.sat_warn?'⚠':'✓'; wEl.style.color=p.sat_warn?'#ffb74d':'#81c784';
               wEl.title=p.sat_warn?`${(p.sat_frac*100).toFixed(1)}% pixels saturated`:'OK'; }
    });

    updatePills();
  }).catch(()=>{});
}

// ── Gel switch ────────────────────────────────────────────────────────────────
function switchGel(idx){
  gelIdx=idx; activeLane=-1;
  document.querySelectorAll('.gel-tab').forEach((t,i)=>t.classList.toggle('active',i===idx));
  loadGelImage(idx); updateTable();
  document.getElementById('rb-slider').value=rbRadius[idx];
  document.getElementById('rb-val').textContent=rbRadius[idx];
}

// ── Quantify ──────────────────────────────────────────────────────────────────
function quantify(){
  const ls=lanes[gelIdx]; if(!ls.length){setStatus('⚠ No boxes yet.','amber');return;}
  setStatus('Computing…');
  const analyst=document.getElementById('inp-analyst').value;
  const notes  =document.getElementById('inp-expt').value+' '+document.getElementById('inp-notes').value;
  fetch('/quantify',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({gel_idx:gelIdx,lanes:ls,rb_radius:rbRadius[gelIdx],analyst,notes})
  }).then(r=>r.json()).then(d=>{
    if(d.error){setStatus('Error: '+d.error,'amber');return;}
    resultData[gelIdx]=d;
    const w=d.warnings||[];
    setStatus(w.length?`✓ ${d.n_lanes} lanes. ⚠ Sat: ${w.join(', ')} → Results`:`✓ ${d.n_lanes} lanes quantified → Results tab`,w.length?'amber':'green');
  });
}

// ── Results ───────────────────────────────────────────────────────────────────
function refreshResults(){
  const d=resultData[gelIdx]; if(!d){return;}
  document.getElementById('results-subtitle').textContent=`${GELS[gelIdx].name} — ${d.n_lanes} lanes`;
  fetch('/render_results',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({gel_idx:gelIdx,
      norm_mode:document.getElementById('norm-select').value,
      show_fc:document.getElementById('show-fc').checked,
      show_line:document.getElementById('show-line').checked,
      ylabel:document.getElementById('ylabel-input').value,
      xlabel:document.getElementById('xlabel-input').value})
  }).then(r=>r.json()).then(rd=>{
    if(rd.error){console.error(rd.error);return;}
    const show=(id,src,dlId)=>{ const el=document.getElementById(id); el.src='data:image/png;base64,'+src; el.style.display='block';
      document.getElementById(id.replace('-img','-ph')).style.display='none';
      if(dlId) document.getElementById(dlId).style.display='block'; };
    show('bar-img',rd.bar,'dl-bar');
    if(rd.line) show('line-img',rd.line,'dl-line');
    if(rd.rows){
      document.getElementById('data-table-wrap').style.display='block';
      document.getElementById('data-thead').innerHTML='<tr>'+rd.cols.map(c=>`<th>${c}</th>`).join('')+'</tr>';
      document.getElementById('data-tbody').innerHTML=rd.rows.map(row=>'<tr>'+row.map((v,ci)=>{
        const num=typeof v==='number'; const warn=rd.cols[ci]==='⚠ Sat'&&v==='⚠';
        return `<td class="${num?'num':''} ${warn?'warn-cell':''}">${v??'—'}</td>`;
      }).join('')+'</tr>').join('');
    }
  });
}

// ── Save ──────────────────────────────────────────────────────────────────────
function saveCSV(){
  const ls=lanes[gelIdx]; if(!ls.length){setStatus('⚠ Quantify first!','amber');return;}
  const analyst=document.getElementById('inp-analyst').value;
  const notes=document.getElementById('inp-expt').value+' '+document.getElementById('inp-notes').value;
  fetch('/save_csv',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({gel_idx:gelIdx,lanes:ls,rb_radius:rbRadius[gelIdx],analyst,notes})
  }).then(r=>r.json()).then(d=>{ if(d.path) setStatus('✓ Saved: '+d.path,'green'); else setStatus('Error: '+d.error,'amber'); });
}
function saveFig(){
  fetch('/save_fig',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({gel_idx:gelIdx,lanes:lanes[gelIdx],
      analyst:document.getElementById('inp-analyst').value,
      expt:document.getElementById('inp-expt').value})
  }).then(r=>r.json()).then(d=>{ if(d.path) setStatus('✓ Figure: '+d.path,'green'); else setStatus('Error: '+d.error,'amber'); });
}
function dlImg(imgId,fn){ const img=document.getElementById(imgId); if(!img.src) return; const a=document.createElement('a'); a.href=img.src; a.download=fn; a.click(); }

// ── Init ──────────────────────────────────────────────────────────────────────
window.addEventListener('load',()=>{ loadGelImage(0); resizeProfileCanvas(); });
</script>
</body>
</html>
"""

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    gels_json = {i:{"name":g["name"],"b64":g["b64"],"w":g["w"],"h":g["h"]} for i,g in GELS.items()}
    return render_template_string(HTML, gels=gels_json, gels_json=json.dumps(gels_json))


@app.route("/profile_data", methods=["POST"])
def profile_data():
    try:
        data=request.json; gi=data["gel_idx"]; lanes=data["lanes"]
        rb=int(data.get("rb_radius",50)); arr=GELS[gi]["arr"]
        out=[]
        for lane in lanes:
            area,clean,prof_raw,bg,x0,y0,x1,y1,snr = integrate_box(arr,lane["band"],rb)
            sat_frac=check_saturation(arr,lane["band"])
            norm_y0=norm_y1=None
            if lane.get("norm"):
                nb=lane["norm"]; norm_y0=int(nb[1]); norm_y1=int(nb[3])
            out.append({
                "clean":[round(float(v),5) for v in clean],
                "raw":  [round(float(v),5) for v in prof_raw],
                "bg":   [round(float(v),5) for v in bg],
                "area": round(area,5), "snr":round(snr,2),
                "sat_frac":round(sat_frac,4),"sat_warn":bool(sat_frac>0.02),
                "norm_y0":norm_y0,"norm_y1":norm_y1,
            })
        return jsonify({"profiles":out})
    except Exception as e:
        import traceback; return jsonify({"error":traceback.format_exc()})


@app.route("/quantify", methods=["POST"])
def quantify():
    try:
        data=request.json; gi=data["gel_idx"]
        rb=int(data.get("rb_radius",50))
        analyst=data.get("analyst",""); notes=data.get("notes","")
        df=build_df(GELS[gi]["arr"],data["lanes"],rb,analyst,notes)
        GELS[gi]["last_df"]=df
        warns=[f"L{r['lane']}" for _,r in df.iterrows() if r.get("saturation_warn")]
        return jsonify({"n_lanes":len(df),"has_lc":bool(df["area_norm_loadctrl"].notna().any()),"warnings":warns})
    except Exception as e:
        import traceback; return jsonify({"error":traceback.format_exc()})


@app.route("/render_results", methods=["POST"])
def render_results():
    try:
        data=request.json; gi=data["gel_idx"]
        norm_mode=data["norm_mode"]; show_fc=data["show_fc"]; show_line=data["show_line"]
        ylabel=data.get("ylabel","Band intensity (normalised)"); xlabel=data.get("xlabel","Lane (time point)")
        df=GELS[gi].get("last_df")
        if df is None: return jsonify({"error":"Run quantify first"})

        norm_col="area_norm_total" if norm_mode=="total" else "area_norm_loadctrl"
        fc_col  ="fc_vs_L1_total"  if norm_mode=="total" else "fc_vs_L1_loadctrl"
        if norm_col not in df or df[norm_col].isna().all(): norm_col="area_norm_total"; fc_col="fc_vs_L1_total"

        y_vals=df[norm_col].values.astype(float); x_lanes=df["lane"].values; n=len(df)
        ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M UTC")
        analyst=df["analyst"].iloc[0] if "analyst" in df else ""

        BG="#0a0a14"; MUT="#8892a4"; TXT="#e2e8f0"; BRD="#2e3245"; BARC="#4fc3f7"; FCC="#ffb74d"; WARNS="#ef5350"

        def style_ax(ax,title=""):
            ax.set_facecolor(BG); ax.tick_params(colors=MUT,labelsize=8)
            for sp in ax.spines.values(): sp.set_color(BRD)
            if title: ax.set_title(title,color=TXT,fontsize=10,pad=6)
            ax.set_xlabel(xlabel,color=MUT,fontsize=9); ax.set_ylabel(ylabel,color=MUT,fontsize=9)
            ax.grid(axis="y",color=BRD,lw=0.5,zorder=0)

        def add_stamp(fig,ax,ts,analyst):
            stamp=f"{GELS[gi]['name']}  |  {ts}"
            if analyst: stamp+=f"  |  {analyst}"
            fig.text(0.01,0.01,stamp,fontsize=6.5,color=MUT,transform=fig.transFigure,va='bottom')

        # Bar chart
        fig_b,ax_b=plt.subplots(figsize=(7,4.2),dpi=130,facecolor=BG)
        style_ax(ax_b,f"{GELS[gi]['name']} — bar chart")
        x=np.arange(n)
        bar_cols=[WARNS if bool(df["saturation_warn"].iloc[i]) else BARC for i in range(n)]
        ax_b.bar(x,y_vals,color=bar_cols,width=0.55,edgecolor="none",alpha=0.88,zorder=3)
        if show_fc and fc_col in df and df[fc_col].notna().any():
            for xi,(yv,fc) in enumerate(zip(y_vals,df[fc_col].values.astype(float))):
                if np.isfinite(yv) and np.isfinite(fc):
                    ax_b.text(xi,yv+max(y_vals)*0.02,f"{fc:.2f}×",ha="center",va="bottom",fontsize=8,color=FCC,fontweight="600")
        ax_b.set_xticks(x); ax_b.set_xticklabels([f"L{l}" for l in x_lanes],color=MUT,fontsize=8)
        ax_b.set_ylim(bottom=-max(y_vals)*0.16,top=max(y_vals)*1.2)
        for xi in range(n):
            snr=float(df["snr"].iloc[xi]) if "snr" in df else 0
            col="#81c784" if snr>10 else "#ffb74d" if snr>4 else "#ef5350"
            ax_b.text(xi,-max(y_vals)*0.08,f"SNR\n{snr:.0f}",ha="center",fontsize=6,color=col)
        if any(df["saturation_warn"]):
            from matplotlib.patches import Patch
            ax_b.legend(handles=[Patch(color=WARNS,label="⚠ Saturated"),Patch(color=BARC,label="OK")],
                        fontsize=7,framealpha=0.4,labelcolor=TXT,facecolor="#1a1d27")
        add_stamp(fig_b,ax_b,ts,analyst)
        fig_b.tight_layout(); bar_b64=fig_to_b64(fig_b)

        # Line plot
        line_b64=None
        if show_line:
            fig_l,ax_l=plt.subplots(figsize=(7,4.2),dpi=130,facecolor=BG)
            style_ax(ax_l,f"{GELS[gi]['name']} — line plot")
            ax_l.plot(x_lanes,y_vals,"o-",color=BARC,lw=2.2,markersize=7,markeredgecolor=BG,markeredgewidth=1.2,zorder=4)
            ax_l.fill_between(x_lanes,y_vals,alpha=0.10,color=BARC)
            for i in range(n):
                if bool(df["saturation_warn"].iloc[i]):
                    ax_l.plot(x_lanes[i],y_vals[i],"*",color=WARNS,markersize=12,zorder=5)
            if show_fc and fc_col in df and df[fc_col].notna().any():
                ax2=ax_l.twinx(); fc_v=df[fc_col].values.astype(float)
                ax2.plot(x_lanes,fc_v,"s--",color=FCC,lw=1.4,markersize=5,alpha=0.85)
                ax2.set_ylabel("Fold-change vs L1",color=FCC,fontsize=9); ax2.tick_params(colors=FCC,labelsize=8)
                for sp in ax2.spines.values(): sp.set_color(BRD)
                ax2.set_facecolor(BG); ax2.axhline(1,color=FCC,lw=0.8,linestyle=":",alpha=0.5)
            ax_l.set_xticks(x_lanes); ax_l.set_xticklabels([f"L{l}" for l in x_lanes],color=MUT,fontsize=8)
            ax_l.set_ylim(bottom=0)
            add_stamp(fig_l,ax_l,ts,analyst)
            fig_l.tight_layout(); line_b64=fig_to_b64(fig_l)

        # Table
        show_cols=["lane",norm_col,fc_col,"snr","saturation_warn","area_raw","lane_total_int","analyst","timestamp"]
        show_cols=[c for c in show_cols if c in df.columns]
        col_labels={"lane":"Lane","area_raw":"Raw area","lane_total_int":"Total int.",
                    "area_norm_total":"÷ total","area_norm_loadctrl":"÷ norm box",
                    "fc_vs_L1_total":"FC (total)","fc_vs_L1_loadctrl":"FC (norm)",
                    "snr":"SNR","saturation_warn":"⚠ Sat","analyst":"Analyst","timestamp":"Timestamp"}
        rows_out=[]
        for _,row in df[show_cols].iterrows():
            r=[]
            for c in show_cols:
                v=row[c]
                if c=="lane": r.append(int(v))
                elif c=="saturation_warn": r.append("⚠" if v else "✓")
                elif pd.isna(v): r.append(None)
                elif isinstance(v,str): r.append(v)
                else: r.append(round(float(v),4))
            rows_out.append(r)

        return jsonify({"bar":bar_b64,"line":line_b64,
                        "cols":[col_labels.get(c,c) for c in show_cols],"rows":rows_out})
    except Exception as e:
        import traceback; return jsonify({"error":traceback.format_exc()})


@app.route("/save_csv", methods=["POST"])
def save_csv():
    try:
        data=request.json; gi=data["gel_idx"]; rb=int(data.get("rb_radius",50))
        df=build_df(GELS[gi]["arr"],data["lanes"],rb,data.get("analyst",""),data.get("notes",""))
        out=Path(GELS[gi]["path"]).with_name(GELS[gi]["name"]+"_quantification.csv")
        df.to_csv(out,index=False); print(f"Saved: {out}")
        return jsonify({"path":str(out)})
    except Exception as e: return jsonify({"error":str(e)})


@app.route("/save_fig", methods=["POST"])
def save_fig():
    try:
        data=request.json; gi=data["gel_idx"]; lanes=data["lanes"]
        arr=GELS[gi]["arr"]; name=GELS[gi]["name"]
        analyst=data.get("analyst",""); expt=data.get("expt","")
        ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M UTC")

        fig,axes=plt.subplots(1,2,figsize=(14,6),facecolor="#0a0a14")
        for ax in axes:
            ax.set_facecolor("#0a0a14")
            for sp in ax.spines.values(): sp.set_color("#2e3245")
            ax.tick_params(colors="#8892a4",labelsize=7)
        axes[0].imshow(arr,cmap="gray",aspect="auto")
        axes[0].set_title(name,color="#e2e8f0",fontsize=10); axes[0].axis("off")
        for i,lane in enumerate(lanes):
            b=lane["band"]
            axes[0].add_patch(mpatches.Rectangle((b[0],b[1]),b[2]-b[0],b[3]-b[1],lw=1.8,edgecolor="#4fc3f7",facecolor="#4fc3f7",alpha=0.2))
            axes[0].text(b[0]+2,b[1]+2,f"L{i+1}",color="#4fc3f7",fontsize=8,fontweight="bold")
            if lane.get("norm"):
                n=lane["norm"]
                axes[0].add_patch(mpatches.Rectangle((n[0],n[1]),n[2]-n[0],n[3]-n[1],lw=1.8,edgecolor="#81c784",facecolor="#81c784",alpha=0.2))
        pal=plt.cm.plasma(np.linspace(0.08,0.92,max(len(lanes),1)))
        for i,lane in enumerate(lanes):
            _,clean,*_=integrate_box(arr,lane["band"],50)
            axes[1].plot(np.arange(len(clean)),clean,color=pal[i],lw=1.0,label=f"L{i+1}")
            axes[1].fill_between(np.arange(len(clean)),clean,alpha=0.12,color=pal[i])
        axes[1].set_title("Lane profiles + area",color="#e2e8f0",fontsize=10)
        axes[1].set_xlabel("Pixel row",color="#8892a4",fontsize=8)
        axes[1].set_ylabel("BG-subtracted intensity",color="#8892a4",fontsize=8)
        axes[1].legend(fontsize=7,framealpha=0.4,labelcolor="#e2e8f0",facecolor="#1a1d27")
        axes[1].grid(color="#2e3245",lw=0.4)
        stamp=f"{name}  |  {ts}"
        if analyst: stamp+=f"  |  {analyst}"
        if expt: stamp+=f"  |  {expt}"
        fig.text(0.01,0.005,stamp,fontsize=7,color="#8892a4",transform=fig.transFigure,va='bottom')
        out=Path(GELS[gi]["path"]).with_name(name+"_annotated.png")
        fig.savefig(out,dpi=150,bbox_inches="tight",facecolor=fig.get_facecolor())
        plt.close(fig)
        return jsonify({"path":str(out)})
    except Exception as e: return jsonify({"error":str(e)})


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv)<2: print(__doc__); sys.exit(0)
    paths=sys.argv[1:]
    for p in paths:
        if not Path(p).exists(): print(f"ERROR: {p} not found"); sys.exit(1)
    print("\n"+"="*50+"\n  Gel Picker v5\n"+"="*50)
    for p in paths: print(f"  • {p}")
    print("\n  ➜  http://localhost:5050\n  ➜  Ctrl+C to quit\n")
    init_gels(paths)
    app.run(host="127.0.0.1",port=5050,debug=False)

if __name__=="__main__":
    main()
