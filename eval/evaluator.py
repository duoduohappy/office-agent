"""评分器 + 执行器（方案 8.1/8.2）。

评分全部是确定性规则，不用 LLM-as-judge：端侧只有 7B，让被测模型给自己打分
既不可信也不可复现。检查分三层：
  1. 最终回答（关键字 / 最短长度）
  2. 产出文件（存在、体积、内容关键字）
  3. 文档格式（GB/T 9704-2012 版式、待办 Excel 表头）

评测器显式区分「模型跑砸了」和「能力没实现」：后者计入完成率分母但单独归因，
不允许靠跳过难题把完成率抬高。
"""

from __future__ import annotations

import json
import logging
import platform
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import PROJECT_ROOT, config
from core.harness import AgentLoop
from core.prompt_builder import build_system_prompt
from core.tool_registry import ToolRegistry
from memory.structured_db import StructuredMemory

from eval.fixtures import ensure_fixtures
from eval.metrics import (
    InstrumentedLLM,
    MemorySampler,
    RecordingRegistry,
    TaskRecord,
    aggregate,
    probe_ttft_ms,
)

logger = logging.getLogger(__name__)

BENCHMARK_PATH = Path(__file__).with_name("benchmark_tasks.json")
RESULTS_DIR = Path(__file__).with_name("results")

MOCK_MARKERS = ("fake", "scripted", "mock", "stub", "dummy")


# ---------------------------------------------------------------- 格式检查
def _east_asia(run) -> str:
    try:
        rpr = run._element.rPr
        if rpr is None or rpr.rFonts is None:
            return ""
        return rpr.rFonts.get(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}eastAsia"
        ) or ""
    except (AttributeError, TypeError):
        return ""


def check_gb9704(path: Path) -> tuple[bool, list[dict[str, Any]]]:
    """对照 tools/doc_tools.py 生成的公文版式逐项读回校验。"""
    from docx import Document

    doc = Document(str(path))
    s = doc.sections[0]
    margins = [getattr(s, k).cm for k in ("top_margin", "bottom_margin", "left_margin", "right_margin")]
    expected_margins = [3.7, 3.5, 2.8, 2.6]
    margin_ok = all(abs(a - b) < 0.06 for a, b in zip(margins, expected_margins))

    paras = [p for p in doc.paragraphs if p.text.strip()]
    if not paras:
        return False, [{"rule": "文档非空", "ok": False}]

    title_run = paras[0].runs[0] if paras[0].runs else None
    title_size = title_run.font.size.pt if (title_run and title_run.font.size) else 0
    title_ok = abs(title_size - 22) < 0.5 and "小标宋" in _east_asia(title_run)

    body_runs = [
        r for p in paras[1:] for r in p.runs
        if r.font.size and abs(r.font.size.pt - 16) < 0.5
    ]
    body_font_ok = any("仿宋" in _east_asia(r) for r in body_runs)
    spacing_ok = any(
        p.paragraph_format.line_spacing is not None
        and abs(float(p.paragraph_format.line_spacing.pt) - 28) < 0.6
        for p in paras[1:]
        if not isinstance(p.paragraph_format.line_spacing, float)
    )
    section_ok = any("会议主要内容" in p.text for p in paras)

    checks = [
        {"rule": "页边距 3.7/3.5/2.8/2.6cm", "ok": margin_ok, "detail": str([round(m, 2) for m in margins])},
        {"rule": "标题二号(22pt)小标宋", "ok": title_ok, "detail": f"{title_size}pt / {_east_asia(title_run) if title_run else ''}"},
        {"rule": "正文三号(16pt)仿宋", "ok": body_font_ok, "detail": f"{len(body_runs)} 个 16pt run"},
        {"rule": "正文行距 28 磅", "ok": spacing_ok},
        {"rule": "含「一、会议主要内容」章节", "ok": section_ok},
    ]
    return all(c["ok"] for c in checks), checks


