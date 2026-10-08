#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
power_monitor.py — 轻量级 macOS 功率实时监控（三曲线 + 时间范围选择）

周期性调用 `ioreg` 读取电池遥测，绘制三条实时功率曲线：
  - 总功率   = BatteryData.AdapterPower 或 PowerTelemetryData.SystemPowerIn / 1000
  - 系统耗电 = BatteryData.SystemPower 或 PowerTelemetryData.SystemLoad / 1000
  - 充电功率 = 电池电压 × 电流             (充入电池的功率, W; 负值=放电)
关系约为：总功率 ≈ 系统耗电 + 充电功率（差值来自充电转换损耗与采样误差）。
另外叠加一条电池电量(%)曲线，绘制在图表右侧的独立 0~100% 坐标轴上。

数据追加写入 CSV(全量历史)，并通过内置的本地 HTTP 服务提供实时曲线页面，
页面支持选择查看的时间范围：近一天/两天/三天/一周/自定义。

数据来源策略：
  - 内存保留最近约 RETAIN_DAYS 天的全精度样本(启动时从 CSV 预加载，跨会话不丢历史)；
  - 按所选时间窗切片后降采样到 ~PLOT_POINTS 个点再返回，保证传输量小、轻量；
  - 自定义范围若早于内存窗口，则按需直接扫描 CSV。

资源占用：采样线程绝大多数时间在 sleep，每个采样点只调用一次 ioreg；
HTTP 服务空闲时阻塞在 accept 上，几乎不占 CPU；仅用 Python 标准库，无需联网。

配置(环境变量，均可选)：
  PM_INTERVAL    采样间隔秒数              默认 5
  PM_PORT        本地 HTTP 端口            默认 8765
  PM_HOST        绑定地址                  默认 127.0.0.1
  PM_DATA_DIR    数据目录                  默认 <脚本目录>/data
  PM_RETAIN_DAYS 内存保留天数              默认 8
  PM_PLOT_POINTS 单次返回(绘图)的最大点数  默认 2500
  PM_MAX_POINTS  内存样本数安全上限        默认 300000
