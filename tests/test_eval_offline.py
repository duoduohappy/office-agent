"""评测体系离线自测：验证评分器/指标聚合/报告逻辑，**不产生方案 8.2 的实测值**。

刻意用 ScriptedLLM 驱动，并在断言里检查报告被标注为"非实测"——
防止把假模型的成绩当成真实指标写进简历或汇报。

运行：.venv/Scripts/python.exe tests/test_eval_offline.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.llm_client import LLMResponse
from core.tool_registry import ToolRegistry
from eval import evaluator as ev
from eval.evaluator import (
    EvalRunner, artifact_text, check_gb9704, check_xlsx_headers, grade_task,
    render_markdown,
)
from eval.fixtures import ensure_fixtures
from eval.metrics import (
    TaskRecord, aggregate, lcs_length, score_tools, scrub_local_paths,
)
from tests.test_pipeline_offline import ScriptedLLM
from tools import doc_tools

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def test_benchmark_schema() -> None:
    bm = json.loads(ev.BENCHMARK_PATH.read_text(encoding="utf-8"))
    tasks = bm["tasks"]
    check("评测集共 20 条", len(tasks) == 20, str(len(tasks)))
    from collections import Counter
    dist = Counter(t["difficulty"] for t in tasks)
    check("难度分布 5/8/7", dist == {"simple": 5, "medium": 8, "complex": 7}, str(dist))
    check("任务 ID 唯一", len({t["id"] for t in tasks}) == 20)
    required_thresholds = {
        "task_completion_rate", "tool_accuracy", "format_correct_rate", "avg_steps_max",
        "avg_elapsed_s_max", "ttft_s_max", "peak_rss_gb_max",
    }
    check("7 项主指标阈值齐全", required_thresholds <= set(bm["thresholds"]),
          str(set(bm["thresholds"])))
    check("简单任务耗时为平均耗时的子约束", "simple_elapsed_s_max" in bm["thresholds"])
    check("每条都有 grading", all("grading" in t for t in tasks))
    check("复杂任务都标注能力依赖", all(t["requires"] for t in tasks if t["difficulty"] == "complex"))
    check("简单/中等任务无能力依赖", all(not t["requires"] for t in tasks if t["difficulty"] != "complex"))
    check("route 合法", all(t["route"] in {"generic", "pipeline"} for t in tasks))
    check("pipeline 任务带 transcript", all(
        t["route_args"].get("transcript") for t in tasks
        if t["route"] == "pipeline" and not t["requires"]
    ))
    reg = ToolRegistry().extend(doc_tools.DOC_TOOLS)
    from tools import audio_tools, file_tools
    reg.extend(file_tools.FILE_TOOLS).extend(audio_tools.AUDIO_TOOLS)
    known = set(reg.names())
    orphan = {
        tool for t in tasks if not t["requires"] for tool in t["expected_tools"] if tool not in known
    }
    check("可执行任务的标准答案工具都已注册", not orphan, str(orphan))
    check("占位符只引用已定义 fixture", all(
        "{fixture:" not in t["instruction"] or any(f"{{fixture:{n}}}" in t["instruction"] for n in
                                                  ("policy_doc", "sales_xlsx", "notice_pdf", "plain_txt",
                                                   "meeting_short", "meeting_long"))
        for t in tasks
    ))


def test_fixtures() -> None:
    files = ensure_fixtures()
    check("6 个素材全部生成", len(files) == 6 and all(p.exists() for p in files.values()))
    check("docx 可读出关键内容", "端侧" in artifact_text(files["policy_doc"]))
    check("xlsx 可读出部门数据", "技术科" in artifact_text(files["sales_xlsx"]))
    pdf_text = artifact_text(files["notice_pdf"])
    check("手写 PDF 能被 pypdf 解析", "intranet" in pdf_text and "Qwen2.5" in pdf_text, pdf_text[:120])
    check("txt 可读", "草稿" in artifact_text(files["plain_txt"]))
    check("长稿能触发 Map-Reduce 分片", len(files["meeting_long"].read_text(encoding="utf-8")) > 1800)


def test_score_tools() -> None:
    check("LCS 完全一致", lcs_length(["a", "b"], ["a", "b"]) == 2)
    check("LCS 顺序敏感", lcs_length(["b", "a"], ["a", "b"]) == 1)
    exact = score_tools(["read_document", "write_file"], ["read_document", "write_file"])
    check("完全一致 precision=1", exact["precision"] == 1.0 and exact["exact"])
    extra = score_tools(["read_document", "list_dir", "write_file"], ["read_document", "write_file"])
    check("多调工具 precision<1 recall=1", extra["precision"] < 1 and extra["recall"] == 1.0, str(extra))
    missed = score_tools(["read_document"], ["read_document", "write_file"])
    check("少调工具 precision=1 recall<1", missed["precision"] == 1 and missed["recall"] == 0.5)
    wrong = score_tools(["delete_file"], ["read_document"])
    check("全错 precision=0", wrong["precision"] == 0.0)
    check("空实际调用不崩溃", score_tools([], ["a"])["precision"] == 0.0)


def test_format_checks() -> None:
    out = ROOT / "tests" / "_tmp_out" / "eval_fmt"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    rel = out.relative_to(ROOT).as_posix()
    reg = ToolRegistry().extend(doc_tools.DOC_TOOLS)
    r1 = reg.dispatch("write_minutes_docx", {
        "path": f"{rel}/纪要.docx", "title": "测试纪要", "summary": "端侧模型跑通，11月30日前功能冻结。",
        "todos": json.dumps([{"who": "李娜", "what": "复核模板", "when": "11月20日前"}], ensure_ascii=False),
    })
    check("生成测试用 docx", r1.ok, str(r1.error))
    ok, sub = check_gb9704(out / "纪要.docx")
    check("GB/T 9704 检查对自家产出判通过", ok, str([c for c in sub if not c["ok"]]))
    check("格式检查含 5 个子项", len(sub) == 5, str(len(sub)))

    from docx import Document
    from docx.shared import Cm
    d = Document(str(out / "纪要.docx"))
    d.sections[0].top_margin = Cm(1.0)
    d.save(str(out / "坏纪要.docx"))
    bad, _ = check_gb9704(out / "坏纪要.docx")
    check("页边距被破坏后判不通过", not bad)

    r2 = reg.dispatch("write_todos_xlsx", {
        "path": f"{rel}/待办.xlsx",
        "todos": json.dumps([{"who": "王强", "what": "单元测试", "when": "11月18日前"}], ensure_ascii=False),
    })
    check("生成测试用 xlsx", r2.ok, str(r2.error))
    xok, _ = check_xlsx_headers(out / "待办.xlsx")
    check("待办 Excel 表头检查通过", xok)
    check("xlsx 内容可抽文本", "王强" in artifact_text(out / "待办.xlsx"))


def test_grade_task() -> None:
    out = ROOT / "tests" / "_tmp_out" / "eval_grade"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    task = {
        "grading": {
            "answer_keywords": ["端侧", "内网"], "answer_keywords_mode": "any", "min_answer_chars": 10,
            "artifacts": [{"suffix": ".txt", "min_bytes": 5, "keywords": ["结论"],
                           "keywords_mode": "all", "format": None}],
        }
    }
    rec = TaskRecord("X", "simple", answer="端侧推理，数据不出内网，符合保密要求")
    grade_task(task, rec, out)
    check("缺产出文件时判未完成", not rec.completed)

    (out / "结论.txt").write_text("结论：端侧推理可行。", encoding="utf-8")
    rec2 = TaskRecord("X", "simple", answer="端侧推理，数据不出内网，符合保密要求")
    grade_task(task, rec2, out)
    check("产出齐全时判完成", rec2.completed, str(rec2.checks))
    check("无格式要求时不计格式分母", rec2.format_total == 0)

    fmt_task = {"grading": {"answer_keywords": [], "min_answer_chars": 0, "artifacts": [
        {"suffix": ".docx", "min_bytes": 100, "keywords": [], "format": "gb9704"}]}}
    rec3 = TaskRecord("Y", "medium", answer="")
    grade_task(fmt_task, rec3, out)
    check("格式项不存在时计入分母且不合格", rec3.format_total == 1 and rec3.format_ok == 0)


def test_aggregate() -> None:
    bm = json.loads(ev.BENCHMARK_PATH.read_text(encoding="utf-8"))
    records = []
    for t in bm["tasks"]:
        if t["requires"]:
            records.append(TaskRecord(t["id"], t["difficulty"], status="skipped", reason="能力缺失"))
        else:
            records.append(TaskRecord(
                t["id"], t["difficulty"], status="completed", completed=True,
                elapsed_s=10.0, llm_calls=4,
                tool_calls=list(t["expected_tools"]),
                expected_tools=list(t["expected_tools"]),
            ))
    ttft = {"value_s": 0.8, "status": "measured", "note": "mock"}
    memory = {"value_gb": 5.0, "status": "measured", "note": "mock"}
    s = aggregate(records, bm, ttft, memory)
    by_name = {m["name"]: m for m in s["metrics"]}
    check("7 项指标齐全", len(s["metrics"]) == 7)
    check("完成率 = 13/20 = 0.65（能力上限）", by_name["任务完成率"]["value"] == 0.65,
          str(by_name["任务完成率"]["value"]))
    check("能力上限低于 70% 目标 → 判定 fail", by_name["任务完成率"]["status"] == "fail")
    check("工具准确率 1.0（标准答案完全命中）", by_name["工具调用准确率"]["value"] == 1.0)
    check("格式不可测时标 unavailable", by_name["格式正确率"]["status"] == "unavailable")
    check("平均步数 = 4", by_name["平均完成步数"]["value"] == 4)
    check("平均耗时 = 10s", by_name["平均耗时"]["value"] == 10)
    check("简单任务均值被记录", s["simple_avg_elapsed_s"] == 10)
    check("跳过任务计入完成率分母", s["skipped"] == 7 and s["completed"] == 13)
    check("分母口径说明写进报告", any("分母为" in n for n in s["notes"]))
    check("全量评测被正确标记", s["is_full_benchmark"] is True and s["total_tasks"] == 20)
    subset = aggregate(records[:1], bm, ttft, memory, task_scope=1)
    subset_values = {m["name"]: m["value"] for m in subset["metrics"]}
    check("子集运行分母按本轮任务数", subset["total_tasks"] == 1
          and subset_values["任务完成率"] == 1.0)
    check("子集运行标注不构成验收成绩", subset["is_full_benchmark"] is False
          and any("子集运行" in n for n in subset["notes"]))
    check("首 Token 与内存可取实测值", by_name["首 Token 延迟"]["status"] == "pass"
          and by_name["内存峰值"]["status"] == "pass")
    unavailable = aggregate(records, bm,
                            {"value_s": None, "status": "unavailable", "note": "无服务"},
                            {"value_gb": None, "status": "unavailable", "note": "不支持"})
    check("不可测指标不误判为达标", sum(1 for m in unavailable["metrics"] if m["status"] == "unavailable") == 3)


def test_memory_sampling() -> None:
    from eval.metrics import MemorySampler, sample_runtime_rss_mb

    total, source, ollama_mb = sample_runtime_rss_mb()
    check("本进程 RSS 能真实读到（非 0）", total is not None and total > 1, f"{total}MB / {source}")
    s = MemorySampler()
    s.start()
    result = s.stop()
    check("内存采样结构完整", "status" in result and "note" in result)
    if ollama_mb <= 0:
        check("无 Ollama 进程时不把 Python 内存冒充推理内存",
              result["status"] == "unavailable" and result["value_gb"] is None, str(result))
    check("采样来源如实记录", "tasklist" in result["note"] or "/proc" in result["note"] or "未检测到" in result["note"])


def test_runner_guards() -> None:
    out = ROOT / "tests" / "_tmp_out" / "eval_runner"
    shutil.rmtree(out, ignore_errors=True)
    runner = EvalRunner(ScriptedLLM(), out_root=out, task_ids=["M07"], probe_ttft=False)
    rejected = False
    try:
        runner.run()
    except RuntimeError as exc:
        rejected = "真实模型" in str(exc)
    check("默认拒绝脚本化 LLM 冒充实测", rejected)

    saved_results = ev.RESULTS_DIR
    tmp_results = ROOT / "tests" / "_tmp_out" / "eval_results"
    shutil.rmtree(tmp_results, ignore_errors=True)
    tmp_results.mkdir(parents=True, exist_ok=True)
    ev.RESULTS_DIR = tmp_results
    try:
        runner2 = EvalRunner(ScriptedLLM(), out_root=out, task_ids=["M07"],
                             probe_ttft=False, allow_mock=True)
        report = runner2.run()
    finally:
        ev.RESULTS_DIR = saved_results
    check("显式 allow_mock 后可跑通单条闭环", len(report["records"]) == 1)
    check("报告标注为非实测", report["is_real_measurement"] is False)
    check("markdown 报告有醒目免责声明", "不构成方案 8.2 的指标实测" in render_markdown(report))
    check("离线报告不给出任何达标判定", "达标判定不成立" in render_markdown(report)
          and "✅ |" not in render_markdown(report))
    check("结果文件已落盘", any(tmp_results.glob("*.json")) and any(tmp_results.glob("*.md")))
    r0 = report["records"][0]
    check("会议闭环产出被记录", len(r0["artifacts"]) >= 2, str(r0["artifacts"]))
    check("工具调用序列被埋点采集", len(r0["tool_calls"]) >= 3, str(r0["tool_calls"]))
    check("循环步数被埋点采集", r0["llm_calls"] >= 3, str(r0["llm_calls"]))
    check("评测报告含完整七项指标", len(report["summary"]["metrics"]) == 7)


def test_report_no_absolute_paths() -> None:
    """报告会被公开，序列化时不得带出本机绝对路径；评分原文不受影响。"""
    root = str(ROOT)
    answer = "产出：" + root + "\\data\\outbox\\a.docx、" + ROOT.as_posix() + "/data/outbox/b.xlsx"
    rec = TaskRecord(task_id="X", difficulty="simple", answer=answer, reason="失败于 " + root)
    d = rec.to_dict()
    check("报告脱敏：answer_preview 无绝对路径", ":\\" not in d["answer_preview"] and "D:/" not in d["answer_preview"], d["answer_preview"])
    check("报告脱敏：reason 无绝对路径", root not in d["reason"], d["reason"])
    check("报告脱敏：相对路径仍可读", "a.docx" in d["answer_preview"] and "outbox" in d["answer_preview"])
    check("报告脱敏：评分原文保持完整", rec.answer == answer and len(rec.answer) > len(d["answer_preview"]))
    check("脱敏函数对非路径文本无副作用", scrub_local_paths("任务完成，共 3 步") == "任务完成，共 3 步")


def main() -> int:
    test_benchmark_schema()
    test_fixtures()
    test_score_tools()
    test_format_checks()
    test_grade_task()
    test_aggregate()
    test_memory_sampling()
    test_runner_guards()
    test_report_no_absolute_paths()
    print(f"\n{'=' * 46}\n通过 {PASS} / 失败 {FAIL} / 共 {PASS + FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