def check_xlsx_headers(path: Path) -> tuple[bool, list[dict[str, Any]]]:
    from openpyxl import load_workbook

    ws = load_workbook(str(path)).active
    headers = [c.value for c in ws[1]]
    expected = ["序号", "责任人", "事项", "完成时限", "状态"]
    header_ok = headers[: len(expected)] == expected
    rows_ok = ws.max_row >= 2
    checks = [
        {"rule": "表头符合待办清单模板", "ok": header_ok, "detail": str(headers)},
        {"rule": "至少 1 行数据", "ok": rows_ok, "detail": f"max_row={ws.max_row}"},
    ]
    return all(c["ok"] for c in checks), checks


def artifact_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        from docx import Document

        return "\n".join(p.text for p in Document(str(path)).paragraphs)
    if suffix == ".xlsx":
        from openpyxl import load_workbook

        ws = load_workbook(str(path)).active
        return "\n".join(" ".join(str(c.value or "") for c in row) for row in ws.iter_rows())
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
        except Exception:  # noqa: BLE001
            return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _check_keywords(text: str, words: list[str], mode: str) -> tuple[bool, str]:
    if not words:
        return True, "无关键字要求"
    hits = [w for w in words if w in text]
    ok = bool(hits) if mode == "any" else len(hits) == len(words)
    return ok, f"命中 {hits}/{words}（mode={mode}）"


# ---------------------------------------------------------------- 评分
def grade_task(task: dict[str, Any], record: TaskRecord, out_dir: Path) -> TaskRecord:
    grading = task.get("grading", {})
    checks: list[dict[str, Any]] = []

    keywords = grading.get("answer_keywords") or []
    min_chars = int(grading.get("min_answer_chars") or 0)
    if keywords:
        ok, detail = _check_keywords(record.answer, keywords, grading.get("answer_keywords_mode", "any"))
        checks.append({"rule": "回答关键字", "ok": ok, "detail": detail})
    if min_chars:
        ok = len(record.answer.strip()) >= min_chars
        checks.append({"rule": f"回答长度 ≥{min_chars}", "ok": ok, "detail": f"实际 {len(record.answer.strip())}"})

    files = sorted(p for p in out_dir.rglob("*") if p.is_file()) if out_dir.exists() else []
    record.artifacts = [str(p.relative_to(PROJECT_ROOT)).replace("\\", "/") for p in files]

    for spec in grading.get("artifacts") or []:
        suffix = spec["suffix"]
        candidates = [p for p in files if p.suffix.lower() == suffix]
        label = f"产出 {suffix}"
        if not candidates:
            checks.append({"rule": label + " 存在", "ok": False, "detail": "未找到文件"})
            if spec.get("format"):
                record.format_total += 1
                checks.append({"rule": f"{suffix} 格式检查", "ok": False, "detail": "文件不存在"})
            continue
        checks.append({"rule": label + " 存在", "ok": True, "detail": f"{len(candidates)} 个"})

        # 多个同后缀文件时，任一满足全部检查即通过
        best: tuple[int, list[dict[str, Any]]] | None = None
        for path in candidates:
            sub: list[dict[str, Any]] = []
            size_ok = path.stat().st_size >= int(spec.get("min_bytes") or 0)
            sub.append({"rule": "文件体积", "ok": size_ok, "detail": f"{path.stat().st_size} bytes"})
            text = artifact_text(path)
            words = spec.get("keywords") or []
            kw_ok, kw_detail = _check_keywords(text, words, spec.get("keywords_mode", "all"))
            if words:
                sub.append({"rule": "文件内容关键字", "ok": kw_ok, "detail": kw_detail})
            fmt_checks: list[dict[str, Any]] = []
            if spec.get("format") == "gb9704" and suffix == ".docx":
                _, fmt_checks = check_gb9704(path)
            elif spec.get("format") == "xlsx_headers" and suffix == ".xlsx":
                _, fmt_checks = check_xlsx_headers(path)
            score = sum(1 for c in sub + fmt_checks if c["ok"])
            if best is None or score > best[0]:
                best = (score, sub + fmt_checks)
        if best:
            checks.extend(best[1])
            if spec.get("format"):
                record.format_total += 1
                # 格式子项全部通过才算这份文档格式合格
                fmt_checks = [c for c in best[1] if c["rule"] not in {"文件体积", "文件内容关键字"}]
                fmt_ok = bool(fmt_checks) and all(c["ok"] for c in fmt_checks)
                record.format_ok += 1 if fmt_ok else 0

    record.checks = checks
    record.completed = bool(checks) and all(c["ok"] for c in checks)
    return record


