/* 聆心 · ECG Monitor 前端渲染引擎（无依赖、无构建、离线可用）
 * 数据通路：SSE /events（snapshot 帧 + alarm 事件） → 客户端样本环 → rAF 渲染。
 * 关键约束：波形按心电图纸网格绘制（1 小格 = 0.04 s / 0.1 mV），60 fps 靠时间外推而不依赖 20 Hz 数据帧率。 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const fmt = (v, n = 1) => (v === null || v === undefined || Number.isNaN(v)) ? '--' : Number(v).toFixed(n);

  const state = {
    fs: 200,
    windowSec: 6,
    mode: 'scroll',              // scroll | sweep
    sound: false,
    connected: false,
    firstFrame: true,
    t0: 0,
    // 视图参数（自动增益）
    centerCode: 32768,
    pxPerCode: 0.15,
    pxPerCodeTarget: 0.15,
    cursor: 0,
    sweepBase: 0,
    fps: 0,
    frames: 0,
    hrHist: [],                  // {t, hr, ok}
    beats: [],                   // 绝对样本号
    alarm: { level: 'info', state: '等待数据' },
    hrvWindow: '60s',
    last: null,                  // 最新快照
    events: 0,
    recorder: { recording: false },
    quick30: { active: false, progress: 0 },
    paused: false,               // 暂停画面（数据继续累积，便于读数）
    readout: null,               // {x, y, pinned} 悬停/钉住读数
    gainMode: '0.5',             // 'auto' | '0.5' | '1' | '2'（默认 0.5×）
    manualPx: 0,                 // 手动档冻结的 mm/码 标定（0 = 未启用）
    autoPx: 0,                   // 自动拟合的像素/码 基线（手动档倍率的唯一基准）
    manualUntil: 0,
    _gainTimer: 0,
    _lastClick: 0,
    basePx: 0,                   // 基线平移（像素，↑↓ 调整，双击复位）
    jumpAbs: null,               // 事件定位锚点（样本号）
    beatSound: false,            // 每拍提示音
    beatHist: [],                // 节拍时间轴：最近 60 s 的 R 峰 [{abs,t}]
    eventHist: [],               // 节拍时间轴：最近 60 s 的事件 [{abs,level,t}]
  };

  // ---------------------------------------------------------------- 样本环
  const ring = { cap: 0, data: null, abs: null, start: 0, end: 0 };

  // 描迹采样缓冲（复用，避免每帧分配；按 fs×窗口 自适应扩容，见 ensureScratch）
  const _scratch = { xs: null, ys: null, brk: null, n: 0 };

  function ensureScratch(need) {
    const size = Math.max(1024, Math.ceil(need * 1.1));
    if (_scratch.xs && _scratch.xs.length >= size) return;
    _scratch.xs = new Float32Array(size);
    _scratch.ys = new Float32Array(size);
    _scratch.brk = new Uint8Array(size);
  }

  const GRID = '152,155,161';      // 网格/标尺用的中性灰（近单色）
  const MV_PER_CODE = 1 / 65536 / 0.301 * 1000;   // 码值 → mV（A0 端），≈0.0507
  const PAUSE_BUDGET_S = 60;       // 暂停时样本环额外保留的时长（避免冻结画面被覆盖）
  let _motionQuery = null;
  let REDUCED = false;
  try {
    _motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
    REDUCED = !!_motionQuery.matches;
    if (_motionQuery.addEventListener) {
      _motionQuery.addEventListener('change', (e) => { REDUCED = !!e.matches; });
    }
  } catch (_) { /* 老浏览器忽略 */ }
  const WIN_CHOICES = [3, 6, 12];

  // ---- 视觉效果层：level 2=全效（辉光底+能量头+涟漪+采样点）/ 1=减效 / 0=仅主线
  // 按 2 s 滑窗帧率自动下调（<55→1，<45→0）、回稳 4 s 后逐级恢复；?fx=0|1|2 固定，系统"减少动效"强制 0。
  const RIPPLE_MS = 260;                 // R 峰涟漪寿命
  const fx = {
    level: 2,
    fixed: null,
    introP: 0,                           // 入场揭示进度 0→1（首帧数据到达后推进）
    trailSec: 0.35,                      // 能量头时长
    ripples: [],                         // {abs, y, t0}
    fade: null,                          // 模式/窗口切换的离屏交叉淡化 {canvas, until}
    demoted: false,                      // 是否已提示过降级
    _slowSince: 0,
    _goodSince: 0,
    _stepAt: 0,                          // 上次调档时刻（最小驻留用）
  };

  function fxTick(fps) {
    // 减少动效优先级最高：它必须能压过 ?fx= 固定档
    if (REDUCED) { fx.level = 0; fx._stepAt = 0; return; }
    if (fx.fixed !== null) { fx.level = fx.fixed; return; }
    const now = performance.now();
    if (now - fx._stepAt < 4000) return;        // 最小驻留 4 s：避免抖动来回跳档
    if (fps < 55) {                             // 单阈值 + 迟滞：55 以下逐级下调
      fx._slowSince = fx._slowSince || now;
      fx._goodSince = 0;
      const dur = now - fx._slowSince;
      if (dur > 1500) {
        const next = fx.level > 1 ? (fps < 45 ? 0 : 1) : 0;
        if (next !== fx.level) { fx.level = next; fx._stepAt = now; }
      }
    } else if (fps > 62) {                      // 回稳 4 s 后逐级恢复（每次只升一级）
      fx._slowSince = 0;
      fx._goodSince = fx._goodSince || now;
      if (now - fx._goodSince > 4000 && fx.level < 2) {
        fx.level += 1;
        fx._stepAt = now;
        fx._goodSince = now;
      }
    }
  }

  function fxPushRipple(abs, y) {
    if (fx.level < 2) return;
    fx.ripples.push({ abs, y, t0: performance.now() });
    while (fx.ripples.length > 6) fx.ripples.shift();
  }

  function ensureRing(fs) {
    // 24 s 基础窗 + 60 s 暂停预算：暂停时数据仍在入环，避免冻结画面被逐步覆盖
    const cap = Math.max(512, Math.ceil(fs * (24 + PAUSE_BUDGET_S)));
    if (ring.cap === cap) return;
    ring.cap = cap;
    ring.data = new Float32Array(cap);
    ring.abs = new Int32Array(cap).fill(-1);
    ring.start = 0;
    ring.end = 0;
    ensureScratch(cap);
  }

  function ringPush(wave, waveStart, step) {
    if (!wave || !wave.length) return;
    for (let i = 0; i < wave.length; i++) {
      const abs = waveStart + i * step;
      if (abs < ring.end) continue;                 // 旧数据（重叠区）
      const pos = abs % ring.cap;
      ring.data[pos] = wave[i];
      ring.abs[pos] = abs;
      ring.end = abs + 1;
      if (ring.end - ring.start > ring.cap) ring.start = ring.end - ring.cap;
    }
  }

  // ---------------------------------------------------------------- SSE
  let es = null;
  function setConn(stateName, text) {
    const pill = $('conn-pill');
    pill.dataset.state = stateName;
    $('conn-text').textContent = text;
  }

  function connect() {
    if (es) es.close();
    es = new EventSource('/events');
    es.addEventListener('hello', (e) => {
      try {
        const d = JSON.parse(e.data);
        const m = d.meta || {};
        if (m.fs) { state.fs = Number(m.fs); ensureRing(state.fs); }
        $('foot-meta').textContent = `v${m.version || '--'} · fs ${fmt(state.fs, 2)} S/s · 插件 ${(m.plugins || []).length} 个`;
      } catch (_) { /* ignore */ }
      resetStream();                                  // 每次(重)连都从干净状态开始
      setConn('live', '已连接');
    });
    es.addEventListener('snapshot', (e) => {
      let f;
      try { f = JSON.parse(e.data); } catch (_) { return; }
      onFrame(f);
    });
    es.addEventListener('alarm', (e) => {
      let ev;
      try { ev = JSON.parse(e.data); } catch (_) { return; }
      onAlarm(ev);
    });
    es.onopen = () => { state.connected = true; setConn('live', '已连接'); };
    es.onerror = () => { state.connected = false; setConn('retry', '重连中…'); };
  }

  function resetStream() {
    // 采集/服务重启或采样率变化：清空样本环与历史（否则新数据会被当"旧数据"丢掉）
    ring.start = 0;
    ring.end = 0;
    if (ring.abs) ring.abs.fill(-1);
    state.cursor = 0;
    state.beats = [];
    state.hrHist = [];
    state.events = 0;
    state.sweepBase = 0;
    state.lastFrameAt = 0;
    state.lastFrameEnd = 0;
    state.lastNTotal = undefined;
    state.readout = null;                     // 读数与钉住状态随流重置
    state.geom = null;
    state.lastBeatAbs = undefined;
    state.jumpAbs = null;                     // 事件定位锚点随流失效
    state.last = null;                        // 避免重连后仍显示上一段旧值
    state._lastClick = 0;
    state.beatHist.length = 0;                // 节拍/事件历史随流清空
    state.eventHist.length = 0;
    _hrAnim.cur = null;                       // 重连后允许心率重新"从 0 滚起"
    _srLast = 0;
    fx.ripples.length = 0;                    // 涟漪队列随流清空
    fx.fade = null;
    clearReadout();
    Object.keys(_lastText).forEach((k) => { delete _lastText[k]; });
    const ev = $('event-list'); if (ev) ev.innerHTML = '';
    const rl = $('rhythm-events'); if (rl) rl.innerHTML = '';
    const cnt = $('ev-count'); if (cnt) cnt.textContent = '0';
    const ti = $('trend-info'); if (ti) ti.textContent = '--';
  }

  function onFrame(f) {
    if (f.fs && Math.abs(f.fs - state.fs) > 1e-6) {
      state.fs = Number(f.fs);
      ensureRing(state.fs);
      resetStream();
    }
    if (state.lastNTotal !== undefined && Number(f.n_total) < state.lastNTotal) resetStream();
    state.lastNTotal = Number(f.n_total);
    state.waveStep = Math.max(1, f.wave_step || 1);
    ringPush(f.wave, f.wave_start || 0, state.waveStep);
    state.last = f;
    state.frames++;
    state.lastFrameAt = performance.now();
    state.lastFrameEnd = ring.end;
    if (state.firstFrame) {
      state.firstFrame = false;
      state.t0 = Date.now();
      $('wave-overlay').classList.add('hide');
      state.cursor = ring.end;
      state.sweepBase = ring.end;          // 扫描模式：首段从当前光标处开画（左缘起步）
      playIntro();                         // 有数据了 → 这时候播入场，用户才看得到
    }
    const b = (f.beats && f.beats.idx_abs) || [];
    if (b.length) {
      // 新检出的拍 → 触发涟漪（只给"新增"的拍，避免每帧重放旧拍）
      const prev = state.lastBeatAbs !== undefined ? state.lastBeatAbs : -1;
      let newest = prev;
      for (let i = 0; i < b.length && i < 4; i++) {
        const abs = Number(b[i]);
        if (abs > prev) {
          if (!state.paused) {                         // 暂停时不做涟漪/提示音
            const p = abs % ring.cap;
            const y = ring.abs[p] === abs ? ring.data[p] : null;
            if (y !== null) fxPushRipple(abs, y);
            pushBeat(abs);                             // 节拍时间轴取样
            beatClick();                               // 节拍提示音（默认关）
          }
          if (abs > newest) newest = abs;
        }
      }
      if (!state.paused && newest > prev) pulseHr();   // 心搏同步脉冲（每帧最多一次）
      state.lastBeatAbs = newest;
      state.beats = b;
    }
    if (f.analysis && f.analysis.alarm) state.alarm = f.analysis.alarm;
  }

  function onAlarm(ev) {
    state.events++;
    $('ev-count').textContent = String(state.events);
    const list = $('event-list');
    const li = document.createElement('li');
    li.dataset.level = ev.level || 'info';
    const time = new Date((ev.t || Date.now() / 1000) * 1000);
    const hh = String(time.getHours()).padStart(2, '0');
    const mm = String(time.getMinutes()).padStart(2, '0');
    const ss = String(time.getSeconds()).padStart(2, '0');
    li.innerHTML = `<span class="ev-dot"></span><span class="ev-t mono">${hh}:${mm}:${ss}</span><span>${escapeHtml(ev.text || '')}</span>`;
    li.dataset.abs = String(ring.end);                 // 供"点击事件 → 跳转定位"
    li.title = '点击定位到该时刻';
    list.prepend(li);
    pushEvent(ring.end, ev.level || 'info');           // 节拍时间轴上的事件标记
    while (list.children.length > 60) list.removeChild(list.lastChild);

    // device_suspect 故意不加进白名单：它只以灰点出现在右侧事件日志，
    // 不进节律卡头条（决策"不进头条但显示"，实施计划 §2.5）
    const rhythmCodes = ['tachy', 'brady', 'pause', 'missed', 'missed_suspect',
      'irregular', 'hr_extreme'];
    if (rhythmCodes.includes(ev.code)) {
      const rl = $('rhythm-events');
      const item = document.createElement('li');
      item.dataset.level = ev.level || 'info';       // 让 low（待观察）条目走弱化样式
      item.innerHTML = `<span class="t mono">${hh}:${mm}:${ss}</span><span>${escapeHtml(ev.text || '')}</span>`;
      rl.prepend(item);
      while (rl.children.length > 4) rl.removeChild(rl.lastChild);
    }
    beepFor(ev.level);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // ---------------------------------------------------------------- 画布
  function setupCanvas(canvas) {
    // 用 clientWidth/Height 代替 getBoundingClientRect：避免每帧强制同步布局
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = Math.max(1, Math.round((canvas.clientWidth || 1) * dpr));
    const h = Math.max(1, Math.round((canvas.clientHeight || 1) * dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    return { ctx: canvas.getContext('2d'), dpr, w, h };
  }

  const _viewBuf = { a: null };                        // 增益统计缓冲（复用）
  let _viewLast = -1e9;

  function computeView(plotH) {
    // 自动增益只需 ~8 Hz：避免每帧 O(n log n) 排序与分配
    const now = performance.now();
    if (now - _viewLast < 120) return;
    _viewLast = now;
    const wSamples = Math.round(state.fs * state.windowSec);
    // 暂停时以冻结光标为锚，画面不再随新数据漂移
    const anchor = state.paused ? Math.floor(state.cursor) : ring.end;
    const from = Math.max(ring.start, anchor - wSamples);
    if (!_viewBuf.a || _viewBuf.a.length < wSamples + 8) _viewBuf.a = new Float32Array(wSamples + 8);
    const buf = _viewBuf.a;
    let n = 0;
    for (let abs = from; abs < anchor; abs++) {
      const p = abs % ring.cap;
      if (ring.abs[p] === abs && n < buf.length) buf[n++] = ring.data[p];
    }
    if (n < 16) return;
    const vals = buf.subarray(0, n);
    vals.sort();
    const q = (t) => vals[clamp(Math.round(t * (n - 1)), 0, n - 1)];
    const lo = q(0.02), hi = q(0.98);
    const mid = 0.5 * (lo + hi);                       // 以 p2~p98 的**中点**为显示中心：
    const range = Math.max(220, hi - lo);              // 用心率中位数会把 R 峰顶裁掉（实测）
    state.centerCode += (mid - state.centerCode) * 0.35;
    const autoPx = (0.72 * plotH) / range;            // 自动拟合标定（手动档倍率以它为基准）
    state.autoPx = autoPx;
    // 手动档 = 真正锁定量程（冻结标定，不随信号漂移）；自动档 = 跟随时窗 p2~p98
    // 固定档但尚未冻结标定（典型：启动默认 0.5×）→ 以当前时窗自动标定 × 倍率锚定一次
    if (state.gainMode !== 'auto' && !(state.manualPx > 0)) {
      state.manualPx = autoPx * Number(state.gainMode);
      state.pxPerCode = state.manualPx;              // 直接落位，避免从占位初值漂移
    }
    const manual = state.gainMode !== 'auto' && state.manualPx > 0;
    state.pxPerCodeTarget = manual ? state.manualPx : autoPx;
    state.pxPerCode += (state.pxPerCodeTarget - state.pxPerCode) * 0.2;
  }

  function drawWave() {
    const canvas = $('wave-canvas');
    const { ctx, dpr, w, h } = setupCanvas(canvas);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = canvas.width / dpr, H = canvas.height / dpr;
    ctx.clearRect(0, 0, W, H);

    // 背景
    ctx.fillStyle = '#05070B';
    ctx.fillRect(0, 0, W, H);

    const plotW = W, plotH = H;
    const pxPerSec = plotW / state.windowSec;
    const box = pxPerSec * 0.04;                       // 1 小格 = 0.04 s
    const cy = plotH / 2;

    computeView(plotH);                                // 自动增益
    state.pxPerCode = clamp(state.pxPerCode, 1e-4, 10);

    // —— 标准心电纸网格（小格 1mm / 大格 5mm / 秒线）——
    // 入场时网格先以低透明度浮现（gp），随后与描迹一起进入稳态
    const gp = state.firstFrame ? 0.7 : (0.3 + 0.7 * fx.introP);
    ctx.lineWidth = 1;
    ctx.strokeStyle = `rgba(${GRID},${(0.05 * gp).toFixed(4)})`;
    ctx.beginPath();
    for (let x = 0; x <= plotW; x += box) { ctx.moveTo(x + .5, 0); ctx.lineTo(x + .5, plotH); }
    for (let y = 0; y <= plotH; y += box) { ctx.moveTo(0, y + .5); ctx.lineTo(plotW, y + .5); }
    ctx.stroke();

    ctx.strokeStyle = `rgba(${GRID},${(0.10 * gp).toFixed(4)})`;
    ctx.beginPath();
    for (let i = 0; i * 5 * box <= plotW; i++) { const x = i * 5 * box; ctx.moveTo(x + .5, 0); ctx.lineTo(x + .5, plotH); }
    for (let i = 0; i * 5 * box <= plotH; i++) { const y = i * 5 * box; ctx.moveTo(0, y + .5); ctx.lineTo(plotW, y + .5); }
    ctx.stroke();

    ctx.strokeStyle = `rgba(${GRID},${(0.16 * gp).toFixed(4)})`;
    ctx.beginPath();
    for (let t = 0; t <= state.windowSec; t += 1) { const x = t * pxPerSec; ctx.moveTo(x + .5, 0); ctx.lineTo(x + .5, plotH); }
    ctx.stroke();

    // —— 底边时间标尺（真实监护仪的"走纸速度"刻度感）——
    ctx.strokeStyle = `rgba(${GRID},${(0.32 * gp).toFixed(4)})`;
    ctx.beginPath();
    for (let t = 0; t <= state.windowSec; t += 1) {
      const x = Math.round(t * pxPerSec) + .5;
      const long = (t % 5 === 0);
      ctx.moveTo(x, plotH);
      ctx.lineTo(x, plotH - (long ? 9 : 4));
    }
    ctx.stroke();
    ctx.fillStyle = `rgba(${GRID},${(0.5 * gp).toFixed(4)})`;
    ctx.font = '9.5px ui-monospace, monospace';
    ctx.fillText('25 mm/s · 10 mm/mV', 6, plotH - 4);

    // —— 时间外推：光标按真实时间前进（数据 20 Hz，画面仍是 60 fps 平滑滚动）——
    if (state.paused) {
      // 暂停：冻结光标（数据仍在后台累积），画面静止便于读数
    } else if (state.lastFrameAt && ring.end > 0) {
      const dt = Math.min(0.25, Math.max(0, (performance.now() - state.lastFrameAt) / 1000));
      const target = state.lastFrameEnd + dt * state.fs;
      state.cursor = Math.min(target, ring.end + state.fs * 0.06);   // 最多领先 60 ms
      if (state.cursor < ring.end - state.fs * state.windowSec * 1.5) state.cursor = ring.end;
    } else {
      state.cursor = ring.end;
    }

    const wSamples = Math.max(1, Math.round(state.fs * state.windowSec));
    const level = state.alarm.level || 'info';

    // 暂停过久：冻结窗口即将被样本环覆盖 → 自动恢复（避免出现"空画面 + 已暂停"的假故障）
    if (state.paused && (Math.floor(state.cursor) - wSamples) < ring.start + state.fs * 0.5) {
      togglePause(false);
    }
    // 描迹配色统一由 traceRGB（下方）给出：主线/辉光/涟漪/渐变共用同一色源

    const yOf = (code) => clamp(cy - state.basePx - (code - state.centerCode) * state.pxPerCode, 1, plotH - 2);
    const xScroll = (abs) => ((abs - (state.cursor - wSamples)) / state.fs) * pxPerSec;
    const xSweep = (base, abs) => ((abs - base) / state.fs) * pxPerSec;

    // 扫描模式的"段"管理：一段 = 一个窗宽；书写头越过右缘即开新段（左缘从 0 重新开始）
    let headX = plotW;                    // 书写头横坐标（px）
    let prevBase = 0;                     // 上一段的起点样本号
    let fadePx = 0;                       // 过渡窗宽度（px）
    if (state.mode === 'sweep') {
      if (!state.sweepBase || state.cursor < state.sweepBase) state.sweepBase = Math.floor(state.cursor);
      while (state.cursor - state.sweepBase >= wSamples) state.sweepBase += wSamples;
      headX = clamp(((state.cursor - state.sweepBase) / state.fs) * pxPerSec, 0, plotW);
      prevBase = state.sweepBase - wSamples;
      // 过渡窗：短到不会读成"两条线"，长到足以抹平硬边；减少动效偏好下退化为硬边界（真实监护仪语义）
      fadePx = REDUCED ? 0 : clamp(pxPerSec * 0.085, 10, 22);
    }
    const TAIL_MIN = 0.35;                // 最新数据的最低不透明度（不能把"刚写的"淡到看不见）

    // —— 描迹引擎：一次采样收集 + 按区间用"单条渐变色"描迹（无阶梯、无裂缝）——
    const scr = _scratch;
    const traceRGB = level === 'high' ? '255,107,122'
      : (level === 'medium' ? '255,196,107' : '61,220,151');
    const solidStyle = `rgba(${traceRGB},1)`;
    const prevStyle = `rgba(${traceRGB},0.8)`;
    const collect = (absFrom, absTo, xf) => {
      scr.n = 0;
      let lastAbs = -1e9;
      const a0 = Math.floor(Math.max(ring.start, absFrom));   // ★ 取整：光标是浮点
      const a1 = Math.ceil(absTo);
      for (let abs = a0; abs < a1; abs++) {
        const p = abs % ring.cap;
        if (ring.abs[p] !== abs) continue;
        const x = xf(abs);
        if (x < -4 || x > plotW + 4) { lastAbs = abs; continue; }
        if (scr.n < scr.xs.length) {
          scr.xs[scr.n] = x;
          scr.ys[scr.n] = yOf(ring.data[p]);
          scr.brk[scr.n] = (scr.n === 0 || (abs - lastAbs) > state.fs * 0.5) ? 1 : 0;
          scr.n++;
        }
        lastAbs = abs;
      }
      return scr.n;
    };
    const idxAt = (x) => {                 // 已采集点中第一个 x ≥ 阈值的下标（xs 单调 → 二分）
      let lo = 0, hi = scr.n;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (scr.xs[mid] < x) lo = mid + 1; else hi = mid;
      }
      return lo;
    };
    const strokeRange = (i0, i1, style) => {
      if (!style || i1 <= i0 || i0 < 0) return;
      ctx.strokeStyle = style;
      ctx.beginPath();
      let started = false;
      for (let k = i0; k < i1; k++) {
        if (scr.brk[k]) started = false;
        if (!started) { ctx.moveTo(scr.xs[k], scr.ys[k]); started = true; }
        else ctx.lineTo(scr.xs[k], scr.ys[k]);
      }
      ctx.stroke();
    };
    const gradStyle = (x0, x1, a0, a1) => {   // 沿 x 的 alpha 渐变（一次描迹即可平滑过渡）
      const g = ctx.createLinearGradient(x0, 0, Math.max(x1, x0 + 0.5), 0);
      g.addColorStop(0, `rgba(${traceRGB},${a0})`);
      g.addColorStop(1, `rgba(${traceRGB},${a1})`);
      return g;
    };

    // —— 波形描迹：三层（辉光底 → 主线·新鲜度渐变 → 能量头）——
    const fxFull = fx.level >= 2;                      // 全效：辉光/能量头/涟漪/采样点
    const fxMid = fx.level >= 1;                       // 减效：保留新鲜度渐变，去掉辉光
    // 两条 alpha 斜坡（与渐变描迹共用同一组端点，保证接缝不可见）
    const tailA = (x) => (x <= headX - fadePx ? 1
      : (x >= headX ? TAIL_MIN
        : 1 - (1 - TAIL_MIN) * (x - (headX - fadePx)) / Math.max(fadePx, 0.5)));
    const prevA = (x) => (x <= headX ? 0
      : (x >= headX + fadePx ? 0.8 : 0.8 * (x - headX) / Math.max(fadePx, 0.5)));
    const trailPx = Math.max(6, pxPerSec * fx.trailSec);
    // 报警时辉光更强 → 屏幕"活"起来，视觉上传达紧迫感（心电图相关动效）
    const glowStyle = `rgba(${traceRGB},${level === 'info' ? 0.13 : 0.24})`;
    // 采样点质感：短时间窗（≤3.5 s）下把"数字采样"如实画出来（点数过多则跳过）
    const drawDots = (i0, i1, style) => {
      if (!fxFull || state.windowSec > 3.5) return;
      const n = i1 - i0;
      if (n < 2 || n > 2400) return;
      // 判据用"点间距"而不是点数：太密时视觉上只会糊成一团，直接跳过
      const spacing = Math.abs(scr.xs[i1 - 1] - scr.xs[i0]) / (n - 1);
      if (spacing < 5) return;
      const r = spacing > 12 ? 1.4 : 1.1;
      ctx.fillStyle = style;
      ctx.beginPath();                                 // 单条 path 一次 fill，减少状态切换
      for (let k = i0; k < i1; k++) {
        ctx.moveTo(scr.xs[k] + r, scr.ys[k]);
        ctx.arc(scr.xs[k], scr.ys[k], r, 0, Math.PI * 2);
      }
      ctx.fill();
    };

    // 入场揭示裁剪：描迹/标记/涟漪共用同一裁剪，避免"先出现标记、后长出波形"
    const introClip = () => {
      if (fx.introP >= 1 || REDUCED) return false;
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, plotW * fx.introP, plotH);
      ctx.clip();
      return true;
    };
    ctx.save();
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    const clipTrace = introClip();
    if (state.mode === 'scroll') {
      collect(Math.floor(state.cursor) - wSamples - 8, ring.end, xScroll);
      if (fxFull) {                                    // ① 辉光底（一次描迹）
        ctx.lineWidth = 4.4;
        strokeRange(0, scr.n, glowStyle);
      }
      ctx.lineWidth = 1.6;                             // ② 主线：旧→新的 alpha 渐变（尾迹感）
      strokeRange(0, scr.n, fxMid ? gradStyle(0, plotW, 0.58, 1) : solidStyle);
      drawDots(0, scr.n, `rgba(${traceRGB},0.5)`);
      if (fxFull && scr.n > 2) {                       // ③ 能量头：最新一段叠加提亮
        // 样本数要按抽稀步长折算，否则 wave_step>1 时能量头会拉长 step 倍
        const i0 = Math.max(0, scr.n - Math.round(state.fs * fx.trailSec / (state.waveStep || 1)));
        ctx.save();
        ctx.globalCompositeOperation = 'lighter';
        ctx.lineWidth = 2.4;
        strokeRange(i0, scr.n, gradStyle(plotW - trailPx, plotW, 0.05, 0.5));
        ctx.restore();
      }
    } else {
      // 当前段：画到过渡窗左缘，尾部用一条渐变描迹平滑压低（不消失，保持可读）
      collect(state.sweepBase, Math.min(ring.end, Math.floor(state.cursor) + 1),
              (abs) => xSweep(state.sweepBase, abs));
      const cutNow = idxAt(headX - fadePx);
      const cutI = Math.min(scr.n, cutNow + 1);
      if (fxFull) {
        ctx.lineWidth = 4.4;
        strokeRange(0, cutI, glowStyle);
      }
      ctx.lineWidth = 1.6;
      strokeRange(0, cutI, fxMid ? gradStyle(0, Math.max(headX, 1), 0.62, 1) : solidStyle);
      drawDots(0, cutI, `rgba(${traceRGB},0.5)`);
      if (fxFull) {                                    // 能量头：过渡窗之前的最新一段
        const xEnd = Math.max(0, headX - fadePx);
        const i0 = idxAt(Math.max(0, xEnd - trailPx));
        if (cutI > i0) {
          ctx.save();
          ctx.globalCompositeOperation = 'lighter';
          ctx.lineWidth = 2.4;
          strokeRange(i0, cutI, gradStyle(xEnd - trailPx, xEnd, 0.05, 0.45));
          ctx.restore();
        }
      }
      if (fadePx > 1 && cutNow < scr.n) {
        strokeRange(Math.max(0, cutNow - 1), scr.n,
                    gradStyle(headX - fadePx, headX, tailA(headX - fadePx), tailA(headX)));
      }
      // 上一段：书写头之后「涌现」并维持 0.8（边写边擦的真实语义；换窗/重连时按可用范围裁剪）
      if (prevBase < ring.end) {
        collect(Math.max(ring.start, prevBase), state.sweepBase,
                (abs) => xSweep(prevBase, abs));
        const c2 = idxAt(headX);
        const c3 = idxAt(headX + Math.max(fadePx, 0.5));
        if (fxFull) {
          ctx.lineWidth = 4.4;
          strokeRange(c2, scr.n, `rgba(${traceRGB},0.07)`);
        }
        ctx.lineWidth = 1.6;
        if (fadePx > 1) {
          strokeRange(c2, Math.min(scr.n, c3 + 1),
                      gradStyle(headX, headX + fadePx, Math.max(prevA(headX), 0.04), prevA(headX + fadePx)));
          strokeRange(Math.max(0, c3 - 1), scr.n, prevStyle);
        } else {
          strokeRange(c2, scr.n, prevStyle);      // 减少动效：硬边界（真实监护仪擦除棒）
        }
        drawDots(c2, scr.n, `rgba(${traceRGB},0.26)`);
      }
    }
    ctx.restore();
    if (clipTrace) ctx.restore();                      // 结束入场裁剪

    // —— R 峰涟漪（每拍扩散一环，指数衰减收敛；并发上限 6）——
    const xOfBeat = (abs) => {
      if (state.mode === 'scroll') return abs >= Math.floor(state.cursor) - wSamples ? xScroll(abs) : null;
      if (abs >= state.sweepBase) {
        const x = xSweep(state.sweepBase, abs);
        return x <= headX + 1 ? x : null;
      }
      if (abs >= prevBase) return xSweep(prevBase, abs);
      return null;
    };
    const clipR = introClip();
    if (fxFull && fx.ripples.length) {
      const nowMs = performance.now();
      for (let i = fx.ripples.length - 1; i >= 0; i--) {
        const rp = fx.ripples[i];
        const age = (nowMs - rp.t0) / RIPPLE_MS;
        if (age >= 1) { fx.ripples.splice(i, 1); continue; }
        const x = xOfBeat(rp.abs);
        if (x === null) continue;
        const p = rp.abs % ring.cap;
        const code = ring.abs[p] === rp.abs ? ring.data[p] : rp.y;
        const rippleA = (level === 'info' ? 0.4 : 0.62) * (1 - age);
        ctx.strokeStyle = `rgba(${traceRGB},${rippleA.toFixed(3)})`;
        ctx.lineWidth = 1.3 * (1 - age) + 0.4;
        ctx.beginPath();
        ctx.arc(x, yOf(code), 3 + age * 12, 0, Math.PI * 2);
        ctx.stroke();
      }
    }
    if (clipR) ctx.restore();


    // —— R 峰标记（▼ 落在峰顶上方；上一段的标记压低透明度）——
    const beats = (fx.introP >= 1 || REDUCED) ? (state.beats || []) : [];  // 与描迹同步出现
    for (let i = 0; i < beats.length; i++) {
      const abs = beats[i];
      if (abs < ring.start || abs >= ring.end) continue;
      const p = abs % ring.cap;
      if (ring.abs[p] !== abs) continue;
      let x, alpha = 0.95;
      if (state.mode === 'scroll') {
        if (abs < state.cursor - wSamples) continue;
        x = xScroll(abs);
      } else if (abs >= state.sweepBase) {
        x = xSweep(state.sweepBase, abs);
        if (x > headX) continue;                       // 未写入区域不画
        alpha = 0.95 * tailA(x);                       // 跟着尾部一起淡出
        if (alpha <= 0.12) continue;
      } else if (abs >= prevBase) {
        x = xSweep(prevBase, abs);
        const f = prevA(x) / 0.8;                      // 0~1 的涌现系数
        if (f <= 0.1) continue;
        alpha = 0.42 * f;                              // 上一段整体压低
      } else {
        continue;
      }
      const y = yOf(ring.data[p]);
      // 新检出的拍：标记做一次弹入（180 ms，锚在峰顶）
      let pop = 1;
      if (fx.level >= 2) {
        const nowMs = performance.now();
        for (let r = 0; r < fx.ripples.length; r++) {
          if (fx.ripples[r].abs === abs) {
            const age = (nowMs - fx.ripples[r].t0) / 180;
            if (age < 1) pop = 1 + 0.22 * (1 - age);
            break;
          }
        }
      }
      const old = state.mode === 'sweep' && abs < state.sweepBase;   // 上一段的标记偏灰
      ctx.fillStyle = old ? `rgba(140,168,158,${alpha.toFixed(3)})`
                          : `rgba(61,220,151,${alpha.toFixed(3)})`;
      ctx.beginPath();
      ctx.moveTo(x - 4 * pop, y - 5 - 7 * pop);
      ctx.lineTo(x + 4 * pop, y - 5 - 7 * pop);
      ctx.lineTo(x, y - 5);
      ctx.closePath();
      ctx.fill();
    }

    // —— 扫描模式：柔光书写头（窄渐变光带 + 1px 核心线；报警时轻微染色）——
    if (state.mode === 'sweep') {
      const bandW = clamp(Math.max(fadePx * 2, 18), 18, 52);
      const tint = level === 'high' ? '255,107,122'
        : (level === 'medium' ? '255,196,107' : '220,227,236');
      if (!REDUCED && fx.level >= 1) {                 // 减少动效/降级档：不画滚动光带，只留位置线
        const g = ctx.createLinearGradient(headX - bandW / 2, 0, headX + bandW / 2, 0);
        g.addColorStop(0, `rgba(${tint},0)`);
        g.addColorStop(0.5, `rgba(${tint},.07)`);
        g.addColorStop(1, `rgba(${tint},0)`);
        ctx.fillStyle = g;
        ctx.fillRect(headX - bandW / 2, 0, bandW, plotH);
      }
      ctx.strokeStyle = `rgba(${tint},.20)`;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(headX) + .5, 0);
      ctx.lineTo(Math.round(headX) + .5, plotH);
      ctx.stroke();
    }

    // —— 悬停/钉住读数：虚线导引 + 采样点（永远画在最上层；钉住按样本号跟随）——
    if (state.readout) {
      const s = readoutSample();
      let rx = clamp(state.readout.x, 0, plotW);
      if (state.readout.pinned && s) {
        rx = state.mode === 'scroll' ? xScroll(s.abs)
          : xSweep(s.abs >= state.sweepBase ? state.sweepBase : prevBase, s.abs);
      }
      rx = clamp(rx, 0, plotW);
      ctx.save();
      ctx.strokeStyle = 'rgba(237,239,242,.16)';
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(Math.round(rx) + .5, 0);
      ctx.lineTo(Math.round(rx) + .5, plotH);
      ctx.stroke();
      ctx.setLineDash([]);
      if (s && s.code !== null) {
        ctx.fillStyle = 'rgba(237,239,242,.9)';
        ctx.beginPath();
        ctx.arc(rx, yOf(s.code), 2.4, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
    }
    // 模式/窗口切换：把切换前的整帧淡出叠加在当前画面之上（220 ms 交叉淡化）
    if (fx.fade) {
      const left = fx.fade.until - performance.now();
      // 尺寸不一致（resize/DPR 变化）直接丢弃，避免拉伸旧帧
      if (left > 0 && fx.fade.w === canvas.width && fx.fade.h === canvas.height) {
        ctx.save();
        ctx.globalAlpha = Math.max(0, Math.min(1, left / 220));
        ctx.drawImage(fx.fade.canvas, 0, 0, plotW, plotH);
        ctx.restore();
      } else {
        fx.fade = null;
      }
    }
    // 供交互层复用本帧几何（悬停读数 / 快捷键）
    state.geom = { headX, prevBase, pxPerSec, plotW, plotH, wSamples };

    // —— 事件定位锚点（点击事件条目后出现；恢复播放即消失）——
    if (state.jumpAbs !== null && state.jumpAbs !== undefined) {
      const jx = xOfBeat(state.jumpAbs);
      if (jx !== null) {
        ctx.save();
        ctx.strokeStyle = 'rgba(245,165,36,.55)';
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(Math.round(jx) + .5, 0);
        ctx.lineTo(Math.round(jx) + .5, plotH);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = 'rgba(245,165,36,.9)';
        ctx.font = '10px ui-monospace, monospace';
        ctx.fillText('事件定位', Math.min(jx + 5, plotW - 48), 12);
        ctx.restore();
      }
    }

    // —— 量程角标（手动增益时明确标注，避免与自动值混淆）——
    const pxPerMv = state.pxPerCode * (1 / (1 / 65536 / 0.301) / 1000); // code→mV→px
    const fullScaleMv = (plotH / 2) / pxPerMv;
    const gb = $('gain-badge');
    if (gb) {
      const manual = state.gainMode !== 'auto' && state.manualPx > 0;
      const txt = manual
        ? `量程 ±${fullScaleMv.toFixed(0)} mV · 手动 ${state.gainMode}×`
        : `量程 ±${fullScaleMv.toFixed(0)} mV`;
      if (gb.textContent !== txt) gb.textContent = txt;   // 只在变化时写（省掉每帧模板串分配）
    }
  }

  // ---------------------------------------------------------------- 节拍时间轴（最近 60 s）
  const STRIP_SEC = 60;
  function pushBeat(abs) {
    const t = Date.now();
    state.beatHist.push({ abs, t });
    const cut = t - STRIP_SEC * 1000 - 2000;
    while (state.beatHist.length > 900 || (state.beatHist.length && state.beatHist[0].t < cut)) {
      state.beatHist.shift();
    }
  }
  function pushEvent(abs, level) {
    const t = Date.now();
    state.eventHist.push({ abs, level, t });
    const cut = t - STRIP_SEC * 1000 - 2000;
    while (state.eventHist.length > 200 || (state.eventHist.length && state.eventHist[0].t < cut)) {
      state.eventHist.shift();
    }
  }
  function pulseHr() {
    if (REDUCED) return;
    const card = $('hr-card');
    const num = card && card.querySelector('.hr-num');
    if (!num) return;
    num.classList.remove('pulse');
    void num.offsetWidth;                              // 重启动画
    num.classList.add('pulse');
    if (card) {                                        // 左侧色条同步一亮（心搏节奏的可视化）
      card.classList.remove('pulse');
      void card.offsetWidth;
      card.classList.add('pulse');
      setTimeout(() => card.classList.remove('pulse'), 240);
    }
  }

  let _stripLast = 0;
  function drawStrip() {
    const now = performance.now();
    if (now - _stripLast < (REDUCED ? 600 : 120)) return;
    _stripLast = now;
    const canvas = $('strip-canvas');
    if (!canvas) return;
    const { ctx, dpr } = setupCanvas(canvas);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = canvas.width / dpr, H = canvas.height / dpr;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#07080A';
    ctx.fillRect(0, 0, W, H);

    const t1 = Date.now();
    const span = STRIP_SEC * 1000;
    const xOfT = (t) => ((t - (t1 - span)) / span) * W;
    const baseY = H - 10;

    // 每 10 s 一条刻度
    ctx.strokeStyle = `rgba(${GRID},0.10)`;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let s = 0; s <= STRIP_SEC; s += 10) {
      const x = Math.round(xOfT(t1 - span + s * 1000)) + .5;
      ctx.moveTo(x, 6);
      ctx.lineTo(x, baseY);
    }
    ctx.stroke();
    ctx.fillStyle = `rgba(${GRID},0.45)`;
    ctx.font = '9px ui-monospace, monospace';
    ctx.fillText('-60s', 4, H - 2);
    ctx.fillText('now', W - 26, H - 2);

    // R 峰：竖线（新出现的 400 ms 内做"生长 + 亮点"动画）
    const beats = state.beatHist;
    for (let i = 0; i < beats.length; i++) {
      const b = beats[i];
      const x = xOfT(b.t);
      if (x < -2 || x > W + 2) continue;
      const age = (t1 - b.t) / 400;
      const grow = age < 1 ? 0.35 + 0.65 * age : 1;
      const h = (H - 22) * grow;
      ctx.strokeStyle = `rgba(61,220,151,${age < 1 ? 1 : 0.82})`;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + .5, baseY);
      ctx.lineTo(Math.round(x) + .5, baseY - h);
      ctx.stroke();
      if (age < 1) {
        ctx.fillStyle = `rgba(120,255,190,${(1 - age).toFixed(2)})`;
        ctx.beginPath();
        ctx.arc(x, baseY - h, 2.2, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    // 事件标记：形状 + 颜色双重编码（与事件流一致：方=高 / 三角=中 / 空心圆=低）
    const evs = state.eventHist;
    for (let i = 0; i < evs.length; i++) {
      const e = evs[i];
      const x = xOfT(e.t);
      if (x < -4 || x > W + 4) continue;
      const c = e.level === 'high' ? '242,85,90' : (e.level === 'medium' ? '245,165,36' : '90,169,230');
      ctx.strokeStyle = `rgba(${c},.9)`;
      ctx.fillStyle = `rgba(${c},.9)`;
      ctx.lineWidth = 1.2;
      if (e.level === 'high') {
        ctx.fillRect(Math.round(x) - 3, 6, 6, 6);
      } else if (e.level === 'medium') {
        ctx.beginPath();
        ctx.moveTo(x, 5);
        ctx.lineTo(x + 4, 12);
        ctx.lineTo(x - 4, 12);
        ctx.closePath();
        ctx.fill();
      } else {
        ctx.beginPath();
        ctx.arc(x, 9, 3, 0, Math.PI * 2);
        ctx.stroke();
      }
    }

    const info = $('strip-info');
    if (info) {
      const txt = `最近 ${STRIP_SEC} s · R 峰 ${beats.length}${evs.length ? ` · 事件 ${evs.length}` : ''}`;
      if (info.textContent !== txt) info.textContent = txt;
    }
  }

  let _trendLast = 0;
  function drawTrend() {
    // 数据源 ~5 Hz，趋势无需 60 fps 重绘
    const tnow = performance.now();
    if (tnow - _trendLast < (REDUCED ? 1000 : 150)) return;
    _trendLast = tnow;
    const canvas = $('trend-canvas');
    const { ctx, dpr } = setupCanvas(canvas);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = canvas.width / dpr, H = canvas.height / dpr;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#070B11';
    ctx.fillRect(0, 0, W, H);

    const pad = { l: 32, r: 8, t: 8, b: 10 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;

    const now = Date.now();
    const hist = state.hrHist.filter((pt) => now - pt.t <= 60000);
    state.hrHist = hist;
    if (hist.length < 2) return;

    // 纵轴自适应（至少 40 bpm 带宽，10 bpm 取整）：避免心率偏低时整块空白
    const vals = hist.map((p) => p.hr);
    const lo0 = Math.min(...vals), hi0 = Math.max(...vals);
    const span = Math.max(40, Math.round(((hi0 - lo0) * 1.7 + 12) / 10) * 10);
    const yMin = Math.max(10, Math.round((0.5 * (lo0 + hi0) - span / 2) / 10) * 10);
    const yMax = yMin + span;
    const yOf = (v) => pad.t + ih - (clamp(v, yMin, yMax) - yMin) / (yMax - yMin) * ih;

    // 限值带（50–120 bpm，落在视野内的部分）
    const bLo = Math.max(yMin, 50), bHi = Math.min(yMax, 120);
    if (bHi > bLo) {
      ctx.fillStyle = `rgba(${GRID},0.06)`;
      ctx.fillRect(pad.l, yOf(bHi), iw, yOf(bLo) - yOf(bHi));
    }

    // 网格刻度（25 bpm 一档）
    ctx.strokeStyle = `rgba(${GRID},0.10)`;
    ctx.lineWidth = 1;
    ctx.beginPath();
    const ticks = [];
    for (let v = Math.ceil(yMin / 25) * 25; v <= yMax; v += 25) ticks.push(v);
    ticks.forEach((v) => { const y = Math.round(yOf(v)) + .5; ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); });
    ctx.stroke();

    ctx.fillStyle = `rgba(${GRID},0.5)`;
    ctx.font = '9.5px ui-monospace, monospace';
    ticks.forEach((v) => ctx.fillText(String(v), 4, yOf(v) + 3));

    const t0 = now - 60000;
    const xOf = (t) => pad.l + ((t - t0) / 60000) * iw;

    // 折线（扁平：细线 + 端点，不做渐变面积）
    ctx.beginPath();
    ctx.strokeStyle = '#3DDC97';
    ctx.lineWidth = 1.4;
    ctx.lineJoin = 'round';
    for (let i = 0; i < hist.length; i++) {
      const x = xOf(hist[i].t), y = yOf(hist[i].hr);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // 最新点
    const lastPt = hist[hist.length - 1];
    ctx.beginPath();
    ctx.fillStyle = lastPt.ok ? '#3DDC97' : `rgba(${GRID},0.75)`;
    ctx.arc(xOf(lastPt.t), yOf(lastPt.hr), 2.5, 0, Math.PI * 2);
    ctx.fill();
  }

  // ---------------------------------------------------------------- DOM 刷新（5 Hz）
  // 节律码的中文短名（只用于"观察中 n/need 窗"与节律卡提示，与后端 label 无关）
  const RHYTHM_LABEL = {
    tachy: '心动过速', brady: '心动过缓', pause: '停搏/长间歇', missed: '疑似漏搏',
    missed_suspect: '疑似漏搏（待观察）', irregular: '节律不齐', device_suspect: '设备侧疑点',
  };

  function updateDom() {
    const f = state.last;
    $('v-fps').textContent = String(Math.round(state.fps));
    const up = state.t0 ? Math.floor((Date.now() - state.t0) / 1000) : 0;
    $('v-uptime').textContent = `${String(Math.floor(up / 60)).padStart(2, '0')}:${String(up % 60).padStart(2, '0')}`;
    if (!f) return;

    $('v-fs').textContent = fmt(f.fs, 2);
    $('v-drop').textContent = String(f.dropped);
    $('wave-foot-info').textContent =
      `窗口 ${state.windowSec.toFixed(1)} s · 累计 ${f.n_total} 点 · 硬件计数 ${f.n_hw}`;

    // —— 心率 ——
    const hr = f.hr;
    setHrText(hr);                                     // 首次出值滚动一次，之后平滑过渡
    const q = $('hr-quality');
    q.textContent = f.hr_ok ? '信号可信' : '暂不可信';
    q.dataset.level = f.hr_ok ? 'ok' : '';
    const alarm = (f.analysis && f.analysis.alarm) || state.alarm;
    state.alarm = alarm;
    const lvl = alarm.level || 'info';
    $('hr-card').dataset.level = lvl;
    $('wave-card').dataset.level = lvl;
    const flag = $('alarm-flag');
    if (lvl === 'high' || lvl === 'medium') {          // 角标只给需要立刻行动的两级
      flag.hidden = false;
      flag.dataset.level = lvl;
      const icon = $('alarm-icon');
      if (icon) icon.textContent = lvl === 'high' ? '▲' : '◆';     // 形状编码
      // 该区域是 assertive：只在文本真正变化时写入，否则 5 Hz 重写会持续打断读屏
      const hrShown = (hr === null || hr === undefined) ? '--' : String(Math.round(hr));
      const txt = `${lvl === 'high' ? '高优先级' : '中优先级'}报警：${alarm.state || '报警'} · ${hrShown} bpm`;
      const el = $('alarm-text');
      if (el && el.textContent !== txt) el.textContent = txt;
    } else {
      flag.hidden = true;
    }

    // 心率刻尺（30–210 域）
    const lo = (alarm.limits && alarm.limits.lo) || 50;
    const hi = (alarm.limits && alarm.limits.hi) || 120;
    const span = 210 - 30;
    $('hr-zone-lo').style.width = `${((lo - 30) / span) * 100}%`;
    $('hr-zone-lo').style.left = '0';
    $('hr-zone-hi').style.left = `${((hi - 30) / span) * 100}%`;
    $('hr-zone-hi').style.width = `${((210 - hi) / span) * 100}%`;
    const ptr = $('hr-ptr');
    if (hr !== null && hr !== undefined) {
      ptr.style.opacity = '1';
      ptr.style.left = `calc(${clamp(((hr - 30) / span) * 100, 0, 100)}% - 1.5px)`;
      ptr.style.background = lvl === 'high' ? '#FF4D5E' : (lvl === 'medium' ? '#FFB020' : '#37E19A');
    } else {
      ptr.style.opacity = '0';
    }
    $('hr-lo-tag').textContent = String(lo);
    $('hr-hi-tag').textContent = String(hi);

    const beats = f.beats || {};
    const rrFromItv = (f.analysis && f.analysis.intervals && f.analysis.intervals.rr_ms) || null;
    $('v-rr').textContent = rrFromItv ? `RR ${Math.round(rrFromItv)} ms` : 'RR -- ms';
    $('v-nbeats').textContent = `拍 ${beats.n_beats ?? '--'}`;
    const bb = $('beats-badge');
    if (bb) bb.textContent = `R 峰 ${beats.n_beats ?? '--'}`;
    const bq = (f.analysis && f.analysis.quality) || null;
    $('v-q').textContent = `质量 ${bq && bq.sqi !== undefined ? bq.sqi.toFixed(2) : '--'}`;

    // —— 趋势（暂停时不再采样，保持"冻结一致"）——
    if (!state.paused && hr !== null && hr !== undefined) {
      state.hrHist.push({ t: Date.now(), hr: Number(hr), ok: !!f.hr_ok });
      $('trend-info').textContent = `${Math.round(hr)} bpm`;
    }

    // —— 节律 ——
    const arr = (f.analysis && f.analysis.arrhythmia) || null;
    const arrTrust = (arr && arr.trust) || null;
    const arrSuspect = !!(arrTrust && arrTrust.ok === false);   // A 门在报"设备侧疑点"
    $('rhythm-status').textContent = arr ? (arr.status || '--') : '未启用 arrhythmia 插件';
    const rl = $('rhythm-level');
    // chip 文案：info 时常规显示"常规"；A 门生效时显示"信号不足"，卡片整体弱化
    rl.textContent = arr
      ? (arr.level === 'info' ? (arrSuspect ? '信号不足' : '常规') : arr.level.toUpperCase())
      : '--';
    rl.dataset.level = arr ? (arr.level === 'info' ? (arrSuspect ? 'info' : '') : arr.level) : '';
    $('rhythm-card').dataset.level = arr
      ? (arrSuspect ? 'info' : (arr.level || 'info')) : 'info';
    // 观察进度（pending）：减速期把"2/3 窗"直接写在卡片上（只增字段，不覆盖 status）
    let rHint = $('rhythm-card').querySelector('.hint');
    if (!rHint) {
      rHint = document.createElement('div');
      rHint.className = 'hint';
      $('rhythm-card').appendChild(rHint);
    }
    const pend = (arr && arr.pending) || null;
    const pendKeys = pend ? Object.keys(pend) : [];
    if (arr && pendKeys.length) {
      rHint.textContent = '观察中：' + pendKeys.map((k) => {
        const it = pend[k] || {};
        return `${RHYTHM_LABEL[k] || k} ${it.n_win || 0}/${it.need || '?'} 窗`;
      }).join(' · ');
    } else if (arrSuspect) {
      rHint.textContent = `设备侧疑点：${(arrTrust.why || []).join(' · ') || '证据不足'}`;
    } else {
      rHint.textContent = '';
    }

    // —— HRV ——
    const hrv = (f.analysis && f.analysis.hrv) || null;
    const win = hrv && hrv.windows ? hrv.windows[state.hrvWindow] : null;
    const setMetric = (id, val, digits = 1) => {
      const el = $(id);
      if (!el) return;
      el.textContent = (val === null || val === undefined) ? '--' : fmt(val, digits);
      el.closest('.metric').classList.toggle('is-na', val === null || val === undefined);
    };
    if (win && win.ready) {
      setMetric('m-sdnn', win.sdnn_ms);
      setMetric('m-rmssd', win.rmssd_ms);
      setMetric('m-pnn50', win.pnn50 === null || win.pnn50 === undefined ? null : win.pnn50 * 100, 1);
      setMetric('m-meanhr', win.mean_hr);
      setMetric('m-lfhf', win.freq_ready ? win.lf_hf : null, 3);
      setMetric('m-total', win.freq_ready ? win.total_ms2 : null, 0);
      const note = $('hrv-note');
      if (win.freq_ready) {
        note.className = 'note is-ok';
        note.textContent = `窗口 ${state.hrvWindow}：${win.n_rr} 个 RR / ${win.span_s} s` +
          (win.n_corrected ? ` · 漏搏修正 ${win.n_corrected} 处` : '') + ' · LF 0.04–0.15 Hz / HF 0.15–0.40 Hz';
      } else {
        note.className = 'note is-warn';
        note.textContent = `窗口 ${state.hrvWindow}：${win.freq_reason || '频域不可用'}（时域已给出）`;
      }
    } else {
      ['m-sdnn', 'm-rmssd', 'm-pnn50', 'm-meanhr', 'm-lfhf', 'm-total'].forEach((id) => setMetric(id, null));
      const note = $('hrv-note');
      note.className = 'note';
      note.textContent = hrv ? (win && win.reason ? win.reason : '数据积累中…') : '未启用 hrv 插件';
    }

    // —— 间期 ——
    const itv = (f.analysis && f.analysis.intervals) || null;
    const itvMap = [['pr', 'itv-pr'], ['qrs', 'itv-qrs'], ['qt', 'itv-qt'], ['qtc', 'itv-qtc']];
    if (itv && itv.ready) {
      itvMap.forEach(([k, id]) => {
        const v = itv[`${k}_ms`];
        $(id).textContent = v === null || v === undefined ? '-- ms' : `${Math.round(v)} ms`;
        const row = $(id).closest('.itv-row');
        const ref = (itv.ref && itv.ref[k]) || [0, 1];
        const zone = row.querySelector('.itv-zone');
        const ptr = row.querySelector('.itv-ptr');
        const domain = [0, 500];
        const dspan = domain[1] - domain[0];
        zone.style.left = `${((ref[0] - domain[0]) / dspan) * 100}%`;
        zone.style.width = `${((ref[1] - ref[0]) / dspan) * 100}%`;
        if (v === null || v === undefined) { ptr.style.opacity = '0'; }
        else {
          ptr.style.opacity = '1';
          ptr.style.left = `calc(${clamp(((v - domain[0]) / dspan) * 100, 0, 100)}% - 1.5px)`;
        }
      });
      const iq = $('itv-quality');
      iq.textContent = itv.reliable ? '齐全' : '部分缺测';
      iq.dataset.level = itv.reliable ? 'ok' : 'medium';
      const note = $('itv-note');
      note.className = 'note';
      note.textContent = `${itv.n_beats} 拍平均 · 上采样 ${itv.fs_up} Hz · 量化 ±${itv.precision_ms} ms` +
        (itv.reason ? ` · ${itv.reason}` : '') + ' · 演示级测量';
    } else {
      itvMap.forEach(([, id]) => { $(id).textContent = '-- ms'; });
      const iq = $('itv-quality');
      iq.textContent = '--';
      iq.dataset.level = '';
      const note = $('itv-note');
      note.className = 'note';
      note.textContent = itv ? (itv.reason || '数据积累中…') : '未启用 intervals 插件';
    }

    // —— 质量 ——
    const qual = (f.analysis && f.analysis.quality) || null;
    const sqi = qual && qual.sqi !== undefined ? Number(qual.sqi) : null;
    const barEl = $('sqi-bar');
    if (barEl) {
      barEl.style.width = `${Math.round(clamp(Number.isFinite(sqi) ? sqi : 0, 0, 1) * 100)}%`;
      barEl.dataset.level = sqi === null ? '' : (sqi < 0.3 ? 'high' : (sqi < 0.55 ? 'medium' : ''));
    }
    $('sqi-text').textContent = sqi === null ? '--' : sqi.toFixed(2);
    const qs = $('qual-state');
    qs.textContent = qual ? qual.electrode || '--' : '--';
    qs.dataset.level = qual ? (qual.electrode === '良好' ? 'ok' : (qual.electrode === '劣化' ? 'medium' : (qual.electrode === '脱落' ? 'high' : ''))) : '';
    const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : null);   // 数值守卫
    const ppv = qual ? num(qual.pp_code) : null;
    const rrv = qual ? num(qual.rate_ratio) : null;
    const dpv = qual ? num(qual.drop_rate) : null;
    $('q-pp').textContent = ppv === null ? '--' : `${Math.round(ppv)} 码`;
    $('q-rate').textContent = rrv === null ? '--' : `${(rrv * 100).toFixed(0)}%`;
    $('q-drop').textContent = dpv === null ? '--' : `${dpv.toFixed(2)} 个/s`;
    $('q-electrode').textContent = qual ? (qual.electrode || '--') : '--';

    // —— 会话（记录 / 快速测量）——
    const rec = (f.session && f.session.recorder) || null;
    state.recorder = rec || { recording: false };
    const btn = $('btn-record');
    btn.classList.toggle('recording', !!state.recorder.recording);
    $('rec-label').textContent = state.recorder.recording
      ? `记录中 ${state.recorder.n_written ?? 0} 点` : '开始记录';

    const q30 = (f.session && f.session.quick30) || null;
    state.quick30 = q30 || { active: false, progress: 0 };
    const qbtn = $('btn-quick30');
    if (state.quick30.active) {
      qbtn.classList.add('is-busy');
      qbtn.textContent = `快速测量 ${Math.round((state.quick30.progress || 0) * 100)}%`;
    } else {
      qbtn.classList.remove('is-busy');
      qbtn.textContent = '30s 测量';
    }

    // —— 会话统计条（把左栏底部空间变成有用信息）——
    const setT = (id, v) => { const el = $(id); if (el && el.textContent !== v) el.textContent = v; };
    const upSec = state.t0 ? Math.floor((Date.now() - state.t0) / 1000) : 0;
    setT('ss-uptime', `${String(Math.floor(upSec / 60)).padStart(2, '0')}:${String(upSec % 60).padStart(2, '0')}`);
    setT('ss-samples', String(f.n_total));
    setT('ss-recorded', String((rec && rec.n_written) || 0));
    setT('ss-dropped', String(f.dropped));
    const fsEst = f.health && Number(f.health.est_fs_sw);
    setT('ss-fs', Number.isFinite(fsEst) ? fsEst.toFixed(2) : '--');
    setT('ss-fps', String(Math.round(state.fps)));

    flashChanged();                                    // 关键数值变化时微亮
    updateReadoutDom();                                // 读数 t 偏移随时间推移刷新（5 Hz）
  }

  // ---------------------------------------------------------------- 声音（WebAudio）
  let audioCtx = null;
  function beepFor(level) {
    if (!state.sound) return;
    if (level !== 'high' && level !== 'medium') return;
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      const seq = level === 'high' ? [880, 880, 880] : [660, 660];
      seq.forEach((freq, i) => {
        const t = audioCtx.currentTime + i * 0.16;
        const o = audioCtx.createOscillator();
        const g = audioCtx.createGain();
        o.frequency.value = freq;
        o.type = 'sine';
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(0.16, t + 0.015);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.13);
        o.connect(g).connect(audioCtx.destination);
        o.start(t); o.stop(t + 0.15);
      });
    } catch (_) { /* 浏览器策略限制时静默 */ }
  }

  // ---------------------------------------------------------------- 命令
  const CMD_TEXT = {
    'recorder.start': '开始记录', 'recorder.stop': '停止记录',
    'quick30.start': '开始 30 s 快速测量', 'loop.set_source': '切换取数源',
  };

  async function cmd(target, action, arg) {
    try {
      const r = await fetch('/api/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target, action, arg }),
      });
      const j = await r.json().catch(() => ({}));
      const label = CMD_TEXT[`${target}.${action}`] || `${target} ${action}`;
      showToast(j && j.ok ? `已发送 · ${label}` : `未入队 · ${label}`);
    } catch (err) {
      console.warn('命令发送失败', err);
      showToast('命令发送失败');
    }
  }

  // ---------------------------------------------------------------- 交互增强
  let toastTimer = 0;
  function showToast(msg) {
    const t = $('toast');
    if (!t) return;
    t.textContent = msg;
    t.hidden = false;
    t.classList.remove('is-out');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      t.classList.add('is-out');                       // 淡出后再隐藏（有始有终）
      toastTimer = setTimeout(() => { t.hidden = true; t.classList.remove('is-out'); }, 220);
    }, 1200);
  }

  function togglePause(force) {
    state.paused = force === undefined ? !state.paused : !!force;
    if (!state.paused) state.jumpAbs = null;            // 恢复播放即清除事件定位锚点
    const b = $('btn-pause');
    if (b) {
      b.setAttribute('aria-pressed', state.paused ? 'true' : 'false');
      b.textContent = state.paused ? '继续' : '暂停';
    }
    const badge = $('pause-badge');
    if (badge) badge.hidden = !state.paused;
    showToast(state.paused ? '画面已暂停（采集继续）' : '已恢复');
  }

  function setWindow(sec, opts) {
    state.windowSec = clamp(Number(sec) || 6, 2, 20);
    document.querySelectorAll('#win-seg .seg-btn').forEach((b) => {
      const on = Number(b.dataset.w) === state.windowSec;
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    try {
      const u = new URL(location.href);
      u.searchParams.set('win', String(state.windowSec));
      history.replaceState(null, '', u);
    } catch (_) { /* 忽略 */ }
    if (state.mode === 'sweep') state.sweepBase = Math.floor(state.cursor);   // 换窗即开新段
    segSync('win-seg');
    if (!(opts && opts.silent)) { beginCrossfade(); showToast(`时间窗 ${state.windowSec} s`); }
  }

  function cycleWindow() {
    const i = WIN_CHOICES.indexOf(state.windowSec);
    setWindow(WIN_CHOICES[(i + 1 + WIN_CHOICES.length) % WIN_CHOICES.length]);
  }

  // —— 波形读数（悬停 / 触摸 / 点击钉住 / ←→ 键盘移动）——
  function absFromX(x) {
    const g = state.geom;
    if (!g) return null;
    let abs;
    if (state.mode === 'scroll') {
      abs = Math.floor(state.cursor - g.wSamples + (x / g.pxPerSec) * state.fs);
    } else {
      const base = (x > g.headX) ? g.prevBase : state.sweepBase;
      abs = Math.floor(base + (x / g.pxPerSec) * state.fs);
    }
    // 越界（换窗/重连/已被环淘汰）时不报假读数
    return (abs >= ring.start && abs < ring.end) ? abs : null;
  }

  // 取读数样本：钉住=锁定样本号；悬停=按像素实时解析（含抽稀/空洞容差）
  function readoutSample() {
    const r = state.readout;
    if (!r) return null;
    // 只有"钉住且已锁定样本号"才按 abs 解析；否则按像素位置实时解析（键盘读数即此路径）
    const abs0 = (r.pinned && r.abs !== null && r.abs !== undefined) ? r.abs : absFromX(r.x);
    if (abs0 === null || abs0 === undefined) return null;
    const g = state.geom;
    if (!g || abs0 < ring.start || abs0 >= ring.end) return null;
    const tol = Math.max(4, Math.round((state.waveStep || 1) * 2));
    for (let d = 0; d <= tol; d++) {
      const cands = d === 0 ? [abs0] : [abs0 - d, abs0 + d];
      for (let i = 0; i < cands.length; i++) {
        const a = cands[i];
        if (a < ring.start || a >= ring.end) continue;
        const p = ((a % ring.cap) + ring.cap) % ring.cap;
        if (ring.abs[p] === a) return { abs: a, code: ring.data[p] };
      }
    }
    return { abs: abs0, code: null };
  }

  function updateReadoutDom() {
    const ro = $('readout');
    if (!ro) return;
    if (!state.readout) { ro.hidden = true; return; }
    ro.hidden = false;
    const s = readoutSample();
    const tEl = $('ro-t');
    const vEl = $('ro-v');
    if (!s) {
      if (tEl.textContent !== 't --') tEl.textContent = 't --';
      if (vEl.textContent !== '-- mV') vEl.textContent = '-- mV';
      return;
    }
    const t = (s.abs - state.cursor) / state.fs;
    const tTxt = `t ${t >= 0 ? '+' : ''}${t.toFixed(2)} s`;
    const mv = s.code === null ? null : (s.code - 32768) * MV_PER_CODE;
    const vTxt = mv === null ? '-- mV' : `${mv >= 0 ? '+' : ''}${mv.toFixed(1)} mV`;
    // 只在变化时写：读数区带 aria-live，重复写会反复触发播报
    if (tEl.textContent !== tTxt) tEl.textContent = tTxt;
    if (vEl.textContent !== vTxt) vEl.textContent = vTxt;
  }

  function setReadout(clientX, clientY) {
    const canvas = $('wave-canvas');
    const g = state.geom;
    if (!canvas || !g) return;
    const rect = canvas.getBoundingClientRect();
    const x = clamp(clientX - rect.left, 0, rect.width);
    const y = clamp(clientY - rect.top, 0, rect.height);
    const keepPinned = !!(state.readout && state.readout.pinned);
    state.readout = { x, y, abs: null, pinned: keepPinned };
    const s = readoutSample();
    if (s) state.readout.abs = s.abs;              // 钉住时锁定这个样本号
    updateReadoutDom();
  }

  function clearReadout() {
    state.readout = null;
    const ro = $('readout');
    if (ro) ro.hidden = true;
  }

  function bindReadout() {
    const canvas = $('wave-canvas');
    if (!canvas) return;
    canvas.addEventListener('mousemove', (ev) => {
      if (state.readout && state.readout.pinned) return;
      setReadout(ev.clientX, ev.clientY);
    });
    canvas.addEventListener('mouseleave', () => {
      if (!(state.readout && state.readout.pinned)) clearReadout();
    });
    canvas.addEventListener('click', (ev) => {
      if (state.readout && state.readout.pinned) {
        clearReadout();
        showToast('已取消钉住');
      } else {
        setReadout(ev.clientX, ev.clientY);
        if (state.readout) state.readout.pinned = true;
        showToast('已钉住读数（再次点击取消）');
      }
    });
    const touch = (ev) => {
      if (state.readout && state.readout.pinned) return;   // 与鼠标路径一致
      const t = ev.touches && ev.touches[0];
      if (t) setReadout(t.clientX, t.clientY);
    };
    canvas.addEventListener('touchstart', touch, { passive: true });
    canvas.addEventListener('touchmove', touch, { passive: true });
  }

  function bindKeys() {
    document.addEventListener('keydown', (ev) => {
      // ① 模态优先：帮助面板打开时，Esc/? 与 Tab 环形陷阱必须可用（与焦点落在哪个按钮无关）
      const modal = $('help');
      if (modal && !modal.hidden) {
        if (ev.key === 'Escape' || ev.key === '?' || ev.key === '/') {
          ev.preventDefault();
          closeHelp();
          return;
        }
        if (ev.key === 'Tab') {
          const items = modal.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
          if (items.length) {
            const first = items[0];
            const last = items[items.length - 1];
            const act = document.activeElement;
            const outside = !modal.contains(act);
            if (ev.shiftKey && (act === first || outside)) { ev.preventDefault(); last.focus(); }
            else if (!ev.shiftKey && (act === last || outside)) { ev.preventDefault(); first.focus(); }
          }
        }
        return;                                       // 面板打开时其余快捷键一律不生效
      }
      // ② 修饰键 / 长按重复：不拦截（避免 Ctrl+R 误开记录等）
      if (ev.ctrlKey || ev.metaKey || ev.altKey || ev.repeat) return;
      // ③ 只有"真正的输入控件"才早退；BUTTON 不再早退
      //    （否则点过任意按钮后 G/T/1-3/S/R 等快捷键全部失灵）
      const tag = (ev.target && ev.target.tagName) || '';
      if (/INPUT|TEXTAREA|SELECT/.test(tag) || (ev.target && ev.target.isContentEditable)) return;
      const g = state.geom;
      if (ev.code === 'Space') {
        if (tag === 'BUTTON') return;                 // 空格对按钮是"点击"语义，不抢
        ev.preventDefault();
        togglePause();
        return;
      }
      if (ev.key === 'ArrowLeft' || ev.key === 'ArrowRight') {   // 键盘移动读数（可访问性）
        const x0 = state.readout ? state.readout.x : (g ? g.plotW * 0.5 : 0);
        const x1 = clamp(x0 + (ev.key === 'ArrowLeft' ? -4 : 4), 0, g ? g.plotW : 0);
        state.readout = { x: x1, y: 0, abs: null, pinned: true };
        const s = readoutSample();
        if (s) state.readout.abs = s.abs;
        updateReadoutDom();
        ev.preventDefault();
        return;
      }
      if (ev.key === 'Escape') {
        // Esc 逐层退出：专注模式 → 取消钉住读数
        if (document.body.classList.contains('focus-mode')) toggleFocusMode(false);
        else if (state.readout && state.readout.pinned) { clearReadout(); showToast('已取消钉住'); }
        ev.preventDefault();
        return;
      }
      if (ev.key === 'ArrowUp' || ev.key === 'ArrowDown') {     // 基线平移（↑ 上移）
        adjustBaseline(ev.key === 'ArrowUp' ? 5 : -5);
        ev.preventDefault();
        return;
      }
      const k = String(ev.key || '').toLowerCase();
      if (k === '?' || k === '/' || k === 'h') { if (helpOpen()) closeHelp(); else openHelp(); }
      else if (k === 'c') toggleTheme();
      else if (k === 'f') toggleFocusMode();
      else if (k === 'g') cycleGain();
      else if (k === 'b') toggleBeatSound();
      else if (k === 'e') exportShot();
      else if (k === 'd') resetView();
      else if (k === 'r') $('btn-record').click();
      else if (k === 's') $('btn-mode').click();
      else if (k === 't') cycleWindow();
      else if (k === '1') setWindow(WIN_CHOICES[0]);
      else if (k === '2') setWindow(WIN_CHOICES[1]);
      else if (k === '3') setWindow(WIN_CHOICES[2]);
    });
  }

  // ---------------------------------------------------------------- 可访问性与入场
  // 心率数字：首次出值从 0 滚动一次（≤220 ms），之后变化做 180 ms 平滑过渡；
  // 等宽数字 + tabular-nums，因此滚动过程不引起布局跳动。
  const _hrAnim = { cur: null, raf: 0 };
  function setHrText(v) {
    const el = $('v-hr');
    if (!el) return;
    const target = (v === null || v === undefined) ? null : Math.round(Number(v));
    if (target === null || !Number.isFinite(target)) {
      cancelAnimationFrame(_hrAnim.raf);
      _hrAnim.cur = null;
      el.textContent = '--';
      return;
    }
    // 起点取"屏幕上正在显示的值"，避免动画中途来新值时起点跳变
    const shown = parseInt(el.textContent, 10);
    const from = _hrAnim.raf && Number.isFinite(shown) ? shown
      : (_hrAnim.cur === null ? 0 : _hrAnim.cur);
    const first = _hrAnim.cur === null;
    _hrAnim.cur = target;
    if (REDUCED || target === from) { el.textContent = String(target); return; }
    cancelAnimationFrame(_hrAnim.raf);
    const t0 = performance.now();
    const dur = first ? 220 : 180;
    const step = (now) => {
      const p = Math.min(1, (now - t0) / dur);
      const eased = 1 - Math.pow(1 - p, 3);
      el.textContent = String(Math.round(from + (target - from) * eased));
      if (p < 1) _hrAnim.raf = requestAnimationFrame(step);
      else el.textContent = String(target);
    };
    _hrAnim.raf = requestAnimationFrame(step);
  }

  // 帮助面板（模态）：打开/关闭 + 焦点归还
  let _lastFocus = null;
  function helpOpen() { const m = $('help'); return !!m && !m.hidden; }
  function openHelp() {
    const m = $('help');
    if (!m || !m.hidden) return;
    _lastFocus = document.activeElement;
    m.hidden = false;
    const btn = $('btn-help-close');
    if (btn) btn.focus();
  }
  function closeHelp() {
    const m = $('help');
    if (!m || m.hidden) return;
    m.hidden = true;
    if (_lastFocus && _lastFocus.focus) _lastFocus.focus();
  }

  // 高对比主题（C 键 / 页脚按钮；记住选择）
  function toggleTheme(force) {
    const on = force === undefined ? !document.body.classList.contains('theme-hc') : !!force;
    document.body.classList.toggle('theme-hc', on);
    const b = $('btn-theme');
    if (b) b.setAttribute('aria-pressed', on ? 'true' : 'false');
    try { localStorage.setItem('ecgTheme', on ? 'hc' : 'default'); } catch (_) { /* 忽略 */ }
  }

  // 读屏摘要：默认关闭（opt-in），开启后约每 15 s 播报，且内容不变则不重复播报
  let _srOn = false;
  let _srLast = 0;
  function toggleSr(force) {
    _srOn = force === undefined ? !_srOn : !!force;
    const b = $('btn-sr');
    if (b) b.setAttribute('aria-pressed', _srOn ? 'true' : 'false');
    const el = $('sr-summary');
    if (el) el.textContent = _srOn ? '读屏摘要已开启' : '读屏摘要已关闭';
    if (_srOn) _srLast = 0;                       // 开启后允许立即播报一次
  }
  function srTick() {
    if (!_srOn) return;
    const now = performance.now();
    if (now - _srLast < 15000) return;             // 15 s 一次，且只在内容变化时写
    const el = $('sr-summary');
    const f = state.last;
    if (!el || !f) return;
    _srLast = now;
    const hrTxt = (f.hr === null || f.hr === undefined)
      ? '心率未知' : `心率 ${Math.round(f.hr)} 次每分`;
    const rh = $('rhythm-status');
    const ql = (f.analysis && f.analysis.quality) || null;
    const next = `${hrTxt}，节律：${(rh && rh.textContent) || '未知'}，`
      + `信号：${(ql && ql.electrode) || '未知'}${state.paused ? '，画面已暂停' : ''}`;
    if (el.textContent !== next) el.textContent = next;
  }

  // 入场：绑定到"首帧数据到达"再播放，否则动画会在波形出现前播完
  let introPlayed = false;
  function playIntro() {
    if (introPlayed) return;
    introPlayed = true;
    if (REDUCED) return;                              // 尊重"减少动效"
    void document.body.offsetWidth;                   // 强制一次样式计算，避免动画被同帧合并
    document.body.classList.add('stagger');           // 参数栏错峰
    document.body.classList.add('intro');             // 顶栏/舞台/页脚/舞台内分层
    setTimeout(() => document.body.classList.remove('intro'), 1200);
  }

  function bindIntroA11y() {
    const bh = $('btn-help');
    if (bh) bh.addEventListener('click', () => (helpOpen() ? closeHelp() : openHelp()));
    const bc = $('btn-help-close');
    if (bc) bc.addEventListener('click', closeHelp);
    const modal = $('help');
    if (modal) modal.addEventListener('click', (ev) => { if (ev.target === modal) closeHelp(); });
    try { if (localStorage.getItem('ecgTheme') === 'hc') toggleTheme(true); } catch (_) { /* 忽略 */ }
    const bt = $('btn-theme');
    if (bt) bt.addEventListener('click', () => toggleTheme());
    const bs = $('btn-sr');
    if (bs) bs.addEventListener('click', () => toggleSr());
    setInterval(srTick, 1000);
  }

  // ---------------------------------------------------------------- 增益/基线/定位/专注/节拍/导出
  const GAIN_CHOICES = ['auto', '0.5', '1', '2'];

  function setGain(mode, opts) {
    const m = GAIN_CHOICES.indexOf(String(mode)) >= 0 ? String(mode) : 'auto';
    state.gainMode = m;
    if (m === 'auto') {
      state.manualPx = 0;
      state.manualUntil = 0;
    } else {
      const base = state.autoPx > 0 ? state.autoPx : state.pxPerCode;   // 基准恒为自动拟合值，避免 0.5× 后再点 1× 无变化
      state.manualPx = (opts && opts.noFreeze) ? 0 : base * Number(m);  // 冻结自动标定 × 倍率 → 真正固定量程
      state.manualUntil = performance.now() + 30000;
    }
    clearTimeout(state._gainTimer);
    if (m !== 'auto' && !(opts && opts.keep)) {
      state._gainTimer = setTimeout(() => setGain('auto'), 30000);   // 到期自动回自动并提示
    }
    document.querySelectorAll('#gain-seg .seg-btn').forEach((b) => {
      const on = b.dataset.g === m;
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    segSync('gain-seg');
    if (!(opts && opts.silent)) showToast(m === 'auto' ? '增益：自动' : `增益：固定 ${m}×（30 s 后回自动）`);
  }

  function cycleGain() {
    const i = GAIN_CHOICES.indexOf(state.gainMode);
    setGain(GAIN_CHOICES[(i + 1) % GAIN_CHOICES.length]);
  }

  function adjustBaseline(deltaPx) {
    const canvas = $('wave-canvas');
    const g = state.geom;
    const lim = g ? g.plotH * 0.45 : 160;
    state.basePx = clamp(state.basePx + deltaPx, -lim, lim);
    if (canvas) { /* 下一帧生效 */ }
  }

  function resetView() {
    state.basePx = 0;
    setGain('0.5', { silent: true, noFreeze: true, keep: true });
    showToast('已复位（0.5× 增益 / 基线居中）');
  }

  // 事件定位：点击事件条目 → 暂停并跳到该时刻（利用环的 24 s + 60 s 暂停预算）
  function jumpToEvent(abs) {
    const a = Number(abs);
    if (!Number.isFinite(a)) return;
    if (a < ring.start || a >= ring.end) {
      showToast(`该事件已滚出缓存（环容量 ${(ring.cap / state.fs).toFixed(0)} s）`);
      return;
    }
    togglePause(true);
    const wS = state.fs * state.windowSec;
    const lo = ring.start + wS;
    state.cursor = lo < ring.end ? clamp(a + wS * 0.35, lo, ring.end) : ring.end;
    if (state.mode === 'sweep') state.sweepBase = Math.floor(state.cursor);   // 扫描模式重开一段，定位不落在"上一段"
    state.jumpAbs = a;
    const card = $('ev-card');                         // 被定位的面板闪一下，指明"点的是这里"
    if (card && !REDUCED) {
      card.classList.remove('locate');
      void card.offsetWidth;
      card.classList.add('locate');
    }
    showToast('已定位到事件时刻（空格继续）');
  }

  // 专注模式：隐藏参数栏，波形全宽
  function toggleFocusMode(force) {
    const on = force === undefined ? !document.body.classList.contains('focus-mode') : !!force;
    document.body.classList.toggle('focus-mode', on);
    showToast(on ? '专注模式（F 或 Esc 退出）' : '已退出专注模式');
  }

  // 节拍提示音：每拍一声 40 ms 短促 click（与报警声互不影响）
  function toggleBeatSound(force) {
    state.beatSound = force === undefined ? !state.beatSound : !!force;
    showToast(state.beatSound ? '节拍提示音：开' : '节拍提示音：关');
  }

  function beatClick() {
    if (!state.beatSound || REDUCED) return;
    const now = performance.now();
    if (now - (state._lastClick || 0) < 200) return;      // 去抖：不密于 5 Hz
    state._lastClick = now;
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtx.state === 'suspended') audioCtx.resume();
      const t = audioCtx.currentTime;
      const osc = audioCtx.createOscillator();
      const g = audioCtx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(state.alarm.level === 'high' ? 1200 : 880, t);
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.07, t + 0.006);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.05);
      osc.connect(g);
      g.connect(audioCtx.destination);
      osc.start(t);
      osc.stop(t + 0.06);
    } catch (_) { /* 音频不可用时静默 */ }
  }

  // 快照导出：波形 + 关键参数 + 免责声明（离屏合成，不干扰主循环）
  function exportShot() {
    const wave = $('wave-canvas');
    if (!wave || !wave.width) { showToast('暂无波形可导出'); return; }
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const headH = Math.round(56 * dpr);
    const footH = Math.round(32 * dpr);
    const out = document.createElement('canvas');
    out.width = wave.width;
    out.height = wave.height + headH + footH;
    const c = out.getContext('2d');
    if (!c) { showToast('导出失败：画布不可用'); return; }
    c.fillStyle = '#0B0C0E';
    c.fillRect(0, 0, out.width, out.height);
    c.drawImage(wave, 0, headH);
    // 头部：标题 + 关键参数
    const f = state.last || {};
    const itv = (f.analysis && f.analysis.intervals) || {};
    const q = (f.analysis && f.analysis.quality) || {};
    const alarm = state.alarm || {};
    c.save();
    c.scale(dpr, dpr);
    const W = out.width / dpr;
    c.fillStyle = '#EDEFF2';
    c.font = '600 14px -apple-system, "Segoe UI", "PingFang SC", sans-serif';
    c.fillText('聆心 · ECG 快照', 14, 22);
    c.fillStyle = '#9CA0A7';
    c.font = '11.5px ui-monospace, Consolas, monospace';
    c.fillText(new Date().toLocaleString('zh-CN'), 14, 40);
    const parts = [
      `HR ${(f.hr === null || f.hr === undefined) ? '--' : Math.round(f.hr)} bpm`,
      `节律 ${$('rhythm-status') ? $('rhythm-status').textContent : '--'}`,
      `PR/QRS/QT ${itv.pr_ms ? Math.round(itv.pr_ms) : '--'}/${itv.qrs_ms ? Math.round(itv.qrs_ms) : '--'}/${itv.qt_ms ? Math.round(itv.qt_ms) : '--'} ms`,
      `SQI ${(typeof q.sqi === 'number') ? q.sqi.toFixed(2) : '--'}`,
      `窗口 ${state.windowSec}s · ${state.mode === 'sweep' ? '扫描' : '滚动'}`,
      alarm.level && alarm.level !== 'info' ? `报警 ${alarm.state || ''}` : '',
    ].filter(Boolean).join('   ·   ');
    c.fillText(parts, 14, 46);
    c.restore();
    // 页脚：免责声明
    c.fillStyle = '#7A7E85';
    c.font = `${11.5 * dpr}px ui-monospace, Consolas, monospace`;
    c.fillText('教学演示 · 不作为医疗诊断用途 · 等效 II 导联', Math.round(14 * dpr),
               out.height - Math.round(12 * dpr));
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    const done = (blob) => {
      if (!blob) { showToast('导出失败'); return; }
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `ecg_snapshot_${stamp}.png`;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 4000);
      showToast('已导出快照 PNG');
    };
    if (out.toBlob) out.toBlob(done, 'image/png');
    else done(null);
  }

  // 数值变化微亮（一次遍历，避免逐处插桩；尊重减少动效偏好）
  const _lastText = {};
  // 注：v-hr 不在此列——它走 setHrText 的滚动过渡，闪背景会与滚动互相干扰
  const FLASH_IDS = ['sqi-text', 'm-sdnn', 'm-rmssd', 'm-pnn50', 'm-meanhr',
                     'm-lfhf', 'm-total', 'itv-pr', 'itv-qrs', 'itv-qt', 'itv-qtc'];
  function flashChanged() {
    if (REDUCED || fx.level < 1) return;               // 降级档不做强制回流
    for (let i = 0; i < FLASH_IDS.length; i++) {
      const id = FLASH_IDS[i];
      const el = $(id);
      if (!el) continue;
      const t = el.textContent;
      if (_lastText[id] !== undefined && _lastText[id] !== t && t !== '--') {
        const host = el.closest('.metric') || el.closest('.itv-row') || el;
        host.classList.remove('flash');
        void host.offsetWidth;                 // 重启动画
        host.classList.add('flash');
      }
      _lastText[id] = t;
    }
  }

  function syncModeButton() {
    const b = $('btn-mode');
    if (!b) return;
    b.textContent = state.mode === 'scroll' ? '滚动' : '扫描';
    b.setAttribute('aria-pressed', state.mode === 'sweep' ? 'true' : 'false');
    if (state.mode === 'sweep') state.sweepBase = Math.floor(state.cursor);  // 切到扫描：从干净一段开始
  }

  // 分段控件滑动指示器：只更新索引，位移交给 CSS 过渡（避免逐帧布局）
  function segSync(segId) {
    const seg = $(segId);
    if (!seg) return;
    const btns = seg.querySelectorAll('.seg-btn');
    let idx = 0;
    for (let i = 0; i < btns.length; i++) if (btns[i].classList.contains('is-on')) idx = i;
    seg.style.setProperty('--seg-n', String(Math.max(1, btns.length)));
    seg.style.setProperty('--seg-i', String(idx));
  }

  // 模式/时间窗切换：给"上一帧"做一次 220 ms 交叉淡化（网格与描迹同时过渡）
  function beginCrossfade() {
    if (REDUCED || fx.level < 1) return;                // 降级档不做离屏合成
    const canvas = $('wave-canvas');
    if (!canvas || !canvas.width) return;
    const off = document.createElement('canvas');
    off.width = canvas.width;
    off.height = canvas.height;
    const octx = off.getContext('2d');
    if (!octx) return;
    octx.drawImage(canvas, 0, 0);
    // 记下尺寸：resize/DPR 变化时尺寸不一致，宁可丢弃也不要拉伸旧帧
    fx.fade = { canvas: off, w: canvas.width, h: canvas.height, until: performance.now() + 220 };
  }

  function bindControls() {
    $('btn-record').addEventListener('click', () => {
      const rec = state.recorder && state.recorder.recording;
      cmd('recorder', rec ? 'stop' : 'start');
    });
    $('btn-quick30').addEventListener('click', () => cmd('quick30', 'start'));
    $('btn-mode').addEventListener('click', () => {
      beginCrossfade();                                // 先抓旧帧，再切模式 → 视觉上平滑过渡
      state.mode = state.mode === 'scroll' ? 'sweep' : 'scroll';
      syncModeButton();
    });
    $('btn-sound').addEventListener('click', () => {
      state.sound = !state.sound;
      const b = $('btn-sound');
      b.setAttribute('aria-pressed', state.sound ? 'true' : 'false');
      b.textContent = `声音 ${state.sound ? '开' : '关'}`;
    });
    document.querySelectorAll('#hrv-window-seg .seg-btn').forEach((b) => {
      b.addEventListener('click', () => {
        document.querySelectorAll('#hrv-window-seg .seg-btn').forEach((x) => {
          x.classList.remove('is-on');
          x.setAttribute('aria-pressed', 'false');
        });
        b.classList.add('is-on');
        b.setAttribute('aria-pressed', 'true');
        state.hrvWindow = b.dataset.w;
        segSync('hrv-window-seg');
      });
    });
    // —— 交互绑定：暂停 / 时间窗 / 读数 / 快捷键 ——
    $('btn-pause').addEventListener('click', () => togglePause());
    document.querySelectorAll('#win-seg .seg-btn').forEach((b) => {
      b.addEventListener('click', () => setWindow(Number(b.dataset.w)));
    });
    // 增益档位（自动 / 固定倍率）
    document.querySelectorAll('#gain-seg .seg-btn').forEach((b) => {
      b.addEventListener('click', () => setGain(b.dataset.g));
    });
    // 导出快照
    const shot = $('btn-shot');
    if (shot) shot.addEventListener('click', exportShot);
    // 双击波形：复位增益与基线
    const wc = $('wave-canvas');
    if (wc) wc.addEventListener('dblclick', () => resetView());
    // 事件条目点击 → 跳转定位
    const log = $('event-list');
    if (log) {
      log.addEventListener('click', (ev) => {
        const li = ev.target && ev.target.closest ? ev.target.closest('li[data-abs]') : null;
        if (li) jumpToEvent(li.dataset.abs);
      });
    }
    bindReadout();
    bindKeys();
  }

  // ---------------------------------------------------------------- 主循环
  let lastT = performance.now();
  function loop(now) {
    const dt = Math.max(1e-3, (now - lastT) / 1000);
    lastT = now;
    state.fps = state.fps * 0.9 + (1 / dt) * 0.1;
    fxTick(state.fps);                                 // 效果阶梯：卡顿就自动降级
    if (fx.introP < 1) {                               // 入场揭示：减少动效时直接到稳态
      if (REDUCED) fx.introP = 1;
      else if (!state.firstFrame) fx.introP = Math.min(1, fx.introP + dt / 0.42);
    }
    drawWave();
    drawTrend();
    drawStrip();
    requestAnimationFrame(loop);
  }

  function init() {
    // URL 参数（演示/排障用）：?mode=sweep|scroll&win=3|6|12
    let initWin = null;
    try {
      const qs = new URLSearchParams(location.search);
      if (qs.get('mode') === 'sweep') state.mode = 'sweep';
      const win = parseFloat(qs.get('win') || '');
      if (Number.isFinite(win)) initWin = clamp(win, 2, 20);
      if (qs.get('fx') !== null) {                      // ?fx=0|1|2 固定效果档（排障/演示用）
        const v = parseInt(qs.get('fx'), 10);
        if (v >= 0 && v <= 2) fx.fixed = v;
      }
    } catch (_) { /* 老浏览器忽略 */ }
    ensureRing(state.fs);
    bindControls();
    syncModeButton();
    setWindow(initWin === null ? state.windowSec : initWin, { silent: true });
    segSync('win-seg');
    segSync('hrv-window-seg');
    setGain('0.5', { silent: true, noFreeze: true, keep: true });   // 默认 0.5×（不冻结假标定、不 30 s 回自动）
    segSync('gain-seg');
    setTimeout(playIntro, 1600);                            // 兜底：1.6 s 内无数据也播一次入场（REDUCED 内部跳过）
    bindIntroA11y();                                        // 分层入场 + 帮助面板/主题/读屏摘要
    window.__ecg = { state, fx, ring, setWindow, togglePause, toggleTheme, openHelp, closeHelp,
                     setGain, cycleGain, toggleFocusMode, toggleBeatSound, exportShot, jumpToEvent,
                     resetView, adjustBaseline };
    connect();
    setInterval(() => {
      try { updateDom(); } catch (err) { console.warn('[ecg] updateDom 异常', err); }
    }, 200);
    requestAnimationFrame(loop);
  }

  init();
})();
