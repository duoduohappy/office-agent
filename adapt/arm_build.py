"""ARM 交叉编译支持与 aarch64 依赖审计（方案 7.4 步骤 2）。

**真实性边界**：本项目自身是纯 Python，没有需要编译的 C/C++ 源码；
需要交叉编译的是推理后端 llama.cpp（GGUF 量化模型的 ARM 版）。
本模块做三件可在本机真实完成的事：
  1. 探测交叉工具链，缺什么如实记录，不写「已编译」；
  2. 生成 aarch64 CMake toolchain 与 llama.cpp 构建脚本（脚本本身不在本机执行）；
  3. 真实执行 pip download，审计依赖链在 aarch64/CPython 3.10 下是否有可用 wheel。

脚本执行与 ARM 实机推理验证留给用户拿到目标机后运行，本模块输出中
cross_compile_executed 恒为 False，除非由目标机上的显调用另行写入。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from adapt.hal import detect_platform

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = PROJECT_ROOT / "adapt" / "reports"
REPORT_PATH = REPORT_DIR / "aarch64_wheel_audit.json"

# 纯 Python 业务依赖（不含可选的 chromadb 重依赖）
CORE_REQUIREMENTS = ["python-docx>=1.1", "openpyxl>=3.1", "python-pptx>=0.6", "pypdf>=5.0"]
OPTIONAL_REQUIREMENTS = ["chromadb>=1.0"]

TARGET_PLATFORM_TAGS = [
    "manylinux_2_28_aarch64", "manylinux_2_17_aarch64",
    "manylinux2014_aarch64", "linux_aarch64",
]
DEFAULT_INDEX = "https://mirrors.cloud.tencent.com/pypi/simple/"


def probe_toolchains() -> dict[str, Any]:
    """探测交叉编译工具链。只报事实，不因为检测到 clang 就宣称能编译。"""
    tools = ["aarch64-linux-gnu-gcc", "aarch64-linux-gnu-g++", "cmake", "ninja",
             "clang", "zig", "docker", "qemu-aarch64-static", "file"]
    found: dict[str, str] = {}
    for t in tools:
        p = shutil.which(t)
        if p:
            found[t] = "available"
    gnu = "aarch64-linux-gnu-gcc" in found and "aarch64-linux-gnu-g++" in found
    return {
        "tools": found,
        "missing": [t for t in tools if t not in found],
        "gnu_cross_toolchain": gnu,
        "can_cross_compile_here": gnu and "cmake" in found,
        "container_emulation": "docker" in found and "qemu-aarch64-static" in found,
        "note": "本机为 x86_64 Windows 开发环境；方案 7.4 步骤 2 要求在 ARM Linux 或带交叉工具链的 Linux 上执行",
        "source": "shutil.which",
    }


def build_plan() -> dict[str, Any]:
    """方案 7.2/7.4 对应的交叉编译步骤，全部标注执行主体与未执行状态。"""
    return {
        "project_native_code": False,
        "native_component": "llama.cpp / Ollama 推理后端（C++），不在本仓库内",
        "target_cpu": ["飞腾腾锐 D3000M (ARMv8 FTC862)", "鲲鹏 920 (ARMv8 TaiShan)",
                       "飞腾腾云 S5000C (ARMv8)"],
        "steps": [
            {"id": 1, "action": "取得目标机架构与内核信息",
             "where": "ARM Linux 目标机", "command": "uname -m && cat /etc/os-release", "executed_here": False},
            {"id": 2, "action": "交叉编译 llama.cpp（或直接使用官方 ARM 版 Ollama）",
             "where": "带 aarch64 工具链的 Linux 构建机",
             "command": "bash scripts/cross_build_llamacpp.sh --verify", "executed_here": False},
            {"id": 3, "action": "在目标机用 Qwen2.5-7B Q4_K_M 跑最小推理探针",
             "where": "ARM Linux 目标机",
             "command": "./build-aarch64/bin/llama-cli -m qwen2.5-7b-instruct-q4_k_m.gguf -p '国产化适配测试' -n 32",
             "executed_here": False},
            {"id": 4, "action": "采集 TTFT / 内存 / 纯 CPU 口径基线",
             "where": "ARM Linux 目标机",
             "command": "python -m eval.run_eval", "executed_here": False},
        ],
        "cross_compile_executed": False,
        "arm_inference_verified": False,
        "python_min_version": "3.10",
        "python_version_note": (
            "银河麒麟 V10 / 统信 UOS V20 常见自带 Python 3.7/3.8，本项目使用 PEP 604 (X | Y) "
            "与 match 等 3.10+ 语法，目标机需另装 CPython 3.10+；此项必须在实机确认"
        ),
    }


def audit_wheels(
    requirements: list[str] | None = None,
    *,
    dest: Path | None = None,
    python_version: str = "3.10",
    index_url: str = DEFAULT_INDEX,
    timeout: int = 180,
    include_optional: bool = True,
) -> dict[str, Any]:
    """真实调用 pip download，逐个检查 aarch64/CPython wheel 是否可获取。

    wheel 文件落在缓存目录（默认 D 盘缓存，不入仓库）；报告只写包名与结果。
    """
    reqs = list(requirements or (CORE_REQUIREMENTS + (OPTIONAL_REQUIREMENTS if include_optional else [])))
    dest = dest or Path(__import__("os").environ.get(
        "OA_AARCH64_WHEELHOUSE", str(PROJECT_ROOT / "data" / "wheelhouse-aarch64")))
    dest.mkdir(parents=True, exist_ok=True)
    results = []
    for req in reqs:
        platform_flags: list[str] = []
        for tag in TARGET_PLATFORM_TAGS:
            platform_flags += ["--platform", tag]
        cmd = [
            sys.executable, "-m", "pip", "download", "--disable-pip-version-check",
            "--no-input", "--only-binary=:all:", "--no-deps",
            "--python-version", python_version, "--implementation", "cp",
            "--abi", f"cp{python_version.replace('.', '')}",
            "--index-url", index_url, "--dest", str(dest),
        ] + platform_flags + [req]
        t0 = time.monotonic()
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
            out = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
            ok = proc.returncode == 0
            wheels = sorted(p.name for p in dest.glob("*.whl") if req.split(">")[0].split("=")[0].replace("-", "_") in p.name.lower())
            kinds = ["aarch64_native" if "aarch64" in w else "pure_python_any" for w in wheels]
            results.append({
                "requirement": req, "ok": ok, "seconds": round(time.monotonic() - t0, 1),
                "wheels": wheels[:5], "wheel_kinds": kinds[:5],
                "error": "" if ok else _brief_error(out),
            })
        except subprocess.TimeoutExpired:
            results.append({"requirement": req, "ok": False, "seconds": timeout,
                            "wheels": [], "error": f"pip download 超时 {timeout}s"})
        except OSError as exc:
            results.append({"requirement": req, "ok": False, "seconds": 0,
                            "wheels": [], "error": f"{type(exc).__name__}: {exc}"})
    return {
        "target": {"arch": "aarch64", "python": python_version,
                   "abi": f"cp{python_version.replace('.', '')}", "platform_tags": TARGET_PLATFORM_TAGS,
                   "index": index_url},
        "packages": results,
        "all_core_ok": all(r["ok"] for r in results if r["requirement"] in CORE_REQUIREMENTS),
        "optional_ok": all(r["ok"] for r in results if r["requirement"] in OPTIONAL_REQUIREMENTS),
        "native_aarch64_packages": [r["requirement"] for r in results
                                    if "aarch64_native" in r.get("wheel_kinds", [])],
        "pure_python_any_packages": [r["requirement"] for r in results
                                     if r.get("wheel_kinds") and set(r["wheel_kinds"]) == {"pure_python_any"}],
        "wheelhouse_files": len(list(dest.glob("*.whl"))),
        "scope_note": (
            "--no-deps：只验证各包自身是否有 aarch64 可用 wheel。wheel_kinds 区分 "
            "pure_python_any（与架构无关的纯 Python wheel）与 aarch64_native（真正的 ARM 原生二进制）；"
            "前者不能被称作「ARM 编译产物」。依赖链需在目标机用完整 pip install 复验"
        ),
        "cross_compile_executed": False,
        "real_arm_install_verified": False,
    }


def _brief_error(out: str) -> str:
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    for key in ("ERROR:", "No matching distribution", "not find", "Could not"):
        hits = [l for l in lines if key in l]
        if hits:
            return hits[-1][:220]
    return (lines[-1] if lines else "pip 非零退出")[:220]


def write_report(report: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return REPORT_PATH


def self_test() -> dict[str, Any]:
    tc = probe_toolchains()
    return {
        "toolchains": tc,
        "plan": build_plan(),
        "host": detect_platform().to_dict(),
        "cross_compile_executed": False,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="aarch64 依赖审计 / 交叉编译工具链探测")
    parser.add_argument("--audit", action="store_true", help="真实执行 pip download 审计")
    parser.add_argument("--python-version", default="3.10")
    parser.add_argument("--no-optional", action="store_true", help="跳过可选的 chromadb")
    args = parser.parse_args(argv)
    report = {
        "host": detect_platform().to_dict(),
        "toolchains": probe_toolchains(),
        "build_plan": build_plan(),
    }
    if args.audit:
        report["wheel_audit"] = audit_wheels(
            python_version=args.python_version, include_optional=not args.no_optional)
    path = write_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2)[:4000])
    print(f"\nreport -> {path.relative_to(PROJECT_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
