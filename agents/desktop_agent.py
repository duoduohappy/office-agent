"""Desktop Agent（方案 4.5）。

UI 树读取而非截图+视觉模型：省 token、精度高、可移植到麒麟/统信（AT-SPI）。
本阶段实现白名单 + 能力探测 + 操作审计；Windows UIA 的具体调用
在装有 uiautomation 包时启用，未装则如实报缺失，不伪造操作成功。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

# 白名单：只允许操作这些应用，禁止系统设置和敏感目录
APP_WHITELIST = {
    "邮件客户端": ["foxmail.exe", "outlook.exe", "thunderbird.exe"],
    "文件管理器": ["explorer.exe", "nautilus", "dolphin"],
    "浏览器": ["chrome.exe", "msedge.exe", "firefox.exe"],
}

BLOCKED_PATH_KEYWORDS = ["windows", "system32", "appdata", "/etc", "/boot", "regedit"]


@dataclass
class DesktopResult:
    ok: bool
    action: str = ""
    detail: str = ""
    errors: list[str] = field(default_factory=list)


class DesktopAgent:
    def __init__(self, audit=None) -> None:
        self.audit = audit  # 可选 StructuredMemory，记录每次操作

    @staticmethod
    def ui_backend() -> str:
        """探测当前可用的 UI 自动化后端。"""
        if os.name == "nt":
            try:
                import uiautomation  # noqa: F401

                return "windows-uia"
            except ImportError:
                return "unavailable"
        try:
            import pyatspi  # noqa: F401

            return "linux-atspi"
        except ImportError:
            return "unavailable"

    def is_allowed(self, app: str) -> bool:
        key = app.lower()
        return any(key in exe.lower() or exe.lower() in key for exes in APP_WHITELIST.values() for exe in exes)

    def is_path_allowed(self, path: str) -> bool:
        low = path.lower().replace("\\", "/")
        return not any(k in low for k in BLOCKED_PATH_KEYWORDS)

    def inspect_window(self, app: str) -> DesktopResult:
        """读取窗口 UI 控件树（能力探测版）。"""
        if not self.is_allowed(app):
            return DesktopResult(False, "inspect_window", "", [f"应用 {app} 不在白名单内，拒绝操作"])
        backend = self.ui_backend()
        if backend == "unavailable":
            return DesktopResult(
                False, "inspect_window", "",
                ["UI 自动化后端未安装（Windows 需 uiautomation / 麒麟统信需 pyatspi）；"
                 "本机能力探测完成，未执行任何 GUI 操作"],
            )
        # 实际项目中这里遍历控件树并按 (name, control_type, automation_id) 输出
        return DesktopResult(True, "inspect_window", f"已通过 {backend} 读取 {app} 的控件树（占位）")

    def execute(self, app: str, action: str, path: str = "") -> DesktopResult:
        """执行桌面操作前的统一安全闸门；真实执行需要人工确认。"""
        if path and not self.is_path_allowed(path):
            return DesktopResult(False, action, "", [f"路径命中敏感目录黑名单：{path}"])
        result = self.inspect_window(app)
        if not result.ok:
            return result
        if self.audit is not None:
            self.audit.log_step("desktop", action, {"app": app, "path": path}, True, 0, agent="DesktopAgent")
        return DesktopResult(
            True, action,
            "安全校验通过；实际 GUI 操作需人工确认后执行（本阶段不自动发送/删除）",
        )
