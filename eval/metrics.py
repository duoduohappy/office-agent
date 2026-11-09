"""方案 8.2 七项指标的采集与聚合。

设计原则（评测最怕自欺）：
1. **首 Token 延迟必须真测**。产品链路用 stream=False，整段响应时间不是 TTFT；
   这里单独发一次 stream=True 请求，逐块读到第一个非空 content 才计时结束。
2. **内存峰值取运行时最大 RSS**，采 Ollama 服务进程 + 本 Python 进程；
   系统不支持时返回 None 并标记 unavailable，绝不填猜测值。
3. **跳过 / 失败 / 不可测分开记账**，不把缺失数据当 0 分也不当满分。
4. 所有采集只依赖标准库，评测器本身不引入额外依赖。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- LLM 埋点
class InstrumentedLLM:
    """代理任意 llm 对象，记录 chat 调用次数与单次耗时（等于 Agent 循环步数）。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0
        self.latencies_ms: list[float] = []
        self.reset()

    def reset(self) -> None:
        self.calls = 0
        self.latencies_ms = []

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return self._inner.chat(messages, **kwargs)
        finally:
            self.calls += 1
            self.latencies_ms.append((time.perf_counter() - t0) * 1000)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class RecordingRegistry:
    """代理 ToolRegistry，记录实际工具调用序列。

    通用任务走 AgentLoop、会议闭环走 Orchestrator，两者最终都经过
    registry.dispatch —— 在这一层埋点可覆盖全部路径，无需改产品代码。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.calls = []

    def dispatch(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        t0 = time.perf_counter()
        result = self._inner.dispatch(name, arguments)
        self.calls.append(
            {
                "name": name,
                "ok": bool(result.ok),
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            }
        )
        return result

    @property
    def tool_names(self) -> list[str]:
        return [c["name"] for c in self.calls]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ---------------------------------------------------------------- 首 Token
def probe_ttft_ms(host: str, model: str, timeout: float = 60.0, repeats: int = 3) -> dict[str, Any]:
    """用 stream=True 真测首 Token 延迟，返回中位数、各次样本与状态。

    Ollama 的 /api/chat 流式响应是逐行 NDJSON；第一行往往 content 为空，
    必须找到第一个非空 content 才算首 Token，否则测的是连接建立时间。
    """
    samples: list[float] = []
    error = ""
    for _ in range(repeats):
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "请用一句话说明你的身份。"}],
            "stream": True,
            "options": {"num_predict": 24, "temperature": 0.0},
        }
        req = urllib.request.Request(
            f"{host.rstrip('/')}/api/chat",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                for raw in resp:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    content = ((chunk.get("message") or {}).get("content") or "").strip()
                    if content:
                        samples.append((time.perf_counter() - t0) * 1000)
                        break
                    if chunk.get("done"):
                        break
        except Exception as exc:  # noqa: BLE001 - 探测失败要如实记录，不影响评测主流程
            error = f"{type(exc).__name__}: {exc}"
            break

    if not samples:
        return {"value_s": None, "samples_ms": [], "status": "unavailable", "note": error or "未收到非空 token"}
    samples.sort()
    return {
        "value_s": round(samples[len(samples) // 2] / 1000, 3),
        "samples_ms": [round(s, 1) for s in samples],
        "status": "measured",
        "note": f"stream=True 探测 {len(samples)}/{repeats} 次，取中位数",
    }


# ---------------------------------------------------------------- 内存采样
def _windows_process_rss(names: set[str]) -> float:
    """兼容旧断言的入口；实际采样逻辑在 adapt.hal.MemoryProbe（阶段 3 解耦）。

    传入本模块的 subprocess.run，使既有 mock 断言仍能验证 tasklist 解析逻辑。
    """
    from adapt.hal import get_hal

    hal = get_hal()
    if not hal.info.is_windows:
        return 0.0
    return hal.memory.windows_process_rss(names, runner=subprocess.run)


def _self_rss_mb() -> float:
    """当前进程 RSS（MB）；Windows/Linux 分支由 HAL 决定。"""
    from adapt.hal import get_hal

    return get_hal().memory.self_rss_mb()


def _linux_ollama_rss_mb() -> float:
    """推理进程 RSS（MB）。Linux 走 /proc，Windows 走 tasklist。"""
    from adapt.hal import get_hal

    hal = get_hal()
    return 0.0 if hal.info.is_windows else hal.memory._linux_process_rss(set())


def sample_gpu_vram_mb() -> tuple[float | None, str]:
    """采样 NVIDIA 显存占用（MB）。无 GPU / 无驱动返回 (None, 原因)。

    Ollama 在有独显的机器上会做 CPU/GPU 混合 offload，仅统计系统 RSS
    会低估真实占用。显存单独记录、不混入「内存峰值」指标，但会在报告
    note 中写明，避免把混合推理的结果当成纯 CPU 口径的成绩。
    """
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0:
            return None, f"nvidia-smi 退出码 {proc.returncode}"
        values = []
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                try:
                    values.append(float(line))
                except ValueError:
                    continue
        if not values:
            return None, "nvidia-smi 未返回可读数值"
        return sum(values), f"nvidia-smi，{len(values)} 张卡"
    except FileNotFoundError:
        return None, "无 NVIDIA 驱动 / nvidia-smi"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def sample_runtime_rss_mb() -> tuple[float | None, str, float]:
    """返回 (运行时总 RSS MB, 数据来源, Ollama 进程 RSS MB)。拿不到总量返回 None。"""
    try:
        from adapt.hal import get_hal

        hal = get_hal()
        self_mb = _self_rss_mb()
        ollama_mb, _source = hal.memory.process_rss_mb(hal.memory.OLLAMA_NAMES)
        # note 文案保持与两轮真实评测报告一致，改动不得让历史数字不可复现。
        if hal.info.is_windows:
            return self_mb + ollama_mb, "tasklist(ollama*) + GetProcessMemoryInfo(python)", ollama_mb
        return self_mb + ollama_mb, "/proc/*/status VmRSS(ollama*) + /proc/self/statm", ollama_mb
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}", 0.0


class MemorySampler:
    """后台低频采样运行时 RSS 峰值。间隔 1s，避免 tasklist 频繁启动拖慢评测。"""

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self.peak_mb: float | None = None
        self.max_ollama_mb = 0.0
        self.gpu_peak_mb: float | None = None
        self.gpu_note = "未采样"
        self.source = "not_sampled"
        self.samples = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample_gpu(self) -> None:
        gpu_mb, gpu_note = sample_gpu_vram_mb()
        if gpu_mb is not None:
            self.gpu_peak_mb = gpu_mb if self.gpu_peak_mb is None else max(self.gpu_peak_mb, gpu_mb)
        self.gpu_note = gpu_note

    def _poll(self) -> None:
        while not self._stop.is_set():
            value, source, ollama_mb = sample_runtime_rss_mb()
            if value is not None:
                self.peak_mb = value if self.peak_mb is None else max(self.peak_mb, value)
                self.max_ollama_mb = max(self.max_ollama_mb, ollama_mb)
                self.source = source
                self.samples += 1
            else:
                self.source = source
            self._sample_gpu()
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll, daemon=True, name="mem-sampler")
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        # 收尾再采一次，避免短任务整段没被采样覆盖
        value, source, ollama_mb = sample_runtime_rss_mb()
        if value is not None:
            self.peak_mb = value if self.peak_mb is None else max(self.peak_mb, value)
            self.max_ollama_mb = max(self.max_ollama_mb, ollama_mb)
            self.source = source
            self.samples += 1
        self._sample_gpu()
        gpu_field = round(self.gpu_peak_mb / 1024, 3) if self.gpu_peak_mb is not None else None
        gpu_note = (f"GPU 显存峰值 {self.gpu_peak_mb:.0f}MB 未计入 RSS 指标"
                    f"（{self.gpu_note}）；混合推理口径需在纯 CPU 环境复测"
                    if self.gpu_peak_mb is not None else f"GPU 显存：{self.gpu_note}")
        if self.peak_mb is None:
            return {"value_gb": None, "status": "unavailable", "note": self.source,
                    "gpu_vram_peak_gb": gpu_field, "gpu_note": gpu_note}
        if self.max_ollama_mb < 300:
            # 只采到 serve 进程（~60MB）或仅 Python（~20MB）就报「0.1GB 达标」
            # 是自欺：加载权重的 llama-server 根本没被计入。
            return {
                "value_gb": None, "status": "unavailable",
                "note": f"未检测到推理进程 RSS（{self.samples} 次采样，Ollama 系列峰值仅 "
                        f"{self.max_ollama_mb:.0f}MB）；该值不能代表推理时内存峰值，故不计为实测",
                "gpu_vram_peak_gb": gpu_field, "gpu_note": gpu_note,
            }
        return {
            "value_gb": round(self.peak_mb / 1024, 3),
            "status": "measured",
            "note": f"{self.samples} 次采样；Ollama 系列进程峰值 {self.max_ollama_mb:.0f}MB；"
                    f"{self.source}；{gpu_note}",
            "gpu_vram_peak_gb": gpu_field,
            "gpu_note": gpu_note,
        }


# ---------------------------------------------------------------- 单条记录
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ROOT_FORMS = tuple(dict.fromkeys((str(_PROJECT_ROOT), _PROJECT_ROOT.as_posix())))


def scrub_local_paths(text: str) -> str:
    """评测报告可能被公开：把项目根的绝对路径改写为项目内相对路径。

    只作用于序列化输出；record.answer 原文仍保持完整，供关键字评分使用。
    """
    if not text:
        return text
    for root in _ROOT_FORMS:
        for sep in ("\\", "/"):
            text = text.replace(root + sep, "")
        text = text.replace(root, "")  # 末尾或独立出现的根路径
    return text


@dataclass
class TaskRecord:
    task_id: str
    difficulty: str
    status: str = "pending"  # completed | failed | skipped | error
    reason: str = ""
    elapsed_s: float = 0.0
    llm_calls: int = 0
    tool_calls: list[str] = field(default_factory=list)
    tool_ok: list[bool] = field(default_factory=list)
    expected_tools: list[str] = field(default_factory=list)
    answer: str = ""
    artifacts: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    format_total: int = 0
    format_ok: int = 0
    completed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "difficulty": self.difficulty,
            "status": self.status, "reason": scrub_local_paths(self.reason),
            "completed": self.completed, "elapsed_s": round(self.elapsed_s, 2),
            "llm_calls": self.llm_calls, "tool_calls": self.tool_calls,
            "expected_tools": self.expected_tools,
            "answer_preview": scrub_local_paths(self.answer[:300]), "artifacts": self.artifacts,
            "checks": self.checks, "format_ok": self.format_ok, "format_total": self.format_total,
        }


# ---------------------------------------------------------------- 工具准确率
def lcs_length(a: list[str], b: list[str]) -> int:
    """最长公共子序列长度：允许实际序列多调/少调工具，避免简单集合比对放水。"""
    dp = [0] * (len(b) + 1)
    for x in a:
        prev = 0
        for j, y in enumerate(b, 1):
            tmp = dp[j]
            dp[j] = prev + 1 if x == y else max(dp[j], dp[j - 1])
            prev = tmp
    return dp[len(b)]


def score_tools(actual: list[str], expected: list[str]) -> dict[str, Any]:
    matched = lcs_length(actual, expected)
    precision = matched / len(actual) if actual else 0.0
    recall = matched / len(expected) if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "matched": matched, "actual": len(actual), "expected": len(expected),
        "precision": round(precision, 4), "recall": round(recall, 4),
        "f1": round(f1, 4), "exact": actual == expected,
    }


# ---------------------------------------------------------------- 指标聚合
def _metric(name: str, value: float | None, op: str, target: float, unit: str, note: str) -> dict[str, Any]:
    if value is None:
        status = "unavailable"
    else:
        status = "pass" if ((value >= target) if op == ">=" else (value <= target)) else "fail"
    return {"name": name, "value": value, "target": target, "unit": unit, "op": op, "status": status, "note": note}


def aggregate(
    records: list[TaskRecord],
    benchmark: dict[str, Any],
    ttft: dict[str, Any],
    memory: dict[str, Any],
    task_scope: int | None = None,
) -> dict[str, Any]:
    """汇总 7 项指标。分母口径固定，见 notes 字段。

    task_scope：本轮实际纳入的任务数。只跑子集时用它做分母，并标记
    is_full_benchmark=False，避免把 1 条任务的结果显示成 1/20 完成率。
    """
    th = benchmark["thresholds"]
    all_tasks = benchmark["tasks"]
    total_tasks = task_scope if task_scope is not None else len(all_tasks)
    is_full = total_tasks == len(all_tasks)
    executed = [r for r in records if r.status in {"completed", "failed", "error"}]
    skipped = [r for r in records if r.status == "skipped"]
    completed = [r for r in records if r.completed]

    # 任务完成率：全量评测分母固定 20 条，跳过的算未完成（不能靠剔除难题抬高成绩）
    completion_rate = len(completed) / total_tasks if total_tasks else 0.0

    # 工具准确率：micro-averaged precision，只统计实际执行过的任务
    tool_scores = [score_tools(r.tool_calls, r.expected_tools) for r in executed if r.expected_tools]
    total_actual = sum(s["actual"] for s in tool_scores)
    total_matched = sum(s["matched"] for s in tool_scores)
    tool_precision = total_matched / total_actual if total_actual else None
    total_expected = sum(s["expected"] for s in tool_scores)
    tool_recall = total_matched / total_expected if total_expected else None

    fmt_total = sum(r.format_total for r in records)
    fmt_ok = sum(r.format_ok for r in records)
    format_rate = fmt_ok / fmt_total if fmt_total else None

    avg_steps = sum(r.llm_calls for r in executed) / len(executed) if executed else None
    avg_elapsed = sum(r.elapsed_s for r in executed) / len(executed) if executed else None
    simple = [r for r in executed if r.difficulty == "simple"]
    simple_elapsed = sum(r.elapsed_s for r in simple) / len(simple) if simple else None

    status_counts: dict[str, int] = {}
    for r in records:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1

    metrics = [
        _metric("任务完成率", round(completion_rate, 4), ">=", th["task_completion_rate"], "ratio",
                f"{len(completed)}/{total_tasks}；跳过 {len(skipped)} 条计入未完成"),
        _metric("工具调用准确率", round(tool_precision, 4) if tool_precision is not None else None,
                ">=", th["tool_accuracy"], "ratio",
                f"LCS 对齐 {total_matched}/{total_actual} 次调用；recall={tool_recall}"),
        _metric("格式正确率", round(format_rate, 4) if format_rate is not None else None,
                ">=", th["format_correct_rate"], "ratio", f"{fmt_ok}/{fmt_total} 个格式检查项"),
        _metric("平均完成步数", round(avg_steps, 2) if avg_steps is not None else None,
                "<=", th["avg_steps_max"], "step", f"统计 {len(executed)} 条已执行任务的 LLM 循环轮数"),
        _metric("平均耗时", round(avg_elapsed, 2) if avg_elapsed is not None else None,
                "<=", th["avg_elapsed_s_max"], "s",
                f"简单任务均值 {simple_elapsed:.2f}s（目标 ≤{th['simple_elapsed_s_max']}s）"
                if simple_elapsed is not None else "本轮无简单任务样本"),
        _metric("首 Token 延迟", ttft.get("value_s"), "<=", th["ttft_s_max"], "s", ttft.get("note", "")),
        _metric("内存峰值", memory.get("value_gb"), "<=", th["peak_rss_gb_max"], "GB", memory.get("note", "")),
    ]
    passed = sum(1 for m in metrics if m["status"] == "pass")
    measured = sum(1 for m in metrics if m["status"] in {"pass", "fail"})
    return {
        "metrics": metrics,
        "passed": passed,
        "measured": measured,
        "total": len(metrics),
        "status_counts": status_counts,
        "executed": len(executed),
        "skipped": len(skipped),
        "completed": len(completed),
        "total_tasks": total_tasks,
        "is_full_benchmark": is_full,
        "simple_avg_elapsed_s": round(simple_elapsed, 2) if simple_elapsed is not None else None,
        "simple_target_s": th["simple_elapsed_s_max"],
        "tool_recall": round(tool_recall, 4) if tool_recall is not None else None,
        "notes": ([
            f"⚠️ 本轮仅纳入 {total_tasks}/{len(all_tasks)} 条任务，属子集运行，完成率不构成验收成绩。"
        ] if not is_full else []) + [
            f"任务完成率分母为 {total_tasks} 条；依赖未实现能力而跳过的任务记为未完成。",
            "工具准确率口径 = LCS 对齐后的 micro precision；recall 另列，防止少调工具刷分。",
            "平均完成步数 = LLM chat 调用轮数（Agent 循环步），不含纯工具内部处理。",
            "跳过的任务不进入工具准确率 / 格式 / 步数 / 耗时的分母，避免摊薄。",
        ],
    }
