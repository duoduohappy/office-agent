"""硬件抽象层 HAL（技术方案 7.2 / 7.3 / 7.4 步骤 1）。

目标：上层 Agent / 工具 / 评测逻辑不直接调用 Windows 专有 API，
系统能力统一经本模块访问。CPU 架构差异由 PlatformInfo 归一化，
OS / 桌面环境差异由各 Probe 屏蔽。

**真实性边界**：本模块只做「探测 + 抽象」，不会把模拟平台或能力缺失
包装成「已在国产真机适配」。所有探测结果带 source 与 available 标志；
detect_platform() 注入参数时 simulated=True，兼容性报告必须据此判为未验证。
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 平台识别

_ARCH_MAP = {
    "amd64": "x86_64", "x86_64": "x86_64", "x64": "x86_64", "i386": "x86", "i686": "x86",
    "arm64": "aarch64", "aarch64": "aarch64", "armv8": "aarch64", "armv8l": "aarch64",
    "loongarch64": "loongarch64", "sw_64": "sw_64", "mips64el": "mips64el",
    "riscv64": "riscv64",
}

# 方案 7.2：ARMv8 是 P0/P1 目标；龙芯/申威列为「可识别但不宣称适配」
_TARGET_ARCHES = {"aarch64"}
_XINCHUANG_DISTROS = {"kylin", "uos", "uniontech", "openkylin", "deepin", "loongnix"}


def normalize_arch(machine: str) -> str:
    """把各平台自报的机器名归一化到方案 7.2 的架构名。未知架构原样小写返回。"""
    m = (machine or "").strip().lower()
    return _ARCH_MAP.get(m, m)


def parse_os_release(text: str) -> dict[str, str]:
    """/etc/os-release 解析（去引号、忽略注释），Windows 上传空串返回 {}。"""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


@dataclass
class PlatformInfo:
    system: str
    release: str
    arch: str
    machine_raw: str
    python_version: str
    distro_id: str = ""
    distro_name: str = ""
    distro_version: str = ""
    desktop: str = ""           # ukui / dde / deepin / gnome / windows / ""
    simulated: bool = False

    @property
    def is_windows(self) -> bool:
        return self.system.lower() == "windows"

    @property
    def is_linux(self) -> bool:
        return self.system.lower() == "linux"

    @property
    def is_xinchuang_distro(self) -> bool:
        d = self.distro_id.lower()
        return any(k in d for k in _XINCHUANG_DISTROS) or any(
            k in self.distro_name.lower() for k in ("麒麟", "统信")
        )

    @property
    def is_arm_target(self) -> bool:
        return self.arch in _TARGET_ARCHES

    @property
    def is_real_target(self) -> bool:
        """必须同时满足：真实探测（非模拟）+ 信创 OS + ARM 架构。"""
        return (not self.simulated) and self.is_linux and self.is_xinchuang_distro and self.is_arm_target

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": self.system, "release": self.release,
            "arch": self.arch, "machine_raw": self.machine_raw,
            "python_version": self.python_version,
            "distro_id": self.distro_id, "distro_name": self.distro_name,
            "distro_version": self.distro_version, "desktop": self.desktop,
            "is_xinchuang_distro": self.is_xinchuang_distro,
            "is_arm_target": self.is_arm_target,
            "is_real_target": self.is_real_target,
            "simulated": self.simulated,
        }


def detect_platform(
    *,
    system: str | None = None,
    machine: str | None = None,
    release: str | None = None,
    os_release_text: str | None = None,
    environ: dict[str, str] | None = None,
) -> PlatformInfo:
    """探测平台。全部参数可注入以便离线断言模拟麒麟/ARM，注入即 simulated=True。

    真实运行时不传任何参数：Linux 读 /etc/os-release，Windows 不读（返回空发行版）。
    """
    simulated = any(x is not None for x in (system, machine, release, os_release_text, environ))
    env = environ if environ is not None else dict(os.environ)
    sysname = system or platform.system()
    rel = release or platform.release()
    mach = machine or platform.machine()
    pyver = platform.python_version()

    distro: dict[str, str] = {}
    if os_release_text is not None:
        distro = parse_os_release(os_release_text)
    elif sysname.lower() == "linux":
        try:
            distro = parse_os_release(Path("/etc/os-release").read_text(encoding="utf-8"))
        except OSError:
            distro = {}

    desktop = ""
    if sysname.lower() == "windows":
        desktop = "windows"
    else:
        xdg = (env.get("XDG_CURRENT_DESKTOP") or env.get("DESKTOP_SESSION") or "").lower()
        for key, label in (("ukui", "ukui"), ("dde", "dde"), ("deepin", "dde"),
                           ("gnome", "gnome"), ("kde", "kde"), ("xfce", "xfce")):
            if key in xdg:
                desktop = label
                break

    return PlatformInfo(
        system=sysname, release=rel, arch=normalize_arch(mach), machine_raw=mach,
        python_version=pyver,
        distro_id=distro.get("ID", ""),
        distro_name=distro.get("NAME", distro.get("PRETTY_NAME", "")),
        distro_version=distro.get("VERSION_ID", ""),
        desktop=desktop, simulated=simulated,
    )


# ---------------------------------------------------------------- 内存探针
class MemoryProbe:
    """进程 RSS 采样。Windows 走 tasklist + WinDLL，Linux 走 /proc，无第三方依赖。"""

    OLLAMA_NAMES = {"ollama.exe", "ollama app.exe", "ollama_llama_server.exe"}

    def __init__(self, info: PlatformInfo, proc_root: Path | None = None) -> None:
        self.info = info
        self.proc_root = proc_root or Path("/proc")

    def self_rss_mb(self) -> float:
        if self.info.is_windows:
            return self._windows_self_rss_mb()
        return self._linux_self_rss_mb()

    def process_rss_mb(self, names: set[str]) -> tuple[float, str]:
        if self.info.is_windows:
            return self.windows_process_rss(names | self.OLLAMA_NAMES), "tasklist(ollama*)"
        return self._linux_process_rss(names), "/proc/*/status VmRSS(ollama*)"

    # ---- Linux ---------------------------------------------------------
    def _linux_self_rss_mb(self) -> float:
        try:
            statm = self.proc_root / "self" / "statm"
            if not statm.exists():
                return 0.0
            pages = int(statm.read_text(encoding="utf-8").split()[1])
            return pages * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024
        except (OSError, ValueError, IndexError, AttributeError):
            return 0.0

    def _linux_process_rss(self, names: set[str]) -> float:
        """按 /proc/*/cmdline 匹配推理进程，读 status 的 VmRSS(kB)。

        与 Windows 分支一样按前缀/子串匹配（ollama / llama-server），避免
        只统计 serve 进程而漏掉真正加载权重的 runner。
        """
        total = 0.0
        try:
            entries = list(self.proc_root.glob("[0-9]*/cmdline"))
        except OSError:
            return 0.0
        for cmdline in entries:
            try:
                raw = cmdline.read_bytes().replace(b"\x00", b" ").lower()
                if b"ollama" not in raw and b"llama-server" not in raw and b"llama_server" not in raw:
                    continue
                status = (cmdline.parent / "status").read_text(encoding="utf-8")
                for line in status.splitlines():
                    if line.startswith("VmRSS:"):
                        total += int(line.split()[1]) / 1024
                        break
            except (OSError, ValueError, IndexError):
                continue
        return total

    # ---- Windows -------------------------------------------------------
    @staticmethod
    def windows_process_rss(names: set[str], runner=None) -> float:
        """runner 可注入，便于离线断言在不启动 tasklist 的情况下验证解析逻辑。"""
        run = runner or subprocess.run
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=10, creationflags=creationflags,
        )
        # 中文 Windows 输出 GBK/OEM，不能 text=True 按 UTF-8 解码。
        out = proc.stdout.decode("utf-8", errors="replace")
        total = 0.0
        for line in out.splitlines():
            parts = [p.strip(' "') for p in line.split('","')]
            if len(parts) < 5:
                continue
            pname = parts[0].lower()
            if not (pname in names or pname.startswith("ollama") or "llama-server" in pname):
                continue
            try:
                total += float(parts[-1].replace(",", "").replace(".", "").replace(" K", "").strip()) / 1024
            except ValueError:
                continue
        return total

    @staticmethod
    def _windows_self_rss_mb() -> float:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counter = PROCESS_MEMORY_COUNTERS()
        counter.cb = ctypes.sizeof(counter)
        # 必须声明 restype/argtypes，否则 64 位伪句柄被截断，静默返回 0 MB。
        psapi = ctypes.WinDLL("psapi")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        ok = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counter), counter.cb
        )
        return counter.WorkingSetSize / 1024 / 1024 if ok else 0.0