"""

import bisect
import json
import math
import os
import plistlib
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------- 配置 ----------------
INTERVAL = float(os.environ.get("PM_INTERVAL", "5"))
PORT = int(os.environ.get("PM_PORT", "8765"))
HOST = os.environ.get("PM_HOST", "127.0.0.1")
DATA_DIR = os.environ.get("PM_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data"
)
RETAIN_DAYS = float(os.environ.get("PM_RETAIN_DAYS", "8"))
RETAIN_SECONDS = RETAIN_DAYS * 86400
PLOT_POINTS = int(os.environ.get("PM_PLOT_POINTS", "2500"))
MAX_POINTS = int(os.environ.get("PM_MAX_POINTS", "300000"))
CSV_PATH = os.path.join(DATA_DIR, "power.csv")

# 预设时间范围(天)
PRESETS = {"1d": 1, "2d": 2, "3d": 3, "7d": 7}

# ---------------- 共享状态 ----------------
_lock = threading.Lock()
_t = []            # epoch 秒(升序)
_charge = []       # 充电功率(W)，正=充入 负=放电
_total = []        # 总功率/适配器输入(W)
_system = []       # 系统耗电(W)
_soc = []          # 电池电量(%)
_latest = {}       # 最近一条样本
_adapter = {}      # 电源适配器信息
_battery_present = True
_running = True


def to_signed(x):
    """ioreg 中电流负值会以无符号整数出现，这里转回有符号。"""
    if x is None:
        return None
    x = int(x)
    if x >= 2 ** 63:        # 64 位补码回绕
        x -= 2 ** 64
    if x >= 2 ** 31:        # 32 位补码回绕(电流量级远小于此，超过即为负值回绕)
        x -= 2 ** 32
    return x


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def read_battery():
    """调用 ioreg 读取一次电池字典，失败或无电池返回 None。"""
    try:
        out = subprocess.run(
            ["ioreg", "-a", "-r", "-c", "AppleSmartBattery"],
            capture_output=True, timeout=5,
        ).stdout
        if not out:
            return None
        data = plistlib.loads(out)
        if not data:
            return None
        return data[0]
    except Exception:
        return None


def sample_once():
    info = read_battery()
    global _battery_present
    if not info:
        _battery_present = False
        return None
    _battery_present = True

    volt_mv = info.get("Voltage")
    amp_ma = info.get("Amperage")
    if amp_ma is None:
        amp_ma = info.get("InstantAmperage")
    amp_ma = to_signed(amp_ma)
    if volt_mv is None or amp_ma is None:
        return None

    volts = volt_mv / 1000.0
    amps = amp_ma / 1000.0
    charge = volts * amps                      # 充电功率(入电池)，正=充 负=放

    charging = bool(info.get("IsCharging", False))
    external = bool(info.get("ExternalConnected", False))

    # 旧字段以 W 提供；macOS 27 本机仅提供 PowerTelemetryData 的瞬时 mW 字段。
    bd = info.get("BatteryData") or {}
    total = bd.get("AdapterPower")
    system = bd.get("SystemPower")
    total = float(total) if isinstance(total, (int, float)) else None
    system = float(system) if isinstance(system, (int, float)) else None
    telemetry = info.get("PowerTelemetryData") or {}
    if total is None:
        value = telemetry.get("SystemPowerIn")
        if isinstance(value, (int, float)):
            total = value / 1000.0
    if system is None:
        value = telemetry.get("SystemLoad")
        if isinstance(value, (int, float)):
            system = value / 1000.0
    if total is None and not external:
        total = 0.0
    if system is None and not external and charge < 0:
        system = -charge

    # 电量百分比(兼容 Intel 与 Apple Silicon 不同字段)
    soc = None
    raw_cur = info.get("AppleRawCurrentCapacity")
    raw_max = info.get("AppleRawMaxCapacity")
    if raw_cur is not None and raw_max:
        soc = 100.0 * raw_cur / raw_max
    else:
        cur = info.get("CurrentCapacity")
        mx = info.get("MaxCapacity")
        if cur is not None and mx:
            soc = 100.0 * cur / mx

    adapter = {}
    ad = info.get("AdapterDetails") or {}
    if ad:
        av = ad.get("AdapterVoltage")
        ac = ad.get("Current")
        adapter = {
            "watts": ad.get("Watts"),
            "desc": ad.get("Description") or ad.get("Name"),
            "voltage": (av / 1000.0) if av else None,
            "current": (ac / 1000.0) if ac else None,
        }

    return {
        "t": time.time(),
        "charge": charge,
        "total": total,
        "system": system,
        "v": volts,
        "a": amps,
        "soc": soc,
        "charging": charging,
        "external": external,
        "battery_present": True,
        "adapter": adapter,
    }


def ensure_csv():
    new = not os.path.exists(CSV_PATH)
    f = open(CSV_PATH, "a", buffering=1)  # 行缓冲：每写一行即落盘
    if new:
        f.write("iso_time,epoch,charge_w,total_w,system_w,volts,amps,"
                "soc_pct,is_charging,external,adapter_watts\n")
    return f


def _fmt(x, fmt="%.3f"):
    return (fmt % x) if isinstance(x, (int, float)) else ""


def preload_history():
    """启动时把 CSV 中最近 RETAIN_DAYS 天的数据载入内存(跨会话保留历史)。"""
    if not os.path.exists(CSV_PATH):
        return
    cutoff = time.time() - RETAIN_SECONDS
    rows = []
    try:
        with open(CSV_PATH) as f:
            f.readline()  # 跳过表头
            for line in f:
                p = line.rstrip("\n").split(",")
                if len(p) < 5:
                    continue
                ep = _num(p[1])
                if ep is None or ep < cutoff:
                    continue
                soc = _num(p[7]) if len(p) > 7 else None
                rows.append((ep, _num(p[2]), _num(p[3]), _num(p[4]), soc))
    except Exception:
        return
    rows.sort(key=lambda r: r[0])
    with _lock:
        for ep, c, tot, sysw, soc in rows:
            _t.append(ep); _charge.append(c)
            _total.append(tot); _system.append(sysw); _soc.append(soc)


def sampler_loop():
    global _adapter
    f = ensure_csv()
    try:
        while _running:
            start = time.time()
            s = sample_once()
            if s:
                with _lock:
                    _t.append(s["t"])
                    _charge.append(s["charge"])
                    _total.append(s["total"])
                    _system.append(s["system"])
                    _soc.append(s["soc"])
                    # 时间窗裁剪：丢弃早于 RETAIN_SECONDS 的样本
                    cutoff = s["t"] - RETAIN_SECONDS
                    idx = bisect.bisect_left(_t, cutoff)
                    if idx > 0:
                        del _t[:idx]; del _charge[:idx]
                        del _total[:idx]; del _system[:idx]; del _soc[:idx]
                    # 安全上限
                    if len(_t) > MAX_POINTS:
                        cut = len(_t) - MAX_POINTS
                        del _t[:cut]; del _charge[:cut]
                        del _total[:cut]; del _system[:cut]; del _soc[:cut]
                    _latest.clear()
                    _latest.update(s)
                    _adapter = s["adapter"]
                iso = datetime.fromtimestamp(s["t"]).isoformat(timespec="seconds")
                aw = (s["adapter"] or {}).get("watts")
                row = [
                    iso, "%.0f" % s["t"],
                    _fmt(s["charge"]), _fmt(s["total"]), _fmt(s["system"]),
                    _fmt(s["v"]), _fmt(s["a"]), _fmt(s["soc"], "%.1f"),
                    "1" if s["charging"] else "0",
                    "1" if s["external"] else "0",
                    (str(aw) if aw is not None else ""),
                ]
                f.write(",".join(row) + "\n")
            # 休眠到下一个采样点(扣除本次耗时)，并能尽快响应停止
            sleep = INTERVAL - (time.time() - start)
            while sleep > 0 and _running:
                time.sleep(min(0.5, sleep))
                sleep -= 0.5
    finally:
        try:
            f.close()
        except Exception:
            pass


# ---------------- 数据查询 / 降采样 ----------------
def get_window(qs):
    """解析查询参数，返回 (t_from, t_to) epoch 秒。"""
    now = time.time()
    if "from" in qs or "to" in qs:
        t_to = _num((qs.get("to") or [None])[0])
        t_from = _num((qs.get("from") or [None])[0])
        if t_to is None:
            t_to = now
        if t_from is None:
            t_from = t_to - 86400
        if t_from > t_to:
            t_from, t_to = t_to, t_from
        return t_from, t_to
    rng = (qs.get("range") or ["1d"])[0]
    days = PRESETS.get(rng, 1)
    return now - days * 86400, now


def _slice_mem(t_from, t_to):
    """在持有 _lock 时调用；返回窗口内样本的拷贝。"""
    lo = bisect.bisect_left(_t, t_from)
    hi = bisect.bisect_right(_t, t_to)
    return (_t[lo:hi], _charge[lo:hi], _total[lo:hi],
            _system[lo:hi], _soc[lo:hi])


def _read_csv_window(t_from, t_to):
    """从 CSV 读取时间窗内的样本(用于早于内存窗口的自定义范围)。"""
    T = []; C = []; TO = []; SY = []; SO = []
    try:
        with open(CSV_PATH) as f:
            f.readline()
            for line in f:
                p = line.rstrip("\n").split(",")
                if len(p) < 5:
                    continue
                ep = _num(p[1])
                if ep is None or ep < t_from or ep > t_to:
                    continue
                T.append(ep); C.append(_num(p[2]))
                TO.append(_num(p[3])); SY.append(_num(p[4]))
                SO.append(_num(p[7]) if len(p) > 7 else None)
    except FileNotFoundError:
        pass
    return T, C, TO, SY, SO


def _downsample(t, c, tot, sysw, soc, maxp):
    """按桶平均降采样到 <= maxp 个点；保留 None。"""
    n = len(t)
    if n <= maxp:
        return t, c, tot, sysw, soc
    k = math.ceil(n / maxp)
    T = []; C = []; TO = []; SY = []; SO = []
    for i in range(0, n, k):
        bt = t[i:i + k]
        T.append(sum(bt) / len(bt))
        for src, dst in ((c, C), (tot, TO), (sysw, SY), (soc, SO)):
            vals = [x for x in src[i:i + k] if x is not None]
            dst.append(sum(vals) / len(vals) if vals else None)
    return T, C, TO, SY, SO


def _rnd(arr):
    return [round(x, 3) if isinstance(x, (int, float)) else None for x in arr]


def build_payload(qs):
    t_from, t_to = get_window(qs)
    with _lock:
        earliest = _t[0] if _t else None
        latest = dict(_latest)
        adapter = dict(_adapter)
        bp = _battery_present
        if earliest is not None and t_from >= earliest:
            t, c, tot, sysw, soc = _slice_mem(t_from, t_to)
            need_csv = False
        else:
            t = c = tot = sysw = soc = None
            need_csv = True
    if need_csv:
        t, c, tot, sysw, soc = _read_csv_window(t_from, t_to)
    raw = len(t)
    t, c, tot, sysw, soc = _downsample(t, c, tot, sysw, soc, PLOT_POINTS)
    return {
        "interval": INTERVAL,
        "battery_present": bp,
        "range": {"from": int(t_from), "to": int(t_to)},
        "count": raw,
        "t": [int(x) for x in t],
        "charge": _rnd(c),
        "total": _rnd(tot),
        "system": _rnd(sysw),
        "soc": _rnd(soc),
        "latest": latest,
        "adapter": adapter,
    }


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>功率实时监控</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,sans-serif;
         background:#0e1116; color:#e6edf3; }
  header { padding:14px 20px 0; display:flex; flex-wrap:wrap; align-items:center; gap:14px; }
  h1 { font-size:15px; margin:0; font-weight:600; color:#9aa7b4; letter-spacing:.5px; }
  .badge { padding:4px 11px; border-radius:999px; font-size:12px; font-weight:600; background:#333; }
  .cards { display:flex; flex-wrap:wrap; gap:12px; padding:14px 20px 6px; }
  .card { flex:1 1 150px; min-width:140px; border:1px solid #21262d; border-left-width:4px;
          border-radius:10px; padding:10px 14px; background:#11161d; }
  .card .lbl { font-size:12px; color:#9aa7b4; }
  .card .val { font-size:30px; font-weight:700; font-variant-numeric:tabular-nums; margin-top:2px; }
  .stats { display:flex; flex-wrap:wrap; gap:22px; padding:6px 20px 2px; color:#9aa7b4; font-size:13px; }
  .stats b { color:#e6edf3; font-weight:600; font-variant-numeric:tabular-nums; }
  .controls { display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:10px 20px 2px;
              color:#9aa7b4; font-size:13px; }
  .controls button { background:#161b22; color:#c9d1d9; border:1px solid #30363d; border-radius:7px;
                     padding:5px 12px; font-size:13px; cursor:pointer; }
  .controls button:hover { background:#1f2630; }
  .controls button.active { background:#1f6feb; border-color:#1f6feb; color:#fff; }
  .controls input[type=datetime-local] { background:#0d1117; color:#e6edf3; border:1px solid #30363d;
                     border-radius:6px; padding:4px 8px; font-size:13px; }
  #custom-box { display:none; align-items:center; gap:6px; }
  #wrap { padding:8px 12px 16px; }
  canvas { width:100%; height:50vh; display:block; cursor:crosshair; }
  .muted { color:#6b7785; font-size:12px; padding:0 20px 16px; min-height:14px; }
  #tip { position:fixed; z-index:50; display:none; pointer-events:none; white-space:nowrap;
         background:rgba(17,22,29,0.96); border:1px solid #30363d; border-radius:8px;
         padding:8px 10px; font-size:12px; color:#e6edf3; box-shadow:0 4px 16px rgba(0,0,0,.5); }
  #tip .tt-time { color:#9aa7b4; margin-bottom:5px; font-variant-numeric:tabular-nums; }
  #tip .tt-row { display:flex; align-items:center; gap:6px; line-height:1.7; }
  #tip .tt-row b { margin-left:auto; padding-left:18px; font-variant-numeric:tabular-nums; }
  #tip .tt-dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
</style>
</head>
<body>
<header>
  <h1>⚡ 功率实时监控</h1>
  <div class="badge" id="state">--</div>
</header>
<div class="cards">
  <div class="card" style="border-left-color:#58a6ff">
    <div class="lbl">总功率 · 适配器输入</div>
    <div class="val" id="v-total" style="color:#58a6ff">--</div>
  </div>
  <div class="card" style="border-left-color:#e3b341">
    <div class="lbl">系统实时耗电</div>
    <div class="val" id="v-system" style="color:#e3b341">--</div>
  </div>
  <div class="card" style="border-left-color:#3fb950">
    <div class="lbl">充电功率 · 入电池</div>
    <div class="val" id="v-charge" style="color:#3fb950">--</div>
  </div>
  <div class="card" style="border-left-color:#bc8cff">
    <div class="lbl">电池电量</div>
    <div class="val" id="v-soc" style="color:#bc8cff">--</div>
  </div>
</div>
<div class="stats">
  <div>电压 <b id="volt">--</b></div>
  <div>电流 <b id="amp">--</b></div>
  <div>电量 <b id="soc">--</b></div>
  <div>适配器额定 <b id="adapter">--</b></div>
  <div>采样间隔 <b id="iv">--</b></div>
  <div>样本数 <b id="cnt">--</b></div>
</div>
<div class="controls">
  <span>时间范围：</span>
  <button data-days="1">近一天</button>
  <button data-days="2">近两天</button>
  <button data-days="3">近三天</button>
  <button data-days="7">一周内</button>
  <button id="btn-custom">自定义</button>
  <span id="custom-box">
    <input type="datetime-local" id="c-from">
    <span>—</span>
    <input type="datetime-local" id="c-to">
    <button id="c-apply">应用</button>
  </span>
</div>
<div id="wrap"><canvas id="chart"></canvas></div>
<div class="muted" id="msg"></div>
<div id="tip"></div>
<script>
const $ = id => document.getElementById(id);
let interval = 5000;
let sel = { mode:'preset', days:1 };           // 当前选中的时间范围
let cache = { t:[], total:[], system:[], charge:[], soc:[] };   // 最近一次返回的数据(供悬浮/重绘)
let hoverX = null, hoverCX = 0, hoverCY = 0, rafPending = false;  // 鼠标悬浮状态
const SERIES = [
  { key:'total',  name:'总功率',   color:'#58a6ff' },
  { key:'system', name:'系统耗电', color:'#e3b341' },
  { key:'charge', name:'充电功率', color:'#3fb950' },
  { key:'soc',    name:'电池电量', color:'#bc8cff', axis:'right', pct:true },
];
const pad = n => String(n).padStart(2,'0');

function fmtTick(ep, span){
  const d=new Date(ep*1000);
  if(span>36*3600) return pad(d.getMonth()+1)+'-'+pad(d.getDate())+' '+pad(d.getHours())+':'+pad(d.getMinutes());
  return pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds());
}
function toLocalInput(d){
  return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+'T'+pad(d.getHours())+':'+pad(d.getMinutes());
}
function buildQuery(){
  if(sel.mode==='custom') return '?from='+Math.floor(sel.from)+'&to='+Math.floor(sel.to);
  return '?range='+sel.days+'d';
}
function setActive(){
  document.querySelectorAll('.controls button[data-days]').forEach(b=>{
    b.classList.toggle('active', sel.mode==='preset' && (+b.dataset.days)===sel.days);
  });
  $('btn-custom').classList.toggle('active', sel.mode==='custom');
}

function nearestIndex(arr, tm){
  const n=arr.length; if(!n) return -1;
  if(tm<=arr[0]) return 0;
  if(tm>=arr[n-1]) return n-1;
  let lo=0, hi=n-1;
  while(lo<hi){ const mid=(lo+hi)>>1; if(arr[mid]<tm) lo=mid+1; else hi=mid; }
  return (lo>0 && (tm-arr[lo-1])<(arr[lo]-tm)) ? lo-1 : lo;
}

function showTip(ep, data, idx){
  const tip=$('tip'), d=new Date(ep*1000);
  const ts=d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+' '+
           pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds());
  let html='<div class="tt-time">'+ts+'</div>';
  for(const s of SERIES){
    const v=(data[s.key]||[])[idx];
    let val='--';
    if(v!=null) val = s.pct ? (v.toFixed(1)+' %') : ((s.key==='charge'&&v>=0?'+':'')+v.toFixed(1)+' W');
    html+='<div class="tt-row"><span class="tt-dot" style="background:'+s.color+'"></span>'+s.name+'<b>'+val+'</b></div>';
  }
  tip.innerHTML=html; tip.style.display='block';
  const gap=14, tw=tip.offsetWidth, th=tip.offsetHeight;
  let x=hoverCX+gap, y=hoverCY+gap;
  if(x+tw>window.innerWidth-8) x=hoverCX-tw-gap;
  if(y+th>window.innerHeight-8) y=hoverCY-th-gap;
  tip.style.left=Math.max(8,x)+'px'; tip.style.top=Math.max(8,y)+'px';
}

function draw(t, data){
  const cv=$('chart'), dpr=window.devicePixelRatio||1;
  const cssW=cv.clientWidth, cssH=cv.clientHeight;
  cv.width=Math.round(cssW*dpr); cv.height=Math.round(cssH*dpr);
  const ctx=cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,cssW,cssH);
  const mL=58,mR=46,mT=14,mB=30, pW=cssW-mL-mR, pH=cssH-mT-mB;
  if(!t.length){ $('tip').style.display='none'; ctx.fillStyle='#6b7785'; ctx.font='13px sans-serif'; ctx.fillText('暂无数据',mL,mT+20); return; }
  let wmin=0, wmax=1;
  for(const s of SERIES){ if(s.axis==='right') continue; const arr=data[s.key]||[]; for(let i=0;i<arr.length;i++){ const v=arr[i]; if(v==null)continue; if(v<wmin)wmin=v; if(v>wmax)wmax=v; } }
  wmax += (wmax-wmin)*0.1;
  if(wmin<0) wmin -= (wmax-wmin)*0.05;
  const tmin=t[0], tmax=(t[t.length-1]>tmin)?t[t.length-1]:tmin+1, span=tmax-tmin;
  const X=v=> mL+(v-tmin)/(tmax-tmin)*pW;
  const Y=v=> mT+(1-(v-wmin)/(wmax-wmin))*pH;
  const YR=v=> mT+(1-v/100)*pH;                    // 右轴：电量 0~100%
  const Ykey=s=> (s.axis==='right'? YR : Y);
  ctx.font='11px -apple-system,sans-serif'; ctx.lineWidth=1; ctx.textBaseline='middle';
  for(let i=0;i<=5;i++){
    const val=wmin+(wmax-wmin)*i/5, y=Y(val);
    ctx.strokeStyle='#1c2128'; ctx.beginPath(); ctx.moveTo(mL,y); ctx.lineTo(mL+pW,y); ctx.stroke();
    ctx.fillStyle='#6b7785'; ctx.textAlign='right'; ctx.fillText(val.toFixed(1)+'W', mL-8, y);
    ctx.fillStyle='#bc8cff'; ctx.textAlign='left'; ctx.fillText(Math.round(100*i/5)+'%', mL+pW+8, y);
  }
  if(wmin<0){ const y0=Y(0); ctx.strokeStyle='#30363d'; ctx.beginPath(); ctx.moveTo(mL,y0); ctx.lineTo(mL+pW,y0); ctx.stroke(); }
  ctx.fillStyle='#6b7785'; ctx.textAlign='center'; ctx.textBaseline='top';
  const xt=Math.min(6,t.length);
  for(let i=0;i<xt;i++){
    const idx=Math.round(i*(t.length-1)/Math.max(1,xt-1)), x=X(t[idx]);
    ctx.fillText(fmtTick(t[idx],span), x, mT+pH+8);
  }
  for(const s of SERIES){
    const arr=data[s.key]||[], Yf=Ykey(s);
    ctx.beginPath(); let started=false;
    for(let i=0;i<t.length;i++){
      const v=arr[i];
      if(v==null){ started=false; continue; }
      const x=X(t[i]), y=Yf(v);
      if(started) ctx.lineTo(x,y); else { ctx.moveTo(x,y); started=true; }
    }
    ctx.strokeStyle=s.color; ctx.lineWidth=2; ctx.stroke();
    for(let i=t.length-1;i>=0;i--){ if(arr[i]!=null){ ctx.fillStyle=s.color; ctx.beginPath(); ctx.arc(X(t[i]),Yf(arr[i]),3,0,Math.PI*2); ctx.fill(); break; } }
  }
  // 鼠标悬浮：竖直参考线 + 高亮点 + 数值提示
  if(hoverX!=null){
    const mx=Math.max(mL, Math.min(mL+pW, hoverX));
    const idx=nearestIndex(t, tmin+(mx-mL)/pW*(tmax-tmin));
    if(idx>=0){
      const px=X(t[idx]);
      ctx.strokeStyle='rgba(230,237,243,0.28)'; ctx.lineWidth=1;
      ctx.beginPath(); ctx.moveTo(px,mT); ctx.lineTo(px,mT+pH); ctx.stroke();
      for(const s of SERIES){
        const v=(data[s.key]||[])[idx]; if(v==null) continue;
        const py=Ykey(s)(v);
        ctx.fillStyle=s.color; ctx.beginPath(); ctx.arc(px,py,4,0,Math.PI*2); ctx.fill();
        ctx.strokeStyle='#0e1116'; ctx.lineWidth=2; ctx.beginPath(); ctx.arc(px,py,4,0,Math.PI*2); ctx.stroke();
      }
      showTip(t[idx], data, idx);
    }
  }
}

function fmtW(x, signed){
  if(x==null) return '--';
  return (signed&&x>=0?'+':'')+x.toFixed(1)+' W';
}

function redraw(){ draw(cache.t, cache); }

async function tick(){
  try{
    const r=await fetch('/data.json'+buildQuery(),{cache:'no-store'});
    const d=await r.json();
    interval=Math.max(1000,(d.interval||5)*1000);
    $('iv').textContent=(d.interval||5)+' 秒';
    if(!d.battery_present){
      $('msg').textContent='未检测到电池(台式机或读取失败)。';
      $('state').textContent='无电池'; return;
    }
    const L=d.latest||{};
    $('v-total').textContent=fmtW(L.total,false);
    $('v-system').textContent=fmtW(L.system,false);
    $('v-charge').textContent=fmtW(L.charge,true);
    $('v-soc').textContent=L.soc!=null?L.soc.toFixed(1)+' %':'--';
    $('volt').textContent=L.v!=null?L.v.toFixed(2)+' V':'--';
    $('amp').textContent=L.a!=null?L.a.toFixed(2)+' A':'--';
    $('soc').textContent=L.soc!=null?Math.round(L.soc)+' %':'--';
    const np=(d.t||[]).length;
    $('cnt').textContent=(d.count!=null?d.count:np)+(np<(d.count||0)?('（抽样 '+np+'）'):'');
    const ad=d.adapter||{};
    $('adapter').textContent=ad.watts?(ad.watts+' W'+(ad.desc?(' · '+ad.desc):'')):'未连接';
    const st=$('state');
    if(L.charging){ st.textContent='充电中'; st.style.background='#1f6f33'; st.style.color='#d7ffe0'; }
    else if(L.external){ st.textContent=(L.soc!=null&&L.soc>=99)?'已充满':'已连接·未充电'; st.style.background='#3a3f46'; st.style.color='#e6edf3'; }
    else { st.textContent='使用电池'; st.style.background='#7a4318'; st.style.color='#ffe2c2'; }
    cache={ t:d.t||[], total:d.total||[], system:d.system||[], charge:d.charge||[], soc:d.soc||[] };
    draw(cache.t, cache);
    $('msg').textContent = np===0 ? '所选时间范围内暂无数据。' : '';
  }catch(e){ $('msg').textContent='读取失败：'+e; }
}

// 鼠标悬浮：在曲线上显示该时间点的数值
const chartEl=$('chart');
chartEl.addEventListener('mousemove', e=>{
  const rect=chartEl.getBoundingClientRect();
  hoverX=e.clientX-rect.left; hoverCX=e.clientX; hoverCY=e.clientY;
  if(!rafPending){ rafPending=true; requestAnimationFrame(()=>{ rafPending=false; redraw(); }); }
});
chartEl.addEventListener('mouseleave', ()=>{ hoverX=null; $('tip').style.display='none'; redraw(); });

// 时间范围交互
document.querySelectorAll('.controls button[data-days]').forEach(b=>{
  b.onclick=()=>{ sel={mode:'preset',days:+b.dataset.days}; $('custom-box').style.display='none'; setActive(); tick(); };
});
$('btn-custom').onclick=()=>{
  const box=$('custom-box');
  if(box.style.display==='none' || !box.style.display){
    if(!$('c-from').value){ const now=new Date(); $('c-to').value=toLocalInput(now); $('c-from').value=toLocalInput(new Date(now.getTime()-86400000)); }
    box.style.display='inline-flex';
  } else { box.style.display='none'; }
};
$('c-apply').onclick=()=>{
  const f=$('c-from').value, t=$('c-to').value;
  if(!f||!t){ $('msg').textContent='请填写完整的起止时间。'; return; }
  const from=new Date(f).getTime()/1000, to=new Date(t).getTime()/1000;
  if(isNaN(from)||isNaN(to)){ $('msg').textContent='时间格式无效。'; return; }
  sel={mode:'custom',from,to}; setActive(); tick();
};

setActive();
(function loop(){ tick().finally(()=>setTimeout(loop, interval)); })();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静默，避免占资源/刷日志

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" or path.startswith("/index"):
            self._send(200, DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/data.json":
            qs = parse_qs(parsed.query)
            body = json.dumps(build_payload(qs), default=str).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")


def main():
    global _running
    os.makedirs(DATA_DIR, exist_ok=True)
    preload_history()

    th = threading.Thread(target=sampler_loop, daemon=True)
    th.start()

    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print("无法绑定端口 %d: %s\n请用 PM_PORT 指定其它端口后重试。" % (PORT, e),
              file=sys.stderr, flush=True)
        sys.exit(1)

    def stop(*_a):
        global _running
        _running = False
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    print("power-monitor 已启动: http://%s:%d  (采样间隔 %ss, 数据 %s)"
          % (HOST, PORT, INTERVAL, CSV_PATH), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        _running = False
    print("power-monitor 已停止", flush=True)


if __name__ == "__main__":
    main()
