#!/usr/bin/env python3
"""
Written by Nithin Titta, VMware Inc. 
esxtop_viewer.py - A tiny local "Grafana-lite" for esxtop batch-mode CSV captures.

Capture data on the ESXi host, e.g.:
    esxtop -b -a -d 5 -n 720 | gzip > esxtop_capture.csv.gz

Copy it to your workstation, then:
    python esxtop_viewer.py esxtop_capture.csv.gz

Then open http://127.0.0.1:8420 (opens automatically). Search for counters
(e.g. "%RDY", "GAVG", a VM name), tick the ones you want, click "Plot selected".

Dependencies: none (Python 3.7+ standard library only).
Charting uses uPlot from a CDN. To run fully offline, download these two files:
    https://unpkg.com/uplot@1.6.31/dist/uPlot.min.css
    https://unpkg.com/uplot@1.6.31/dist/uPlot.iife.min.js
place them next to this script, and the server will serve the local copies.
"""

import argparse
import csv
import gzip
import io
import json
import math
import os
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Parsed capture lives here (loaded once at startup).
# ---------------------------------------------------------------------------
DATA = {
    "timestamps": [],   # x-axis: epoch seconds (float) or sample index
    "use_time": True,   # whether x is real time vs. synthetic index
    "counters": [],     # [{id, path, host, group, instance, metric}, ...]
    "series": [],       # parallel to counters: [[float|None, ...], ...]
}

_HERE = os.path.dirname(os.path.abspath(__file__))


def open_maybe_gzip(path):
    """Open a CSV that may be gzip-compressed, as text."""
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    return open(path, "r", newline="", encoding="utf-8", errors="replace")