# ---------------------------------------------------------------- 字体解析
# 逻辑字体 -> 平台候选（按优先级）。GB/T 9704-2012 要求标题小标宋、正文仿宋。
_FONT_CANDIDATES: dict[str, dict[str, list[str]]] = {
    "仿宋_GB2312": {
        "windows": ["仿宋_GB2312", "仿宋"],
        "linux": ["方正仿宋_GBK", "方正仿宋简体", "仿宋_GB2312", "仿宋", "FandolFangSong"],
    },
    "方正小标宋简体": {
        "windows": ["方正小标宋简体"],
        "linux": ["方正小标宋_GBK", "方正小标宋简体", "FZXiaoBiaoSong-B05", "方正小标宋"],
    },
    "黑体": {
        "windows": ["黑体"],
        "linux": ["方正黑体_GBK", "黑体", "Noto Sans CJK SC", "WenQuanYi Micro Hei"],
    },
    "Times New Roman": {
        "windows": ["Times New Roman"],
        "linux": ["Times New Roman", "Liberation Serif", "Nimbus Roman"],
    },
}
# 逻辑回退名：即使目标机没装字体，也保留 OOXML 里的中文字体声明。
_FONT_LOGICAL_FALLBACK = {
    "仿宋_GB2312": "仿宋", "方正小标宋简体": "方正小标宋简体",
    "黑体": "黑体", "Times New Roman": "Times New Roman",
}


