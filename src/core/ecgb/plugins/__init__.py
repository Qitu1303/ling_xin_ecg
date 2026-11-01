"""ecgb.plugins —— 可插拔周边功能包。

缺省不启用任何插件时与基线行为 100% 一致；插件异常由宿主隔离并自动停用坏插件。
注意：**不要**在本文件里用 import 触发 ecgb 顶层副作用（tools/gen_regmap.py 会 import ecgb）。
"""
from __future__ import annotations

from typing import Dict

from .base import (  # noqa: F401  （对外 API）
    PLUGIN_REGISTRY,
    PluginBase,
    PluginContext,
    PluginHost,
    available,
    load_params_file,
    parse_plugin_cli,
    register,
    summarize,
)

# 逐个显式导入以填充注册表；任一插件模块坏掉都不影响其它插件与基线运行。
_IMPORT_ERRORS: Dict[str, str] = {}


def _safe_import(modname: str) -> None:
    try:
        __import__(f"{__name__}.{modname}", fromlist=["_"])
    except Exception as exc:                      # pragma: no cover - 环境相关
        _IMPORT_ERRORS[modname] = repr(exc)


for _m in (
    "quality",      # P0 信号质量 / 电极状态
    "hr_alarm",     # P0 心率超限报警
    "recorder",     # P0 长时记录器（CSV 分段）
    "quick30",      # P0 30 秒快速测量
    "voice",        # P0 语音 / 蜂鸣提示
    "hrv",          # P1 HRV（时域 + 频域）
    "arrhythmia",   # P1 心律失常初筛
    "intervals",    # P1 PR/QRS/QT/QTc 自动测量
    "webmon",       # P0 网页实时监视桥
):
    _safe_import(_m)


def import_errors() -> Dict[str, str]:
    """返回导入失败的插件模块（供启动自检/测试使用）。"""
    return dict(_IMPORT_ERRORS)


__all__ = [
    "PLUGIN_REGISTRY", "PluginBase", "PluginContext", "PluginHost",
    "available", "load_params_file", "parse_plugin_cli", "register",
    "summarize", "import_errors",
]
