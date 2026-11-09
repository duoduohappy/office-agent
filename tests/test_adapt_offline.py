"""阶段 3 国产适配离线自测：HAL 分支、跨平台行为与**防伪造**断言。

不依赖 Ollama / ARM 真机 / 麒麟 UOS；模拟平台结果必须被明确标注，
绝不能因为脚本跑通就把「国产适配」写成已验证。

运行：.venv/Scripts/python.exe tests/test_adapt_offline.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapt.arm_build import build_plan, probe_toolchains
from adapt.compat_check import PASS, SKIP, run_checks
from adapt.hal import HAL, MemoryProbe, detect_platform, normalize_arch, parse_os_release

PASS_N = 0
FAIL_N = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS_N, FAIL_N
    if cond:
        PASS_N += 1
    else:
        FAIL_N += 1
        print(f"  FAIL  {name}  {detail}")


KYLIN_OS_RELEASE = 'ID=kylin\nNAME="Kylin Linux Advanced Server"\nVERSION_ID="V10"\n'
UOS_OS_RELEASE = 'ID=uos\nNAME="UnionTech OS Desktop"\nVERSION_ID="20"\n'


def fake_kylin() -> HAL:
    info = detect_platform(
        system="Linux", machine="aarch64", release="5.10.0-kylin",
        os_release_text=KYLIN_OS_RELEASE,
        environ={"XDG_CURRENT_DESKTOP": "UKUI:GNOME"},
    )
    return HAL(info=info, installed_fonts={"方正仿宋_GBK", "方正小标宋_GBK", "Noto Sans CJK SC"})


def test_arch_and_os_release() -> None:
    check("amd64 归一化", normalize_arch("AMD64") == "x86_64")
    check("arm64 归一化", normalize_arch("arm64") == "aarch64")
    check("aarch64 归一化", normalize_arch("aarch64") == "aarch64")
    check("龙芯架构可识别", normalize_arch("loongarch64") == "loongarch64")
    check("申威架构可识别", normalize_arch("sw_64") == "sw_64")
    check("未知架构不吞掉", normalize_arch("FooArch") == "fooarch")
    d = parse_os_release('# comment\nID="kylin"\nNAME="Kylin V10"\nVERSION_ID="V10"\n')
    check("os-release 去引号", d["ID"] == "kylin" and d["NAME"] == "Kylin V10", str(d))
    check("os-release 忽略注释", "# comment" not in d)
    check("空 os-release 返回空", parse_os_release("") == {})


def test_platform_detection() -> None:
    k = fake_kylin().info
    check("模拟麒麟识别为信创 OS", k.is_xinchuang_distro)
    check("模拟麒麟识别为 ARM 目标", k.is_arm_target)
    check("UKUI 桌面识别", k.desktop == "ukui", k.desktop)
    check("模拟平台标记 simulated", k.simulated is True)
    check("模拟平台不算真机", k.is_real_target is False)
    check("发行版版本", k.distro_version == "V10", k.distro_version)

    uos = detect_platform(system="Linux", machine="aarch64",
                          os_release_text=UOS_OS_RELEASE,
                          environ={"XDG_CURRENT_DESKTOP": "DDE"})
    check("统信 UOS 识别", uos.is_xinchuang_distro, uos.distro_id)
    check("DDE 桌面识别", uos.desktop == "dde", uos.desktop)

    real = detect_platform()
    check("真实探测不带模拟标记", real.simulated is False)
    check("真实探测架构已归一化", real.arch in {"x86_64", "aarch64", "loongarch64", "sw_64", "riscv64", "x86"}, real.arch)
    check("本机非信创目标时 is_real_target=False", real.is_real_target == (real.is_linux and real.is_xinchuang_distro and real.is_arm_target))


def test_fonts() -> None:
    k = fake_kylin()
    name, note = k.fonts.resolve("仿宋_GB2312")
    check("Linux 选国产仿宋", name == "方正仿宋_GBK", name)
    check("字体解析说明如实", "fc-list" in note, note)
    k2 = HAL(info=fake_kylin().info, installed_fonts=set())
    name2, note2 = k2.fonts.resolve("仿宋_GB2312")
    check("Linux 缺字体回退逻辑名", "仿宋" in name2, name2)
    check("缺字体不静默降级", "未检测到" in note2, note2)

    w = HAL(info=detect_platform(system="Windows", machine="AMD64"))
    check("Windows 保持仿宋_GB2312", w.fonts.resolve("仿宋_GB2312")[0] == "仿宋_GB2312")
    check("Windows 保持小标宋", w.fonts.resolve("方正小标宋简体")[0] == "方正小标宋简体")
    check("Windows 保持 Times New Roman", w.fonts.resolve("Times New Roman")[0] == "Times New Roman")


def test_memory_probe(tmp: Path) -> None:
    proc = tmp / "proc"
    for pid, cmd, rss in [
        ("123", b"/usr/bin/ollama\x00serve\x00", 512000),
        ("456", b"python\x00main.py\x00", 10000),
        ("789", b"/opt/llama-server\x00-m\x00model.gguf\x00", 1024),
    ]:
        d = proc / pid
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(cmd)
        (d / "status").write_text(f"Name:\ttest\nVmRSS:\t{rss} kB\n", encoding="utf-8")
    k = HAL(info=detect_platform(system="Linux", machine="aarch64",
                                 os_release_text=KYLIN_OS_RELEASE), proc_root=proc)
    total, note = k.memory.process_rss_mb(set())
    check("Linux /proc 只统计推理进程", abs(total - 501.0) < 0.01, f"{total} MB")
    check("Linux 采样来源如实", "/proc" in note, note)
    check("Linux 无 self/statm 不崩溃", k.memory.self_rss_mb() == 0.0)

    fake_out = (b'"ollama.exe","1","Console","1","61,560 K"\r\n'
                b'"llama-server.exe","2","Console","1","1,544,760 K"\r\n'
                b'"python.exe","3","Console","1","20,000 K"\r\n')
    runner = lambda *a, **kw: SimpleNamespace(stdout=fake_out, returncode=0, stderr=b"")
    t = MemoryProbe.windows_process_rss({"ollama.exe"}, runner=runner)
    check("Windows 解析计入 llama-server", 1560 < t < 1580, f"{t} MB")
    t2 = MemoryProbe.windows_process_rss(set(), runner=runner)
    check("Windows 前缀匹配不依赖名单", 1560 < t2 < 1580, f"{t2} MB")


def test_hal_integration() -> None:
    from agents.desktop_agent import DesktopAgent
    from eval.metrics import _self_rss_mb, sample_runtime_rss_mb
    from main import _user_env

    check("本机 _self_rss_mb 可用", _self_rss_mb() >= 0)
    total, note, ollama = sample_runtime_rss_mb()
    check("采样三元组结构", isinstance(total, float) and isinstance(ollama, float))
    check("采样 note 未改变（历史报告可复现）",
          "tasklist" in note or "/proc" in note, note)
    check("GUI 后端三值之一",
          DesktopAgent.ui_backend() in {"windows-uia", "linux-atspi", "unavailable"})
    check("用户环境变量读取不抛异常", isinstance(_user_env("OLLAMA_MODELS"), str))

    from core.tool_registry import ToolRegistry
    from docx.oxml.ns import qn
    from tools import doc_tools
    reg = ToolRegistry().extend(doc_tools.DOC_TOOLS)
    res = reg.dispatch("write_minutes_docx", {
        "path": "data/outbox/_adapt_test/fonts.docx",
        "title": "适配回归", "summary": "中文字体声明回归检查。", "todos": "[]"})
    check("HAL 接入后公文仍可生成", res.ok)
    from docx import Document
    doc = Document(ROOT / "data" / "outbox" / "_adapt_test" / "fonts.docx")
    fonts = [r._element.rPr.rFonts.get(qn("w:eastAsia")) for p in doc.paragraphs
             for r in p.runs if r._element.rPr is not None and r._element.rPr.rFonts is not None]
    check("东方字体声明未丢失", any(f and "仿宋" in f for f in fonts), str(set(fonts)))


def test_arm_audit_report() -> None:
    p = ROOT / "adapt" / "reports" / "aarch64_wheel_audit.json"
    check("aarch64 审计报告存在", p.exists(), str(p))
    if not p.exists():
        return
    d = json.loads(p.read_text(encoding="utf-8"))
    audit = d["wheel_audit"]
    check("核心包全部可获取", audit["all_core_ok"] is True)
    check("可选 Chroma 可获取", audit["optional_ok"] is True)
    check("区分 ARM 原生 wheel", any("chromadb" in x for x in audit["native_aarch64_packages"]),
          str(audit["native_aarch64_packages"]))
    check("区分纯 Python 通用 wheel", len(audit["pure_python_any_packages"]) == 4,
          str(audit["pure_python_any_packages"]))
    check("审计未冒充交叉编译", audit["cross_compile_executed"] is False)
    check("审计未冒充 ARM 实装", audit["real_arm_install_verified"] is False)
    check("scope 说明 --no-deps", "--no-deps" in audit["scope_note"])
    plan = d["build_plan"]
    check("构建计划未冒充执行", plan["cross_compile_executed"] is False)
    check("构建计划未冒充推理验证", plan["arm_inference_verified"] is False)
    check("本项目无自身原生代码", plan["project_native_code"] is False)
    check("Python 下限 3.10", plan["python_min_version"] == "3.10")
    check("步骤均标记未在本机执行", all(s["executed_here"] is False for s in plan["steps"]))
    tc = d["toolchains"]
    check("工具链探测有 missing 字段", isinstance(tc["missing"], list))
    check("工具链结果带来源", tc["source"] == "shutil.which")


def test_compat_report_and_no_fake_pass(tmp: Path) -> None:
    p = ROOT / "adapt" / "reports" / "compat_baseline.json"
    check("兼容性基线报告存在", p.exists(), str(p))
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        check("本机报告 real_target_verified=False", d["real_target_verified"] is False)
        check("本机存在 SKIP 项", d["summary"][SKIP] > 0, str(d["summary"]))
        check("SKIP 语义写入免责声明", "SKIP" in d["disclaimer"] and "不是通过" in d["disclaimer"])
        check("交叉编译未冒充执行", d["cross_compile_executed"] is False)
        check("ARM 推理未冒充验证", d["arm_inference_verified"] is False)
        check("目标机专属项不得在开发机 PASS",
              all(c["status"] != PASS for c in d["checks"] if c["target_only"]),
              str([(c["id"], c["status"]) for c in d["checks"] if c["target_only"]]))
        check("OOXML 回读是真实执行的 PASS",
              any(c["id"] == "ooxml" and c["status"] == PASS for c in d["checks"]))
        check("纯 CPU 基线明确 SKIP",
              any(c["id"] == "cpu_baseline" and c["status"] == SKIP for c in d["checks"]))

    # 模拟麒麟 ARM：可以跑通探针，但目标机专属项一律不得 PASS
    sim = run_checks(fake_kylin(), out_dir=tmp / "sim")
    check("模拟平台标记写入报告", sim["simulated_platform"] is True)
    check("模拟平台 real_target_verified=False", sim["real_target_verified"] is False)
    target_checks = [c for c in sim["checks"] if c["target_only"]]
    check("模拟平台目标机项无 PASS", all(c["status"] != PASS for c in target_checks),
          str([(c["id"], c["status"]) for c in target_checks]))
    check("强制降级有痕迹", any("模拟平台" in c["detail"] for c in target_checks))
    check("模拟平台 OS 项被降级", next(c for c in sim["checks"] if c["id"] == "os")["status"] == SKIP)


def test_docs_and_scripts() -> None:
    sh = ROOT / "scripts" / "cross_build_llamacpp.sh"
    cmake = ROOT / "adapt" / "toolchains" / "aarch64-linux-gnu.cmake"
    doc = ROOT / "docs" / "xinchuang-validation.md"
    req = ROOT / "requirements-aarch64.txt"
    check("交叉编译脚本存在", sh.exists())
    check("CMake toolchain 存在", cmake.exists())
    check("验证清单文档存在", doc.exists())
    check("目标机依赖清单存在", req.exists())
    if sh.exists():
        t = sh.read_text(encoding="utf-8")
        check("脚本声明未在开发机执行", "NOT been executed" in t)
        check("脚本有 --verify", "--verify" in t)
        check("脚本设置 aarch64 编译器", "aarch64-linux-gnu-gcc" in t)
    if cmake.exists():
        t = cmake.read_text(encoding="utf-8")
        check("toolchain 目标系统为 Linux", "CMAKE_SYSTEM_NAME Linux" in t)
        check("toolchain 目标架构 aarch64", "CMAKE_SYSTEM_PROCESSOR aarch64" in t)
    if doc.exists():
        t = doc.read_text(encoding="utf-8")
        check("文档明确未实机验证", "均未执行" in t)
        check("文档含不可说话术", "不能说" in t)
        check("文档标注不改评测", "没有改动" in t and "70%" in t)


def main() -> int:
    import shutil
    import tempfile
    tmp = ROOT / "data" / "outbox" / "_adapt_test"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    test_arch_and_os_release()
    test_platform_detection()
    test_fonts()
    test_memory_probe(tmp / "memory")
    test_hal_integration()
    test_arm_audit_report()
    test_compat_report_and_no_fake_pass(tmp / "compat")
    test_docs_and_scripts()
    print(f"通过 {PASS_N} / 失败 {FAIL_N} / 共 {PASS_N + FAIL_N}")
    return 1 if FAIL_N else 0


if __name__ == "__main__":
    raise SystemExit(main())