class FontResolver:
    """把逻辑字体名映射到目标机实际安装的字体；不装时返回逻辑名并标注。"""

    def __init__(self, info: PlatformInfo, installed: set[str] | None = None) -> None:
        self.info = info
        self._installed = installed

    def available_fonts(self) -> set[str]:
        if self._installed is not None:
            return self._installed
        found: set[str] = set()
        if self.info.is_windows:
            root = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
            try:
                for p in root.iterdir():
                    if p.suffix.lower() in {".ttf", ".ttc", ".otf"}:
                        found.add(p.stem)
            except OSError:
                pass
        else:
            try:
                proc = subprocess.run(["fc-list", ":family"], capture_output=True, timeout=5)
                for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
                    found.update(x.strip() for x in line.split(",") if x.strip())
            except (OSError, subprocess.SubprocessError):
                pass
        return found

    def resolve(self, logical_name: str) -> tuple[str, str]:
        """返回 (实际字体名, 说明)。说明会写进兼容性报告，不静默降级。"""
        family = "windows" if self.info.is_windows else "linux"
        candidates = _FONT_CANDIDATES.get(logical_name, {}).get(family, [logical_name])
        if self.info.is_windows:
            # Windows 字体族名（如「仿宋_GB2312」）与字体文件名（simfang.ttf）
            # 不是一回事，扫文件目录会误判「未安装」并改变既有 OOXML 声明。
            # 保持与适配前完全一致的字体名，避免两轮评测报告不可复现。
            return candidates[0], "按 OOXML 字体族名声明，Windows 不做文件名匹配"
        installed = self.available_fonts()
        for c in candidates:
            if c in installed or any(c.lower() == f.lower() for f in installed):
                return c, f"fc-list 检测到已安装字体 {c}"
        # 未探测到任何候选：保留逻辑名，字体声明不会从 OOXML 里消失。
        fallback = _FONT_LOGICAL_FALLBACK.get(logical_name, logical_name)
        return fallback, f"fc-list 未检测到 {logical_name} 的候选，仅写入逻辑字体名（渲染依赖目标机字体）"