def parse_timestamp(raw):
    """esxtop batch timestamps look like '06/25/2026 14:30:05' (+ optional .ms)."""
    s = raw.strip().strip('"')
    for fmt in ("%m/%d/%Y %H:%M:%S.%f", "%m/%d/%Y %H:%M:%S",
                "%d/%m/%Y %H:%M:%S.%f", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_counter_path(path):
    r"""Split a Perfmon-style path '\\HOST\Group(instance)\Metric'."""
    p = path.strip().strip('"')
    parts = [x for x in p.split("\\") if x != ""]
    host = parts[0] if parts else ""
    metric = parts[-1] if parts else p
    objpart = parts[-2] if len(parts) >= 2 else ""
    group, instance = objpart, ""
    if "(" in objpart and objpart.endswith(")"):
        group = objpart[: objpart.index("(")]
        instance = objpart[objpart.index("(") + 1 : -1]
    return host, group, instance, metric


def classify_category(group):
    """Bucket an esxtop group name into CPU / Memory / Storage / Network / Other."""
    g = group.lower()
    if any(k in g for k in ("disk", "vsan", "datastore", "scsi", "lun", "vscsi")):
        return "Storage"
    if "network" in g or "vmnic" in g or g.startswith("net") or g.endswith("nic"):
        return "Network"
    if "mem" in g or "numa" in g:
        return "Memory"
    if any(k in g for k in ("cpu", "vcpu", "interrupt", "power")):
        return "CPU"
    return "Other"


def compute_stats(vals):
    """min/max/avg/p95 over the non-null samples of one counter."""
    nums = [v for v in vals if v is not None]
    if not nums:
        return {"count": 0, "min": None, "max": None, "avg": None, "p95": None}
    n = len(nums)
    sv = sorted(nums)
    idx = max(0, min(n - 1, int(math.ceil(0.95 * n)) - 1))
    r = lambda x: round(x, 3)
    return {
        "count": n,
        "min": r(sv[0]),
        "max": r(sv[-1]),
        "avg": r(sum(nums) / n),
        "p95": r(sv[idx]),
    }


def load_csv(path):
    print("Loading %s ..." % path)
    with open_maybe_gzip(path) as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            sys.exit("Empty file.")

        # header[0] is the PDH marker; header[1:] are counter paths.
        cols = header[1:]
        counters = []
        for i, c in enumerate(cols):
            host, group, instance, metric = parse_counter_path(c)
            counters.append({
                "id": i,
                "path": c.strip().strip('"'),
                "host": host, "group": group,
                "instance": instance, "metric": metric,
            })

        series = [[] for _ in cols]
        raw_ts = []
        ncols = len(cols)
        for row in reader:
            if not row:
                continue
            raw_ts.append(parse_timestamp(row[0]))
            vals = row[1:]
            for i in range(ncols):
                v = vals[i].strip().strip('"') if i < len(vals) else ""
                if v == "":
                    series[i].append(None)
                else:
                    try:
                        series[i].append(float(v))
                    except ValueError:
                        series[i].append(None)

        if raw_ts and all(t is not None for t in raw_ts):
            x = [t.timestamp() for t in raw_ts]
            use_time = True
        else:
            x = list(range(len(raw_ts)))
            use_time = False

        for c in counters:
            c["stats"] = compute_stats(series[c["id"]])
            c["category"] = classify_category(c["group"])

    DATA["timestamps"] = x
    DATA["use_time"] = use_time
    DATA["counters"] = counters
    DATA["series"] = series
    print("Loaded %d counters x %d samples." % (len(counters), len(x)))


# ---------------------------------------------------------------------------
# Front-end (single page). Uses uPlot; falls back to local files if present.
# ---------------------------------------------------------------------------
INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>esxtop viewer</title>
<link rel="stylesheet" href="UPLOT_CSS">
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; font: 13px/1.4 system-ui, sans-serif; }
  #app { display: flex; height: 100vh; }
  #sidebar { width: 380px; flex: 0 0 380px; border-right: 1px solid #ddd;
             display: flex; flex-direction: column; padding: 10px; gap: 8px; }
  #main { flex: 1; position: relative; padding: 10px; overflow: auto; }
  .u-legend { font-size: 12px; text-align: left !important; }
  .u-legend .u-series { white-space: nowrap; }
  h1 { font-size: 16px; margin: 0; }
  #stats { color: #666; font-size: 12px; }
  #search { width: 100%; padding: 6px 8px; font-size: 13px; }
  #presets { display: flex; flex-wrap: wrap; gap: 4px; }
  .preset { font-size: 11px; padding: 2px 6px; cursor: pointer; border: 1px solid #bbb;
            background: #f5f5f5; border-radius: 3px; }
  .preset:hover { background: #e8e8e8; }
  #hotspotsBtn { padding: 8px; font-weight: 600; cursor: pointer; background: #fff3e0;
                 border: 1px solid #ffb74d; border-radius: 4px; }
  #hotspotsBtn:hover { background: #ffe0b2; }
  .hint { color: #888; font-size: 11px; }
  #toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  #toolbar button { padding: 5px 10px; cursor: pointer; }
  #list { flex: 1; overflow-y: auto; border: 1px solid #eee; }
  .cat { display: flex; align-items: center; gap: 6px; padding: 6px 8px; cursor: pointer;
         background: #eef2f7; border-bottom: 1px solid #dde3ea; user-select: none;
         position: sticky; top: 0; font-size: 13px; }
  .cat:hover { background: #e3ebf4; }
  .caret { display: inline-block; width: 10px; color: #666; }
  .catn { color: #888; font-size: 11px; margin-left: auto; }
  .item { display: flex; gap: 6px; padding: 3px 6px 3px 24px; border-bottom: 1px solid #f3f3f3;
          align-items: baseline; cursor: pointer; }
  .item:hover { background: #f7faff; }
  .item i { color: #888; font-style: normal; font-size: 11px; }
  .more { padding: 6px 6px 6px 24px; color: #999; font-style: italic; }
  #empty { position: absolute; top: 50%; left: 55%; transform: translate(-50%,-50%);
           color: #999; }
  .err { color: #c00; padding: 10px; }
</style>
</head>
<body>
<div id="app">
  <div id="sidebar">
    <h1>esxtop viewer</h1>
    <div id="stats"></div>
    <button id="hotspotsBtn" title="Find the worst instance of each key contention/latency metric and plot them together">&#128293; Find performance hotspots</button>
    <input id="search" placeholder='filter counters... e.g. "% Ready", a VM name, a datastore'>
    <div class="hint">Quick filters &mdash; click to search:</div>
    <div id="presets"></div>
    <div id="toolbar">
      <button id="plotBtn">Plot selected</button>
      <button id="clearBtn">Clear</button>
      <label>Mode
        <select id="mode">
          <option value="overlay">Overlay (raw)</option>
          <option value="normalize">Normalize 0&ndash;1</option>
          <option value="dual">Dual axis (auto)</option>
        </select>
      </label>
    </div>
    <div id="toolbar">
      <span style="color:#666">Top</span>
      <input id="topN" type="number" min="1" value="10" style="width:48px">
      <label>by
        <select id="rankBy">
          <option value="max">max</option>
          <option value="avg">avg</option>
          <option value="p95">p95</option>
        </select>
      </label>
      <button id="rankBtn" title="Rank the currently filtered counters and plot the worst N">Plot top N of filter</button>
    </div>
    <div id="toolbar">
      <button id="pngBtn">Export PNG</button>
      <button id="csvBtn">Export CSV</button>
      <span id="selcount" style="color:#666;font-size:12px;"></span>
    </div>
    <div id="list"></div>
  </div>
  <div id="main">
    <div id="chart"></div>
    <div id="empty">Select counters on the left, then &ldquo;Plot selected&rdquo;.</div>
  </div>
</div>
<script src="UPLOT_JS"></script>
<script>
let META = null, selected = new Set(), chart = null, lastData = null;
let expanded = new Set();
const CATEGORIES = ['CPU', 'Memory', 'Storage', 'Network', 'Other'];
const $ = s => document.querySelector(s);
// Friendly chip label -> the real Perfmon counter name to search for.
const PRESETS = [
  { label: 'CPU Ready',          q: '% Ready' },
  { label: 'CPU Used',           q: '% Used' },
  { label: 'CPU CoStop',         q: '% CoStop' },
  { label: 'CPU Max Limited',    q: '% Max Limited' },
  { label: 'CPU Latency',        q: '% CPU Latency' },
  { label: 'Storage lat (guest)', q: 'Average Guest MilliSec/Command' },
  { label: 'Storage lat (kernel)', q: 'Average Kernel MilliSec/Command' },
  { label: 'IOPS',               q: 'Commands/sec' },
  { label: 'Read MB/s',          q: 'MBytes Read/sec' },
  { label: 'Write MB/s',         q: 'MBytes Written/sec' },
];

// Metrics where "high == bad". Find hotspots picks the worst instance of each.
const HEALTH = [
  { metric: '% Ready' }, { metric: '% CoStop' }, { metric: '% Max Limited' },
  { metric: '% CPU Latency' }, { metric: '% Memory Latency' }, { metric: '% Swap Wait' },
  { metric: 'Average Guest MilliSec/Command' },
  { metric: 'Average Kernel MilliSec/Command' },
];

async function init() {
  if (typeof uPlot === 'undefined') {
    $('#main').innerHTML = '<div class="err">uPlot failed to load. ' +
      'See the offline instructions at the top of esxtop_viewer.py.</div>';
    return;
  }
  META = await (await fetch('/api/meta')).json();
  $('#stats').textContent = META.counters.length + ' counters \\u00b7 ' +
                            META.timestamps.length + ' samples';
  renderPresets();
  renderList('');
  updateSelCount();
  $('#search').addEventListener('input', e => applySearch(e.target.value));
  $('#plotBtn').onclick = plot;
  $('#clearBtn').onclick = () => { selected.clear(); renderList($('#search').value); updateSelCount(); };
  $('#mode').onchange = () => { if (lastData) draw(lastData); };
  $('#rankBtn').onclick = plotTopN;
  $('#pngBtn').onclick = exportPNG;
  $('#csvBtn').onclick = exportCSV;
  $('#hotspotsBtn').onclick = findHotspots;
}

// Triage entry point: for each "high == bad" metric, pick the single worst
// instance (by max), then plot them all (normalized so different scales line up).
function findHotspots() {
  const picks = [];
  HEALTH.forEach(h => {
    const cands = META.counters.filter(
      c => c.metric === h.metric && c.stats && c.stats.count && c.stats.max != null);
    if (!cands.length) return;
    cands.sort((a, b) => b.stats.max - a.stats.max);
    if (cands[0].stats.max > 0) picks.push(cands[0]);  // skip metrics with no signal
  });
  if (!picks.length) { alert('No hotspots: all key health metrics are flat at zero.'); return; }
  selected = new Set(picks.map(c => c.id));
  const list = $('#list'); list.innerHTML = '';
  const hdr = document.createElement('div'); hdr.className = 'cat';
  hdr.innerHTML = '<b>&#128293; Hotspots</b>' +
                  '<span class="catn">worst instance per key metric</span>';
  list.appendChild(hdr);
  picks.sort((a, b) => b.stats.max - a.stats.max).forEach(c => list.appendChild(makeRow(c)));
  updateSelCount();
  $('#mode').value = 'normalize';
  fetchAndDraw([...selected]);
}

function currentMatches(q) {
  q = (q || '').trim().toLowerCase();
  return q ? META.counters.filter(c => c.path.toLowerCase().includes(q)) : META.counters;
}

function labelFor(id) {
  const c = META.counters[id];
  return c.metric + ' ' + c.group + (c.instance ? '(' + c.instance + ')' : '');
}

function renderPresets() {
  const box = $('#presets'); box.innerHTML = '';
  PRESETS.forEach(p => {
    const b = document.createElement('button');
    b.className = 'preset'; b.textContent = p.label; b.title = 'search: ' + p.q;
    b.onclick = () => applySearch(p.q);
    box.appendChild(b);
  });
}

function updateSelCount() { $('#selcount').textContent = selected.size + ' selected'; }

// Typing a search (or clicking a preset) auto-expands the categories that match,
// so results are visible without manually opening each section.
function applySearch(q) {
  $('#search').value = q;
  if ((q || '').trim()) expanded = new Set(currentMatches(q).map(c => c.category));
  renderList(q);
}

function makeRow(c) {
  const row = document.createElement('label'); row.className = 'item';
  const cb = document.createElement('input'); cb.type = 'checkbox';
  cb.checked = selected.has(c.id);
  cb.onchange = () => { cb.checked ? selected.add(c.id) : selected.delete(c.id); updateSelCount(); };
  const txt = document.createElement('span');
  const inst = c.instance ? '(' + c.instance + ')' : '';
  const s = c.stats || {};
  const stat = s.count ? '<i> &middot; avg ' + s.avg + ' &middot; max ' + s.max +
                         ' &middot; p95 ' + s.p95 + '</i>' : '';
  txt.innerHTML = '<b>' + c.metric + '</b> <i>' + c.group + inst + '</i>' + stat;
  row.appendChild(cb); row.appendChild(txt);
  return row;
}

function renderList(q) {
  const list = $('#list'); list.innerHTML = '';
  const matches = currentMatches(q);
  const buckets = {}; CATEGORIES.forEach(c => buckets[c] = []);
  for (const c of matches) (buckets[c.category] || buckets['Other']).push(c);
  const PER_CAT = 400;
  CATEGORIES.forEach(cat => {
    const items = buckets[cat];
    if (!items.length) return;
    const open = expanded.has(cat);
    const hdr = document.createElement('div'); hdr.className = 'cat';
    hdr.innerHTML = '<span class="caret">' + (open ? '\\u25be' : '\\u25b8') + '</span>' +
                    '<b>' + cat + '</b><span class="catn">' + items.length + '</span>';
    hdr.onclick = () => { open ? expanded.delete(cat) : expanded.add(cat); renderList(q); };
    list.appendChild(hdr);
    if (!open) return;
    items.slice(0, PER_CAT).forEach(c => list.appendChild(makeRow(c)));
    if (items.length > PER_CAT) {
      const m = document.createElement('div'); m.className = 'more';
      m.textContent = '... ' + (items.length - PER_CAT) + ' more in ' + cat + ' - refine your filter';
      list.appendChild(m);
    }
  });
  if (!matches.length) {
    const m = document.createElement('div'); m.className = 'more';
    m.textContent = 'No counters match "' + q + '".';
    list.appendChild(m);
  }
}

async function fetchAndDraw(ids) {
  if (!ids.length) return;
  const series = await (await fetch('/api/series?ids=' + ids.join(','))).json();
  lastData = { ids, series };
  draw(lastData);
}

async function plot() {
  if (selected.size === 0) return;
  await fetchAndDraw([...selected]);
}

function plotTopN() {
  const n = Math.max(1, parseInt($('#topN').value, 10) || 10);
  const by = $('#rankBy').value;
  const ranked = currentMatches($('#search').value)
    .filter(c => c.stats && c.stats.count && c.stats[by] != null)
    .sort((a, b) => b.stats[by] - a.stats[by])
    .slice(0, n);
  if (!ranked.length) return;
  selected = new Set(ranked.map(c => c.id));
  renderList($('#search').value);
  updateSelCount();
  fetchAndDraw([...selected]);
}

function normalize(a) {
  let mn = Infinity, mx = -Infinity;
  for (const v of a) { if (v == null) continue; if (v < mn) mn = v; if (v > mx) mx = v; }
  if (!isFinite(mn) || mx === mn) return a.map(v => v == null ? null : 0);
  return a.map(v => v == null ? null : (v - mn) / (mx - mn));
}

function color(i) { return 'hsl(' + ((i * 67) % 360) + ',70%,45%)'; }

// Assign series to left ('y') or right ('y2') axis by magnitude: if the maxima
// span more than ~6x, split at the largest gap (smaller magnitudes go right).
function splitAxes(ids, series) {
  const axis = {}; ids.forEach(id => axis[id] = 'y');
  const maxes = ids.map(id => {
    let m = 0;
    for (const v of (series[id] || [])) { if (v != null && Math.abs(v) > m) m = Math.abs(v); }
    return { id, m };
  }).filter(x => x.m > 0).sort((a, b) => a.m - b.m);
  if (maxes.length < 2) return axis;
  let gapIdx = -1, gapSize = 0;
  for (let i = 1; i < maxes.length; i++) {
    const g = Math.log10(maxes[i].m) - Math.log10(maxes[i - 1].m);
    if (g > gapSize) { gapSize = g; gapIdx = i; }
  }
  if (gapSize < 0.8) return axis;
  for (let i = 0; i < gapIdx; i++) axis[maxes[i].id] = 'y2';
  return axis;
}

function draw({ ids, series }) {
  $('#empty').style.display = 'none';
  const mode = $('#mode').value;
  const data = [META.timestamps];
  // uPlot draws its legend (with live hover values) below the canvas. Reserve
  // vertical room for it so it isn't clipped, and let #main scroll if it's tall.
  const legendReserve = Math.min(300, 40 + ids.length * 22);
  const opts = {
    width: $('#main').clientWidth - 20,
    height: Math.max(220, $('#main').clientHeight - 30 - legendReserve),
    series: [{}],
    scales: { x: { time: META.use_time } },
    axes: [{}, {}],
    legend: { show: true },
  };
  let axisOf = {};
  if (mode === 'dual') {
    axisOf = splitAxes(ids, series);
    if (ids.some(id => axisOf[id] === 'y2')) {
      opts.scales.y2 = {};
      opts.axes.push({ scale: 'y2', side: 1 });
    }
  }
  ids.forEach((id, i) => {
    let vals = series[id] || [];
    if (mode === 'normalize') vals = normalize(vals);
    data.push(vals);
    const s = { label: labelFor(id), stroke: color(i), width: 1, points: { show: false } };
    if (mode === 'dual') s.scale = axisOf[id];
    opts.series.push(s);
  });
  if (chart) chart.destroy();
  chart = new uPlot(opts, data, $('#chart'));
}

function exportPNG() {
  if (!chart) return;
  const can = chart.root.querySelector('canvas');
  const a = document.createElement('a');
  a.href = can.toDataURL('image/png');
  a.download = 'esxtop-chart.png';
  a.click();
}

function csvCell(v) {
  const s = String(v);
  return /[",\\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

function exportCSV() {
  if (!lastData) return;
  const { ids, series } = lastData;
  const x = META.timestamps, useTime = META.use_time;
  const rows = [['time', ...ids.map(labelFor)].map(csvCell).join(',')];
  for (let r = 0; r < x.length; r++) {
    const t = useTime ? new Date(x[r] * 1000).toISOString() : x[r];
    const cols = [csvCell(t)];
    ids.forEach(id => { const v = (series[id] || [])[r]; cols.push(v == null ? '' : v); });
    rows.push(cols.join(','));
  }
  const blob = new Blob([rows.join('\\n')], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'esxtop-data.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

window.addEventListener('resize', () => { if (lastData) draw(lastData); });
init();
</script>
</body>
</html>
"""


def resolve_assets():
    """Use local uPlot files if present (offline), else the CDN."""
    css_local = os.path.join(_HERE, "uPlot.min.css")
    js_local = os.path.join(_HERE, "uPlot.iife.min.js")
    css = "/uPlot.min.css" if os.path.exists(css_local) else \
        "https://unpkg.com/uplot@1.6.31/dist/uPlot.min.css"
    js = "/uPlot.iife.min.js" if os.path.exists(js_local) else \
        "https://unpkg.com/uplot@1.6.31/dist/uPlot.iife.min.js"
    return css, js


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet

    def _send(self, body, ctype="application/json", code=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        encoding = None
        if "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 1024:
            body = gzip.compress(body)
            encoding = "gzip"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                self._send(f.read(), ctype)
        except OSError:
            self._send("not found", "text/plain", 404)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route == "/" or route == "/index.html":
            css, js = resolve_assets()
            html = INDEX_HTML.replace("UPLOT_CSS", css).replace("UPLOT_JS", js)
            return self._send(html, "text/html; charset=utf-8")

        if route == "/uPlot.min.css":
            return self._send_file(os.path.join(_HERE, "uPlot.min.css"), "text/css")
        if route == "/uPlot.iife.min.js":
            return self._send_file(os.path.join(_HERE, "uPlot.iife.min.js"),
                                   "application/javascript")

        if route == "/api/meta":
            return self._send(json.dumps({
                "timestamps": DATA["timestamps"],
                "use_time": DATA["use_time"],
                "counters": DATA["counters"],
            }))

        if route == "/api/series":
            qs = parse_qs(parsed.query)
            ids_raw = qs.get("ids", [""])[0]
            out = {}
            for tok in ids_raw.split(","):
                tok = tok.strip()
                if not tok:
                    continue
                try:
                    idx = int(tok)
                except ValueError:
                    continue
                if 0 <= idx < len(DATA["series"]):
                    out[str(idx)] = DATA["series"][idx]
            return self._send(json.dumps(out))

        self._send("not found", "text/plain", 404)


def main():
    ap = argparse.ArgumentParser(description="Local web viewer for esxtop batch CSV.")
    ap.add_argument("csv", help="esxtop batch CSV file (.csv or .csv.gz)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8420)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        sys.exit("File not found: %s" % args.csv)

    load_csv(args.csv)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d" % (args.host, args.port)
    print("Serving at %s  (Ctrl+C to stop)" % url)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