# ---------------------------------------------------------------- 执行器
class EvalRunner:
    def __init__(
        self,
        llm: Any,
        *,
        benchmark_path: Path = BENCHMARK_PATH,
        out_root: Path | None = None,
        task_ids: list[str] | None = None,
        include_unsupported: bool = False,
        max_steps: int = 12,
        probe_ttft: bool = True,
        allow_mock: bool = False,
    ) -> None:
        self.raw_llm = llm
        self.llm = InstrumentedLLM(llm)
        self.benchmark = json.loads(Path(benchmark_path).read_text(encoding="utf-8"))
        self.task_ids = set(task_ids) if task_ids else None
        self.include_unsupported = include_unsupported
        self.max_steps = max_steps
        self.probe_ttft = probe_ttft
        self.allow_mock = allow_mock
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_id = f"eval_{stamp}"
        self.out_root = out_root or (PROJECT_ROOT / "data" / "outbox" / self.run_id)
        try:
            self.out_root.relative_to(PROJECT_ROOT)
        except ValueError as exc:
            raise ValueError("评测产出目录必须位于项目根内，否则文件工具的沙箱会拒绝写入") from exc
        self.out_root.mkdir(parents=True, exist_ok=True)
        self.results_dir = RESULTS_DIR
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.fixtures = ensure_fixtures()
        self.is_mock = any(m in type(llm).__name__.lower() for m in MOCK_MARKERS)

    # -------------------------------------------------- 单条任务
    def _expand(self, text: str, out_dir: Path) -> str:
        for name, path in self.fixtures.items():
            text = text.replace(f"{{fixture:{name}}}", str(path))
        return text.replace("{outdir}", out_dir.relative_to(PROJECT_ROOT).as_posix())

    def _seed_contacts(self, mem: StructuredMemory) -> None:
        if mem.all_contacts():
            return
        for name, email, dept in [
            ("张伟", "zhangwei@example.gov.cn", "综合办"),
            ("李娜", "lina@example.gov.cn", "综合办"),
            ("王强", "wangqiang@example.gov.cn", "技术科"),
            ("赵敏", "zhaomin@example.gov.cn", "技术科"),
        ]:
            mem.add_contact(name, email, dept)

    def _run_one(self, task: dict[str, Any]) -> TaskRecord:
        rec = TaskRecord(
            task_id=task["id"], difficulty=task["difficulty"],
            expected_tools=list(task.get("expected_tools") or []),
        )
        requires = list(task.get("requires") or [])
        if requires and not self.include_unsupported:
            rec.status = "skipped"
            rec.reason = "依赖未实现能力：" + "、".join(requires)
            return rec

        out_dir = self.out_root / task["id"]
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        instruction = self._expand(task["instruction"], out_dir)
        route_args = {k: self._expand(str(v), out_dir) for k, v in (task.get("route_args") or {}).items()}

        base_registry = ToolRegistry()
        from tools import audio_tools, doc_tools, file_tools

        base_registry.extend(file_tools.FILE_TOOLS)
        base_registry.extend(doc_tools.DOC_TOOLS)
        base_registry.extend(audio_tools.AUDIO_TOOLS)
        registry = RecordingRegistry(base_registry)
        mem = StructuredMemory(PROJECT_ROOT / "data" / "workspace" / "eval.sqlite3")
        self._seed_contacts(mem)
        self.llm.reset()

        t0 = time.perf_counter()
        try:
            if task["route"] == "pipeline":
                from agents.orchestrator import Orchestrator

                orch = Orchestrator(self.llm, registry, mem)
                result = orch.run_meeting_pipeline(
                    route_args.get("transcript", ""),
                    title=route_args.get("title", "部门例会纪要"),
                    recipients_group=route_args.get("recipients_group", "全体成员"),
                    out_dir=out_dir.relative_to(PROJECT_ROOT).as_posix(),
                    use_plan=True,
                )
                rec.answer = result.summary()
                if result.errors:
                    rec.reason = "；".join(result.errors)[:300]
                if result.blocked_by_reflection:
                    rec.reason = (rec.reason + " | Reflection 拦截").strip(" |")
            else:
                loop = AgentLoop(
                    self.llm, registry, max_steps=self.max_steps,
                    system_prompt=build_system_prompt("GeneralAgent", registry),
                )
                result = loop.run(instruction)
                rec.answer = result.answer
                if result.stop_reason != "final":
                    rec.reason = f"stop_reason={result.stop_reason}"
                failed = [s.tool_name for s in result.failed_steps]
                if failed:
                    rec.reason = (rec.reason + f" | 工具失败：{','.join(failed)}").strip(" |")
        except Exception as exc:  # noqa: BLE001 - 单条任务异常不能打断整轮评测
            logger.exception("任务 %s 执行异常", task["id"])
            rec.status = "error"
            rec.reason = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            rec.elapsed_s = time.perf_counter() - t0
            rec.llm_calls = self.llm.calls
            rec.tool_calls = registry.tool_names
            rec.tool_ok = [c["ok"] for c in registry.calls]
            mem.close()

        rec = grade_task(task, rec, out_dir)
        if rec.status != "error":
            rec.status = "completed" if rec.completed else "failed"
            if not rec.completed and not rec.reason:
                failed_rules = [c["rule"] for c in rec.checks if not c["ok"]]
                rec.reason = "未通过：" + "、".join(failed_rules[:4])
        return rec

    # -------------------------------------------------- 整轮
    def run(self) -> dict[str, Any]:
        tasks = self.benchmark["tasks"]
        if self.task_ids:
            tasks = [t for t in tasks if t["id"] in self.task_ids]

        if self.is_mock and not self.allow_mock:
            raise RuntimeError(
                "检测到脚本化/模拟 LLM。评测指标必须由真实模型产生；"
                "若只是验证评测器逻辑，请显式传 allow_mock=True（报告会标注为非实测）。"
            )

        ttft = {"value_s": None, "status": "unavailable", "note": "探测未执行"}
        if self.probe_ttft and not self.is_mock:
            ttft = probe_ttft_ms(
                getattr(self.raw_llm, "host", config.ollama_host),
                getattr(self.raw_llm, "model", config.model),
            )

        sampler = MemorySampler()
        sampler.start()
        records: list[TaskRecord] = []
        try:
            for idx, task in enumerate(tasks, 1):
                print(f"[{idx:2d}/{len(tasks)}] {task['id']} ({task['difficulty']}) ... ", end="", flush=True)
                rec = self._run_one(task)
                records.append(rec)
                mark = "PASS" if rec.completed else rec.status.upper()
                print(f"{mark}  {rec.elapsed_s:.1f}s  steps={rec.llm_calls}  tools={len(rec.tool_calls)}")
        finally:
            memory = sampler.stop()

        summary = aggregate(records, self.benchmark, ttft, memory, task_scope=len(tasks))
        report = {
            "run_id": self.run_id,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "is_real_measurement": not self.is_mock,
            "model": getattr(self.raw_llm, "model", "unknown"),
            "host": getattr(self.raw_llm, "host", config.ollama_host),
            "platform": f"{platform.system()} {platform.release()} / Python {sys.version.split()[0]}",
            "include_unsupported": self.include_unsupported,
            "task_count": len(tasks),
            "is_full_benchmark": summary["is_full_benchmark"],
            "out_root": str(self.out_root.relative_to(PROJECT_ROOT)).replace("\\", "/"),
            "records": [r.to_dict() for r in records],
            "ttft": ttft,
            "memory": memory,
            "summary": summary,
        }
        self._write(report)
        return report

    def _write(self, report: dict[str, Any]) -> None:
        json_path = self.results_dir / f"{self.run_id}.json"
        md_path = self.results_dir / f"{self.run_id}.md"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(render_markdown(report), encoding="utf-8")
        print(f"\n结果：{json_path}\n报告：{md_path}")