# ---------------------------------------------------------------- GUI / Office / 推理
class GuiBackend:
    def __init__(self, info: PlatformInfo) -> None:
        self.info = info

    def probe(self) -> dict[str, Any]:
        if self.info.is_windows:
            pkg, name = "uiautomation", "windows-uia"
        else:
            pkg, name = "pyatspi", "linux-atspi"
        try:
            __import__(pkg)
            available, note = True, f"{pkg} 可导入"
        except ImportError:
            available, note = False, f"{pkg} 未安装，GUI 自动化能力不可用"
        return {"name": name, "package": pkg, "available": available,
                "desktop": self.info.desktop, "note": note, "source": "importlib probe"}


class OfficeSuite:
    """国产办公软件探测 + OOXML 回读校验；不假装能驱动未安装的 WPS。"""

    EXECUTABLES = {
        "wps": ["wps", "wps-office"], "et": ["et"], "wpp": ["wpp"],
        "libreoffice": ["libreoffice", "soffice"],
        "yozo": ["yozo", "eioffice"],
        "msoffice": ["winword.exe", "excel.exe"],
    }

    def __init__(self, info: PlatformInfo) -> None:
        self.info = info

    def probe(self) -> dict[str, Any]:
        found: dict[str, str] = {}
        for suite, cmds in self.EXECUTABLES.items():
            for cmd in cmds:
                path = shutil.which(cmd)
                if path:
                    found[suite] = cmd
                    break
        converter = "libreoffice" if "libreoffice" in found else None
        return {
            "installed": sorted(found), "executables": found,
            "wps_available": any(k in found for k in ("wps", "et", "wpp")),
            "headless_converter": converter,
            "can_headless_convert": converter is not None,
            "note": "WPS Linux 多数版本不提供命令行转换接口；检测到 CLI 才执行转换，否则标 SKIP",
            "source": "shutil.which",
        }

    @staticmethod
    def verify_ooxml(path: Path) -> dict[str, Any]:
        """用 python-docx / openpyxl 回读，验证生成的 OOXML 可被标准解析器读取。"""
        suffix = path.suffix.lower()
        try:
            if suffix == ".docx":
                from docx import Document
                doc = Document(str(path))
                return {"ok": True, "format": "docx", "paragraphs": len(doc.paragraphs),
                        "note": "python-docx 回读成功"}
            if suffix == ".xlsx":
                from openpyxl import load_workbook
                wb = load_workbook(str(path), read_only=True)
                sheets = list(wb.sheetnames)
                wb.close()
                return {"ok": True, "format": "xlsx", "sheets": sheets,
                        "note": "openpyxl 回读成功"}
            return {"ok": False, "format": suffix, "note": "未支持的格式"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "format": suffix, "note": f"{type(exc).__name__}: {exc}"}

    def convert_to_pdf(self, src: Path, out_dir: Path) -> dict[str, Any]:
        probe = self.probe()
        if not probe["can_headless_convert"]:
            return {"ok": False, "status": "skip",
                    "note": "未检测到 LibreOffice/soffice 命令行，WPS 无稳定 CLI；未在 WPS 图形界面验证"}
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                [probe["headless_converter"], "--headless", "--convert-to", "pdf",
                 "--outdir", str(out_dir), str(src)],
                capture_output=True, timeout=120,
            )
            produced = list(out_dir.glob(f"{src.stem}*.pdf"))
            return {"ok": proc.returncode == 0 and bool(produced), "status": "measured",
                    "pdf": produced[0].name if produced else None,
                    "note": "LibreOffice 转换，仅代表 OOXML 可解析，不等价于 WPS 渲染一致"}
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "status": "error", "note": f"{type(exc).__name__}: {exc}"}


class InferenceBackend:
    def __init__(self, info: PlatformInfo) -> None:
        self.info = info

    def probe(self) -> dict[str, Any]:
        found: dict[str, str] = {}
        for name, cmds in {
            "ollama": ["ollama"], "llama-server": ["llama-server", "llama-cli"],
            "ascend": ["npu-smi"], "cambricon": ["cnmon"],
        }.items():
            for cmd in cmds:
                p = shutil.which(cmd)
                if p:
                    found[name] = cmd
                    break
        return {"available": found, "backend": next(iter(found), None),
                "note": "仅探测可执行文件，未验证模型推理；ARM 实机需另跑 llama.cpp 探针",
                "source": "shutil.which"}


