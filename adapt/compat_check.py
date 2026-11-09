"""麒麟 / UOS / WPS Linux 兼容性验证探针（方案 7.4 步骤 3、4）。

在**目标机**上一键运行，逐项给出 PASS / WARN / FAIL / SKIP 与证据。
在开发机（x86_64 Windows）上也能运行，但所有只可能发生在信创目标机上的
项目一律标 SKIP，绝不因为「脚本跑通了」就写成「兼容性验证通过」。

用法：
    python -m adapt.compat_check                       # 写本机基线到 adapt/reports/
    python -m adapt.compat_check --out data/outbox/x  # 目标机另存报告
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from adapt.arm_build import probe_toolchains
from adapt.hal import HAL, PlatformInfo, detect_platform

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = PROJECT_ROOT / "adapt" / "reports"

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"


def _check(cid: str, name: str, status: str, detail: str,
           evidence: Any = None, target_only: bool = False) -> dict[str, Any]:
    return {"id": cid, "name": name, "status": status, "detail": detail,
            "evidence": evidence, "target_only": target_only}


def _enforce_no_fake_pass(checks: list[dict[str, Any]], info: PlatformInfo) -> list[dict[str, Any]]:
    """模拟平台 / 非信创目标机不得产生目标机专属项的 PASS，防止自欺。"""
    if not info.simulated:
        return checks
    for c in checks:
        if c["target_only"] and c["status"] == PASS:
            c["status"] = SKIP
            c["detail"] = "[模拟平台，强制降级] " + c["detail"]
    return checks


def run_checks(hal: HAL | None = None, *, out_dir: Path | None = None) -> dict[str, Any]:
    hal = hal or HAL()
    info = hal.info
    # 模拟平台永远不算目标机：即使注入了麒麟/arm64 也不能给出 PASS
    target = info.is_linux and info.is_xinchuang_distro and not info.simulated
    checks: list[dict[str, Any]] = []

    # ---- OS / 架构 ----
    if target:
        checks.append(_check("os", "信创 OS 识别", PASS,
                             f"检测到 {info.distro_name} {info.distro_version}（{info.distro_id}）",
                             {"desktop": info.desktop}, True))
    else:
        checks.append(_check("os", "信创 OS 识别", SKIP,
                             f"当前为开发机 {info.system} {info.release}，需在麒麟 V10 / UOS V20 目标机执行",
                             {"system": info.system}, True))
    checks.append(_check("arch", "CPU 架构", PASS if target and info.is_arm_target else SKIP,
                         f"归一化架构 {info.arch}（原始 {info.machine_raw}）",
                         {"target_arches": ["aarch64"]}, True))
    py = tuple(int(x) for x in info.python_version.split(".")[:2])
    checks.append(_check("python", "Python 版本", PASS if py >= (3, 10) else FAIL,
                         f"{info.python_version}；本项目语法下限 3.10，麒麟/UOS 自带 3.7/3.8 通常需另装",
                         {"min": "3.10"}))

    # ---- 依赖 ----
    deps: dict[str, str] = {}
    for mod, pkg in (("docx", "python-docx"), ("openpyxl", "openpyxl"),
                     ("pptx", "python-pptx"), ("pypdf", "pypdf")):
        try:
            __import__(mod)
            deps[pkg] = "ok"
        except Exception as exc:  # noqa: BLE001
            deps[pkg] = f"{type(exc).__name__}: {exc}"
    checks.append(_check("deps", "核心文档依赖可导入",
                         PASS if all(v == "ok" for v in deps.values()) else FAIL,
                         "python-docx / openpyxl / python-pptx / pypdf", deps))
    try:
        import chromadb  # noqa: F401
        chroma = {"available": True, "version": getattr(chromadb, "__version__", "unknown")}
        checks.append(_check("chroma", "Chroma 向量库", PASS, "真实 chromadb 可导入", chroma))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("chroma", "Chroma 向量库", WARN,
                             f"未安装，将降级到 fallback-hash-cosine：{type(exc).__name__}", None))

    # ---- 中文字体（WPS 渲染的关键） ----
    font_name, font_note = hal.fonts.resolve("仿宋_GB2312")
    if info.is_windows:
        checks.append(_check("fonts", "公文中文字体", SKIP,
                             f"开发机按 OOXML 字体族名写入 {font_name}；目标机需另验方正仿宋_GBK / FandolFangSong",
                             {"resolved": font_name}, True))
    elif "fc-list 检测到" in font_note:
        checks.append(_check("fonts", "公文中文字体", PASS, font_note, {"resolved": font_name}, True))
    else:
        checks.append(_check("fonts", "公文中文字体", WARN,
                             font_note + "；公文在 WPS 中可能回退成宋体，版式验收不通过",
                             {"resolved": font_name}, True))

    # ---- GUI 后端 ----
    gui = hal.gui.probe()
    if info.is_windows:
        checks.append(_check("gui", "AT-SPI / UKUI / DDE 桌面自动化", SKIP,
                             f"开发机后端 {gui['name']}（available={gui['available']}）；需在麒麟 UKUI / UOS DDE 实机验证",
                             gui, True))
    else:
        checks.append(_check("gui", "AT-SPI / UKUI / DDE 桌面自动化",
                             PASS if gui["available"] else WARN, gui["note"], gui, True))

    # ---- 办公套件 ----
    office = hal.office.probe()
    if office["wps_available"]:
        checks.append(_check("wps", "WPS Linux / 永中 Office 安装", PASS,
                             "检测到 " + ", ".join(office["installed"]), office, True))
    else:
        checks.append(_check("wps", "WPS Linux / 永中 Office 安装", SKIP,
                             "未检测到 wps/et/wpp/永中，无法做国产办公软件渲染兼容性验证",
                             office, True))

    # ---- OOXML 回读（本机真实执行，不依赖 WPS） ----
    ooxml = _ooxml_roundtrip(hal, out_dir or DEFAULT_REPORT_DIR)
    checks.append(_check("ooxml", "公文 docx / 待办 xlsx 回读",
                         PASS if ooxml["ok"] else FAIL,
                         "用 python-docx / openpyxl 回读本项目的 OOXML 产出；"
                         "仅证明标准解析器可读，不等价于 WPS 渲染一致", ooxml))
    conv = {"status": SKIP, "note": "未检测到 WPS 或 LibreOffice 命令行转换接口，未做版式渲染比对"}
    if office["wps_available"] and office["can_headless_convert"]:
        conv = hal.office.convert_to_pdf(Path(ooxml["docx"]), (out_dir or DEFAULT_REPORT_DIR) / "pdf")
    checks.append(_check("wps_render", "WPS 版式渲染比对",
                         PASS if conv.get("ok") else SKIP, conv.get("note", ""), conv, True))

    # ---- 推理后端 ----
    infer = hal.inference.probe()
    checks.append(_check("inference", "端侧推理后端",
                         PASS if infer["backend"] else SKIP,
                         infer["note"], infer, True))
    checks.append(_check("cpu_baseline", "纯 CPU 内存 / TTFT 基线", SKIP,
                         "方案 8.2 的 <6GB 与 <1.5s 需在纯 CPU 信创机复测；"
                         "开发机为 GPU 混合 offload，历史报告数值不作为信创口径", None, True))

    checks = _enforce_no_fake_pass(checks, info)
    counts = {s: sum(1 for c in checks if c["status"] == s)
              for s in (PASS, WARN, FAIL, SKIP)}
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "platform": info.to_dict(),
        "simulated_platform": info.simulated,
        "toolchains": probe_toolchains(),
        "checks": checks,
        "summary": counts,
        "real_target_verified": info.is_real_target,
        "cross_compile_executed": False,
        "arm_inference_verified": False,
        "disclaimer": (
            "SKIP 表示本环境无法验证，不是通过。只有同时满足"
            "「非模拟探测 + 麒麟/UOS Linux + aarch64」时 real_target_verified 才为 true；"
            "并且仍需人工在 WPS 中打开公文做版式目视验收。"
        ),
    }


def _ooxml_roundtrip(hal: HAL, base: Path) -> dict[str, Any]:
    """真实调用本项目的文档工具生成 docx/xlsx 再回读，失败如实返回。"""
    try:
        from core.tool_registry import ToolRegistry
        from tools import doc_tools

        # 产物落在已被 .gitignore 排除的 outbox，避免二进制文件进公开仓库
        base.mkdir(parents=True, exist_ok=True)
        reg = ToolRegistry().extend(doc_tools.DOC_TOOLS)
        docx_rel = "data/outbox/compat/minutes.docx"
        xlsx_rel = "data/outbox/compat/todos.xlsx"
        r1 = reg.dispatch("write_minutes_docx", {
            "path": docx_rel, "title": "国产适配兼容性验证纪要",
            "summary": "本文件由兼容性探针生成，用于验证 OOXML 回读与字体声明。", "todos": "[]"})
        r2 = reg.dispatch("write_todos_xlsx", {
            "path": xlsx_rel,
            "todos": json.dumps([{"who": "办公室", "what": "完成目标机验证", "when": "待定"}], ensure_ascii=False)})
        v1 = hal.office.verify_ooxml(PROJECT_ROOT / docx_rel)
        v2 = hal.office.verify_ooxml(PROJECT_ROOT / xlsx_rel)
        return {"ok": r1.ok and r2.ok and v1["ok"] and v2["ok"],
                "docx": docx_rel, "xlsx": xlsx_rel,
                "docx_check": v1, "xlsx_check": v2}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def render_markdown(report: dict[str, Any]) -> str:
    p = report["platform"]
    s = report["summary"]
    lines = [
        "# 国产适配兼容性报告", "",
        f"- 生成时间：{report['generated_at']}",
        f"- 平台：`{p['system']} {p['release']}` / `{p['arch']}` / Python `{p['python_version']}`"
        f" / 发行版 `{p['distro_name'] or '未识别'}` / 桌面 `{p['desktop'] or '未识别'}`",
        f"- 目标机真实验证：**{'是' if report['real_target_verified'] else '否'}**"
        f"（模拟平台={p['simulated']}）",
        f"- 交叉编译已执行：{'是' if report['cross_compile_executed'] else '否'}"
        f"；ARM 推理已验证：{'是' if report['arm_inference_verified'] else '否'}",
        f"- 汇总：PASS {s[PASS]} / WARN {s[WARN]} / FAIL {s[FAIL]} / **SKIP {s[SKIP]}**",
        "", "> " + report["disclaimer"], "",
        "## 逐项结果", "",
        "| 项 | 状态 | 说明 |", "|---|---|---|",
    ]
    for c in report["checks"]:
        detail = c["detail"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {c['name']} | {c['status'].upper()} | {detail} |")
    lines += ["", "## 交叉工具链", "",
              f"- 检测到：{', '.join(report['toolchains']['tools']) or '无'}",
              f"- 缺失：{', '.join(report['toolchains']['missing'])}",
              f"- GNU 交叉工具链可用：{report['toolchains']['gnu_cross_toolchain']}"
              f"；本机能交叉编译：{report['toolchains']['can_cross_compile_here']}",
              "", "## 边界", "",
              "- WPS 版式一致性仍需人工在图形界面打开公文目视核对，命令行转换不能替代；",
              "- `py3-none-any` wheel 的 aarch64 可用性不等于依赖链可安装，需目标机完整 `pip install` 复验；",
              "- 本报告不改动 20 条评测集的 grading / requires / 阈值，不计入任何验收成绩。", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="麒麟/UOS/WPS Linux 兼容性探针")
    parser.add_argument("--out", default="", help="报告输出目录（默认 adapt/reports）")
    args = parser.parse_args(argv)
    out = Path(args.out) if args.out else DEFAULT_REPORT_DIR
    out.mkdir(parents=True, exist_ok=True)
    report = run_checks(out_dir=out)
    (out / "compat_baseline.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "compat_baseline.md").write_text(render_markdown(report), encoding="utf-8")
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