# ---------------------------------------------------------------- 报告
def render_markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        f"# 评测报告 {report['run_id']}",
        "",
        f"- 时间：{report['timestamp']}",
        f"- 模型：`{report['model']}` @ {report['host']}",
        f"- 平台：{report['platform']}",
        f"- 任务数：{report['task_count']}"
        f"（{'全量 20 条' if report.get('is_full_benchmark') else '子集运行，不构成验收成绩'}）",
        f"- 产出目录：`{report['out_root']}`",
        "",
    ]
    if not report["is_real_measurement"]:
        lines += [
            "> ⚠️ **本报告由脚本化 LLM 产生，仅用于验证评测器逻辑，不构成方案 8.2 的指标实测。**",
            "",
        ]
    lines += [
        "## 七项指标（方案 8.2）",
        "",
        "| 指标 | 实测 | 目标 | 判定 | 口径 / 备注 |",
        "|---|---|---|---|---|",
    ]
    mock_run = not report["is_real_measurement"]
    for m in s["metrics"]:
        if m["value"] is None:
            value = "不可测"
        elif m["unit"] == "ratio":
            value = f"{m['value']:.1%}"
        else:
            value = f"{m['value']}{m['unit']}"
        sign = ">=" if m["op"] == ">=" else "<="
        target = f"{sign}{m['target']:.0%}" if m["unit"] == "ratio" else f"{sign}{m['target']}{m['unit']}"
        # 脚本化 LLM 的"达标"没有任何意义，一律不判定，避免被截取截图当成成绩
        mark = "⬜" if mock_run else {"pass": "✅", "fail": "❌", "unavailable": "⬜"}[m["status"]]
        lines.append(f"| {m['name']} | {value} | {target} | {mark} | {m['note']} |")
    simple_display = f"{s['simple_avg_elapsed_s']}s" if s["simple_avg_elapsed_s"] is not None else "—"
    verdict = "（非实测，达标判定不成立）" if mock_run else ""
    lines += [
        "",
        f"**达标 {s['passed']}/{s['total']} 项，其中已测 {s['measured']} 项{verdict}**；"
        f"任务状态分布：{s['status_counts']}；简单任务平均耗时 {simple_display}"
        f"（目标 ≤{s['simple_target_s']}s）。",
        "",
        "口径说明：",
    ]
    lines += [f"- {n}" for n in s["notes"]]
    lines += [
        "",
        "## 逐条任务",
        "",
        "| ID | 难度 | 状态 | 耗时 | 循环步 | 工具调用 | 说明 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in report["records"]:
        tools = " → ".join(r["tool_calls"]) or "—"
        reason = (r["reason"] or "—").replace("|", "/")[:90]
        lines.append(
            f"| {r['task_id']} | {r['difficulty']} | {'✅' if r['completed'] else '❌'} {r['status']} "
            f"| {r['elapsed_s']}s | {r['llm_calls']} | {tools} | {reason} |"
        )
    skipped = [r for r in report["records"] if r["status"] == "skipped"]
    if skipped:
        lines += ["", "## 能力缺口归因", "", "以下任务依赖尚未实现的能力，计入未完成：", ""]
        for r in skipped:
            lines.append(f"- **{r['task_id']}**：{r['reason']}")
        lines += [
            "",
            f"> 当前能力上限：最多 {report['task_count'] - len(skipped)}/{report['task_count']} 条可完成"
            f"（{(report['task_count'] - len(skipped)) / report['task_count']:.0%}）。"
            "若该上限低于 70% 目标，补齐能力之前无论怎么调优模型都无法达标。",
        ]
    lines += ["", "## 环境备注", "", f"- 首 Token 探测：{report['ttft'].get('note', '')}",
              f"- 内存采样：{report['memory'].get('note', '')}", ""]
    return "\n".join(lines)