# ---------------------------------------------------------------- 路径 / 环境变量
class PathProvider:
    def __init__(self, info: PlatformInfo) -> None:
        self.info = info

    def cache_root(self) -> Path:
        override = os.getenv("OA_CACHE_ROOT")
        if override:
            return Path(override)
        if self.info.is_windows:
            return Path(os.getenv("LOCALAPPDATA", ".")) / "office-agent" / "cache"
        return Path(os.getenv("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "office-agent"

    def model_dir(self) -> Path:
        override = os.getenv("OLLAMA_MODELS")
        return Path(override) if override else self.cache_root() / "ollama" / "models"

    def user_env(self, name: str) -> str:
        """用户级环境变量（方案 7.4 步骤 1：不直接调 Windows 注册表）。"""
        current = os.getenv(name, "")
        if current:
            return current
        if self.info.is_windows:
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                    return str(winreg.QueryValueEx(key, name)[0])
            except (ImportError, OSError):
                return ""
        # Linux：environment.d / /etc/environment.d 的 KEY=VALUE，不解析 shell 脚本
        for base in (Path.home() / ".config" / "environment.d", Path("/etc/environment.d")):
            try:
                for conf in sorted(base.glob("*.conf")):
                    for line in conf.read_text(encoding="utf-8").splitlines():
                        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
                        if m and m.group(1) == name:
                            return m.group(2).strip().strip('"').strip("'")
            except OSError:
                continue
        return ""


# ---------------------------------------------------------------- 门面
class HAL:
    def __init__(self, info: PlatformInfo | None = None, proc_root: Path | None = None,
                 installed_fonts: set[str] | None = None) -> None:
        self.info = info or detect_platform()
        self.memory = MemoryProbe(self.info, proc_root=proc_root)
        self.fonts = FontResolver(self.info, installed=installed_fonts)
        self.gui = GuiBackend(self.info)
        self.office = OfficeSuite(self.info)
        self.inference = InferenceBackend(self.info)
        self.paths = PathProvider(self.info)

    def user_env(self, name: str) -> str:
        return self.paths.user_env(name)

    def capabilities(self) -> dict[str, Any]:
        return {
            "platform": self.info.to_dict(),
            "python_min_version": "3.10",
            "python_note": "本项目使用 PEP 604 (X | Y) 等 3.10+ 语法；麒麟 V10 / UOS V20 自带 Python 3.7/3.8 通常不够，需另装 3.10+",
            "gui": self.gui.probe(),
            "office": self.office.probe(),
            "inference": self.inference.probe(),
            "fonts": {k: {"resolved": v[0], "note": v[1]}
                      for k, v in ((name, self.fonts.resolve(name)) for name in _FONT_CANDIDATES)},
            "real_target_verified": self.info.is_real_target,
            "disclaimer": "能力探测不等于国产真机兼容性验证；模拟平台结果一律不得标为 PASS",
        }


_HAL_SINGLETON: HAL | None = None


def get_hal() -> HAL:
    global _HAL_SINGLETON
    if _HAL_SINGLETON is None:
        _HAL_SINGLETON = HAL()
    return _HAL_SINGLETON


def self_test() -> dict[str, Any]:
    """进程内自测：本机真实探测 + 模拟麒麟 ARM 平台，两者必须可区分。"""
    real = HAL().capabilities()
    fake = HAL(info=detect_platform(
        system="Linux", machine="aarch64", release="5.10.0-kylin",
        os_release_text='ID=kylin\nNAME="Kylin Linux Advanced Server"\nVERSION_ID="V10"\n',
        environ={"XDG_CURRENT_DESKTOP": "UKUI:GNOME"},
    )).capabilities()
    return {
        "real_platform": real["platform"],
        "real_target_verified": real["real_target_verified"],
        "simulated": fake["platform"],
        "simulated_is_real_target": fake["platform"]["is_real_target"],
        "simulated_font": fake["fonts"]["仿宋_GB2312"]["resolved"],
        "gui": real["gui"],
        "inference": real["inference"],
    }


if __name__ == "__main__":
    import json
    print(json.dumps(self_test(), ensure_ascii=False, indent=2))
