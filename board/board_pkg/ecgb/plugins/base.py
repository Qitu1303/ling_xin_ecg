"""base.py —— 可插拔插件框架（宿主 + 基类 + 上下文）。

回调在采集线程内、time.sleep(poll_dt) 之前同步执行：插件耗时直接转化为漏样，故宿主逐插件
计时并告警，异常按插件事隔离（连续 3 次自动停用）。插件只读 Snapshot，禁止写寄存器。
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import traceback
from collections import deque
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


# ----------------------------------------------------------------------------
# 注册表
# ----------------------------------------------------------------------------
PLUGIN_REGISTRY: Dict[str, type] = {}


def register(cls: type) -> type:
    """把插件类登记进全局注册表（用类属性 name 作为键）。"""
    name = getattr(cls, "name", None)
    if not name or name == "unnamed":
        raise ValueError(f"{cls.__name__} 必须有唯一的 name 类属性")
    if name in PLUGIN_REGISTRY:
        raise ValueError(f"插件名重复：{name}")
    PLUGIN_REGISTRY[name] = cls
    return cls


def available() -> List[str]:
    """已注册的插件名列表（按优先级排序）。"""
    return sorted(PLUGIN_REGISTRY, key=lambda n: getattr(PLUGIN_REGISTRY[n], "priority", 50))


# ----------------------------------------------------------------------------
# 上下文（插件唯一可见的对外接口）
# ----------------------------------------------------------------------------
class PluginContext:
    """插件上下文：只读环境信息 + 日志 + 跨插件发布/订阅 + 事件上报。

    刻意**不暴露** afe / 寄存器写权限：写权限只留在 board_demo.py。
    `loop` 句柄只允许读属性与调用 `set_source()`（宿主在采集线程内代执行）。
    """

    def __init__(self, fs: Optional[float], out_dir: str, verbose: bool = True,
                 loop: Any = None) -> None:
        self.fs = fs
        self.out_dir = out_dir
        self.verbose = verbose
        self.loop = loop                      # AcquisitionLoop 句柄（只读 + set_source）
        self._host: Optional["PluginHost"] = None
        self._name = "ctx"

    # ---- 内部绑定 ----
    def _bind(self, host: "PluginHost", name: str) -> None:
        self._host = host
        self._name = name

    @property
    def host(self) -> Optional["PluginHost"]:
        """宿主句柄（webmon 等需要读 status()/submit()；不要用它绕过插件边界）。"""
        return self._host

    def child(self, name: str) -> "PluginContext":
        """给某个插件用的具名视图（日志前缀不同，共享同一 host）。"""
        view = PluginContext(self.fs, self.out_dir, self.verbose, self.loop)
        view._host = self._host
        view._name = name
        return view

    # ---- 日志 ----
    def log(self, msg: str) -> None:
        if self.verbose:
            print(f"[{self._name}] {msg}", flush=True)

    # ---- 跨插件发布 / 订阅 ----
    def publish(self, key: str, value: Any) -> None:
        """发布本插件最近一次结果（线程安全；webmon 等消费方读取）。"""
        assert self._host is not None, "上下文未绑定宿主"
        self._host._shared[key] = value

    def latest(self, key: str, default: Any = None) -> Any:
        assert self._host is not None, "上下文未绑定宿主"
        return self._host._shared.get(key, default)

    # ---- 事件上报（报警/状态变化）----
    def emit_event(self, level: str, text: str, code: str = "", **extra: Any) -> None:
        """上报一条事件。level: high / medium / low / info。

        - 级别语义对齐 IEC 60601-1-8：high=红、medium=黄、low=青、info=灰。
        - 宿主负责入队 + 分发给其它插件的 on_event（语音/网页据此发声/显示）。
        """
        assert self._host is not None, "上下文未绑定宿主"
        self._host._emit_event(self._name, level, text, code, **extra)


# ----------------------------------------------------------------------------
# 插件基类
# ----------------------------------------------------------------------------
class PluginBase:
    """所有插件的基类。

    子类最少只需覆盖 `name` 与 `on_snapshot`；参数通过构造关键字传入，
    由 PluginHost 用 `cls(**params)` 实例化（params 来自 --plugins-file 或默认值）。
    """

    name = "unnamed"
    priority = 50          # 数值小者先收到快照（分析类在前，消费类在后）
    description = ""

    def __init__(self, **params: Any) -> None:
        self.params: Dict[str, Any] = dict(params)
        self.enabled = True
        self.ctx: Optional[PluginContext] = None
        # 统计
        self.n_snapshots = 0
        self.n_events = 0
        self.n_errors = 0
        self.n_errors_consec = 0
        self.n_slow = 0
        self.total_ms = 0.0
        self.max_ms = 0.0
        self.last_error = ""

    # ---- 子类可覆盖 ----
    def on_start(self) -> None:
        """构造后、进入主循环前调用一次；可开线程/开文件/起服务。"""

    def on_snapshot(self, snap: Any) -> None:
        """每帧快照。**必须快进快出**（预算 ≤ 5 ms，宿主会计量并告警）。"""

    def on_event(self, event: Dict[str, Any]) -> None:
        """收到（含其它插件上报的）事件；用于语音/网页联动。"""

    def on_command(self, action: str, arg: Any = None) -> Any:
        """处理外部命令（网页/脚本经 host.submit 投递，在采集线程内执行）。"""
        return None

    def on_close(self) -> None:
        """收尾：关线程/关文件/关服务；必须幂等。"""

    # ---- 宿主调用 ----
    def _do_start(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.on_start()

    def _do_snapshot(self, snap: Any) -> float:
        t0 = time.perf_counter()
        self.on_snapshot(snap)
        return (time.perf_counter() - t0) * 1000.0

    def _do_event(self, event: Dict[str, Any]) -> None:
        self.n_events += 1
        self.on_event(event)

    def stats(self) -> Dict[str, Any]:
        avg = (self.total_ms / self.n_snapshots) if self.n_snapshots else 0.0
        return {
            "name": self.name,
            "enabled": self.enabled,
            "priority": self.priority,
            "n_snapshots": self.n_snapshots,
            "n_events": self.n_events,
            "n_errors": self.n_errors,
            "n_errors_consec": self.n_errors_consec,
            "n_slow": self.n_slow,
            "avg_ms": round(avg, 4),
            "max_ms": round(self.max_ms, 3),
            "last_error": self.last_error,
        }


# ----------------------------------------------------------------------------
# 宿主
# ----------------------------------------------------------------------------
class PluginHost:
    """插件宿主：注册、启用/禁用、快照分发（异常隔离 + 计时）、命令队列、事件总线。

    线程模型：dispatch() / 命令 drain / close() 由采集线程调用；submit() 可被任意线程调用，
    只做"入队 + 立即返回"；_shared 的读写在 GIL 下对 dict 单键赋值原子，聚合读容忍弱一致。
    """

    def __init__(self, ctx: PluginContext, plugins: Iterable[PluginBase], *,
                 warn_ms: float = 15.0, max_consecutive_failures: int = 3,
                 verbose: bool = True) -> None:
        self.ctx = ctx
        self.warn_ms = float(warn_ms)
        self.max_consecutive_failures = int(max_consecutive_failures)
        self.verbose = verbose
        self.plugins: List[PluginBase] = sorted(plugins, key=lambda p: p.priority)
        self._shared: Dict[str, Any] = {}
        self._events: deque = deque(maxlen=500)
        self._event_seq = 0
        self._commands: "queue.Queue[Tuple[str, str, Any]]" = queue.Queue(maxsize=256)
        self._lock = threading.Lock()
        self._n_dispatch = 0
        self._n_cmd_dropped = 0
        self._n_event_dropped = 0
        self._started: List[str] = []
        self._failed_start: Dict[str, str] = {}
        self._closed = False
        ctx._bind(self, "host")
        for p in self.plugins:
            pctx = ctx.child(p.name)
            try:
                p._do_start(pctx)
                self._started.append(p.name)
            except Exception as exc:                     # 启动失败：停用但不拖垮其它插件
                p.enabled = False
                p.last_error = f"start: {exc!r}"
                self._failed_start[p.name] = repr(exc)
                if verbose:
                    print(f"[host] 插件 {p.name} 启动失败，已停用：{exc!r}", flush=True)
        if verbose:
            self._print_banner()

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_spec(cls, spec: str, *, fs: Optional[float] = None, out_dir: str = ".",
                  loop: Any = None, verbose: bool = True,
                  params: Optional[Dict[str, Dict[str, Any]]] = None,
                  extra: Optional[Iterable[PluginBase]] = None) -> "PluginHost":
        """按 "a,b,c" 规格建宿主。

        - 空串 / None → 空宿主（**与基线行为 100% 一致**）；
        - "all" → 启用全部已注册插件；
        - params: {插件名: {参数...}}，对应 --plugins-file 的 JSON。
        """
        params = params or {}
        names: List[str] = []
        raw = (spec or "").strip()
        if raw:
            if raw == "all":
                names = available()
            else:
                names = [s.strip() for s in raw.split(",") if s.strip()]
        unknown = [n for n in names if n not in PLUGIN_REGISTRY]
        if unknown:
            raise ValueError(f"未知插件：{unknown}；可用：{available()}")
        built: List[PluginBase] = []
        for n in names:
            kw = dict(params.get(n, {}))
            built.append(PLUGIN_REGISTRY[n](**kw))
        if extra:
            built.extend(extra)
        ctx = PluginContext(fs, out_dir, verbose, loop)
        return cls(ctx, built, verbose=verbose)

    # ------------------------------------------------------------------ 分发
    def dispatch(self, snap: Any) -> None:
        """每帧入口：先执行待办命令（采集线程内），再按优先级分发快照。"""
        if self._closed:
            return
        self._drain_commands()
        for p in self.plugins:
            if not p.enabled:
                continue
            try:
                ms = p._do_snapshot(snap)
            except Exception as exc:
                p.n_errors += 1
                p.n_errors_consec += 1
                p.last_error = f"snapshot: {exc!r}"
                if p.n_errors_consec >= self.max_consecutive_failures and p.enabled:
                    p.enabled = False
                    self._emit_event("host", "medium",
                                     f"插件 {p.name} 连续 {p.n_errors_consec} 次异常，已自动停用",
                                     "plugin_disabled")
                if self.verbose:
                    print(f"[host] 插件 {p.name} 本帧异常（已隔离）：{exc!r}", flush=True)
                continue
            p.n_snapshots += 1
            p.n_errors_consec = 0                     # 成功一帧即清零"连续"计数
            p.total_ms += ms
            if ms > p.max_ms:
                p.max_ms = ms
            if ms > self.warn_ms:
                p.n_slow += 1
                if self.verbose and p.n_slow % 50 == 1:
                    print(f"[host] 警告：{p.name} 单帧 {ms:.1f} ms（预算 {self.warn_ms:.0f} ms，"
                          f"累计 {p.n_slow} 次，耗时会长在漏样账上）", flush=True)
        self._n_dispatch += 1

    def _emit_event(self, source: str, level: str, text: str, code: str = "",
                    **extra: Any) -> None:
        ev = {
            "seq": self._event_seq,
            "t": time.time(),
            "mono": time.monotonic(),
            "source": source,
            "level": level,
            "code": code,
            "text": text,
        }
        if extra:
            ev.update(extra)
        self._event_seq += 1
        self._events.append(ev)
        for p in self.plugins:
            if not p.enabled or p.name == source:
                continue
            try:
                p._do_event(ev)
            except Exception as exc:
                p.n_errors += 1
                p.last_error = f"event: {exc!r}"
                if self.verbose:
                    print(f"[host] 插件 {p.name} 事件回调异常（已隔离）：{exc!r}", flush=True)

    # ------------------------------------------------------------------ 命令
    def submit(self, target: str, action: str, arg: Any = None) -> bool:
        """任意线程投递命令，采集线程在下一帧执行。队列满则丢弃并计数。"""
        try:
            self._commands.put_nowait((target, action, arg))
            return True
        except queue.Full:
            self._n_cmd_dropped += 1
            return False

    def _drain_commands(self) -> None:
        while True:
            try:
                target, action, arg = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                if target == "loop" and self.ctx.loop is not None:
                    if action == "set_source":
                        self.ctx.loop.set_source(str(arg))
                    else:
                        self.ctx.log(f"未知 loop 命令：{action}")
                else:
                    hit = [p for p in self.plugins if p.name == target]
                    if not hit:
                        self.ctx.log(f"未知命令目标：{target}")
                        continue
                    for p in hit:
                        if p.enabled:
                            p.on_command(action, arg)
            except Exception as exc:
                if self.verbose:
                    print(f"[host] 命令执行异常 target={target} action={action}：{exc!r}",
                          flush=True)

    # ------------------------------------------------------------------ 查询
    def events(self, since_seq: int = -1) -> List[Dict[str, Any]]:
        return [e for e in list(self._events) if e["seq"] > since_seq]

    def latest(self, key: str, default: Any = None) -> Any:
        return self._shared.get(key, default)

    def status(self) -> Dict[str, Any]:
        """给网页/脚本看的一份轻量状态（线程安全：只读原子量）。"""
        return {
            "n_dispatch": self._n_dispatch,
            "n_cmd_dropped": self._n_cmd_dropped,
            "plugins": [p.stats() for p in self.plugins],
            "events": len(self._events),
            "started": list(self._started),
            "failed_start": dict(self._failed_start),
            "closed": self._closed,
        }

    # ------------------------------------------------------------------ 收尾
    def close(self) -> None:
        """幂等收尾：反序关闭（消费类先于数据源类）。"""
        if self._closed:
            return
        self._closed = True
        for p in reversed(self.plugins):
            try:
                p.on_close()
            except Exception as exc:
                if self.verbose:
                    print(f"[host] 插件 {p.name} 收尾异常（已忽略）：{exc!r}", flush=True)

    # ------------------------------------------------------------------ 杂项
    def _print_banner(self) -> None:
        if not self.plugins:
            print("[host] 未启用插件（与基线行为一致）", flush=True)
            return
        desc = "，".join(f"{p.name}({p.priority})" for p in self.plugins)
        print(f"[host] 插件 {len(self.plugins)} 个：{desc}", flush=True)
        if self._failed_start:
            print(f"[host] 启动失败：{self._failed_start}", flush=True)


# ----------------------------------------------------------------------------
# CLI 辅助（board_demo 用；把 --plugins / --plugins-file 从 argv 中摘出来）
# ----------------------------------------------------------------------------
def parse_plugin_cli(argv: List[str]) -> Tuple[List[str], str, Optional[str]]:
    """从 argv 里剔除 `--plugins a,b` 与 `--plugins-file x.json`。

    返回 (剩余 argv 列表, plugins 规格字符串, plugins_file 路径或 None)。
    缺省返回 ('', None) ⇒ board_demo 行为与基线 100% 一致。
    """
    rest: List[str] = []
    spec = ""
    pf: Optional[str] = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--plugins":
            spec = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
            continue
        if a == "--plugins-file":
            pf = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        rest.append(a)
        i += 1
    return rest, spec, pf


def load_params_file(path: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """读 --plugins-file 的 JSON：{"webmon": {"port": 8765}, ...}。"""
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层必须是对象：{{\"插件名\": {{参数}}}}")
    out: Dict[str, Dict[str, Any]] = {}
    for k, v in data.items():
        if not isinstance(v, dict):
            raise ValueError(f"{path} 中 {k} 的参数必须是对象")
        out[k] = dict(v)
    return out


def summarize(host: PluginHost) -> str:
    """人类可读的单行统计（board_demo 收尾打印用）。"""
    items = [f"{p.name}:帧{p.n_snapshots}/错{p.n_errors}/峰{p.max_ms:.1f}ms"
             for p in host.plugins]
    return " | ".join(items) if items else "（无插件）"


def beats_of(snap: Any) -> Dict[str, Any]:
    """从快照 extra 里安全取 R 峰/RR 扩展。

    返回 {} 表示本次快照没有可用拍序列（检测器缺席/未出结果/wave 太短），消费方一律用 .get。
    """
    try:
        b = (snap.extra or {}).get("beats") or {}
        if not b.get("idx_abs"):
            return {}
        return b
    except Exception:
        return {}
