"""真实模型最小诊断探针（跑正式评测前必做）。

目的不是出成绩，而是回答四个会决定评测能不能跑的问题：
  1. 模型能否加载、冷启动要多久；
  2. 首 Token 延迟（stream=True 真测，非整段响应时间）；
  3. Qwen2.5-7B 对 Ollama 原生 `tools` 参数的支持是否稳定；
  4. 推理时运行时 RSS 峰值能否被真实采到（而非把 Python 的 20MB 报成达标）。

用法：
    .venv\\Scripts\\python.exe scripts\\probe_real_model.py
    .venv\\Scripts\\python.exe scripts\\probe_real_model.py --model qwen2.5:7b-instruct-q4_K_M

结果写入 eval/results/real_model_probe.json（含本机环境信息，已 gitignore）。
任何一项失败都如实记录，不抛穿、不补假数据。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import config  # noqa: E402
from core.llm_client import OllamaClient  # noqa: E402
from eval.metrics import MemorySampler, probe_ttft_ms  # noqa: E402
from main import build_registry  # noqa: E402

RESULTS = ROOT / "eval" / "results"

# 三条由易到难的 function calling 探测：空参数、单参数、带中文的多参数
TOOL_PROBES = [
    {
        "name": "空参数工具",
        "messages": [{"role": "user", "content": "列出当前项目根目录下有哪些文件和目录。"}],
        "expect_tool": "list_dir",
    },
    {
        "name": "单参数工具",
        "messages": [{"role": "user", "content": "读取 data/inbox/sample_meeting.txt 的内容，告诉我前两行写了什么。"}],
        "expect_tool": "read_file",
    },
    {
        "name": "中文多参数工具",
        "messages": [{"role": "user", "content": "把「端侧推理链路连通」这一行文字写入文件 data/outbox/probe_write.txt。"}],
        "expect_tool": "write_file",
    },
]


def probe_tools(client: OllamaClient, schemas: list[dict], registry_names: set[str]) -> list[dict]:
    out: list[dict] = []
    for case in TOOL_PROBES:
        t0 = time.perf_counter()
        row: dict = {"case": case["name"], "expect_tool": case["expect_tool"]}
        try:
            resp = client.chat(case["messages"], tools=schemas, temperature=0.0)
            elapsed = time.perf_counter() - t0
            names = [c.name for c in calls]
            row.update({
                "elapsed_s": round(elapsed, 2),
                "tool_call_count": len(calls),
                "called": names,
                "arguments": [c.arguments for c in calls],
                "content_preview": resp.content[:120],
                "valid_tool_name": all(n in registry_names for n in names) if names else False,
                "hit_expected": case["expect_tool"] in names,
                "ok": case["expect_tool"] in names and all(n in registry_names for n in names),
            })
            if not calls:
                row["diagnosis"] = "模型未返回 tool_calls（退化为纯文本），原生 tools 支持可能失效"
            elif not row["valid_tool_name"]:
                row["diagnosis"] = "模型返回了工具名但不在注册表中（幻觉工具名）"
            elif not row["hit_expected"]:
                row["diagnosis"] = f"工具名合法但选错：期望 {case['expect_tool']}，实际 {names}"
        except Exception as exc:  # noqa: BLE001 - 探针要如实记录失败
            row.update({
                "elapsed_s": round(time.perf_counter() - t0, 2),
                "ok": False, "tool_call_count": 0, "called": [], "arguments": [],
                "content_preview": "", "valid_tool_name": False, "hit_expected": False,
                "diagnosis": f"{type(exc).__name__}: {exc}",
            })
        out.append(row)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="真实模型最小诊断（非评测）")
    parser.add_argument("--model", default=config.model)
    parser.add_argument("--host", default=config.ollama_host)
    parser.add_argument("--no-ttft", action="store_true", help="跳过 TTFT 探测，省一次加载")
    args = parser.parse_args()

    report: dict = {
        "probe": "real_model_minimal_diagnosis",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "model": args.model,
        "host": args.host,
        "note": "本文件是诊断记录，不是方案 8.2 的指标实测报告",
    }

    client = OllamaClient(model=args.model, host=args.host,
                          temperature=config.temperature, num_ctx=config.num_ctx,
                          timeout=600)  # 冷启动加载 7B 可能数分钟
    if not client.is_available():
        print(f"[中止] 连不上 Ollama 服务：{args.host}")
        return 2
    models = client.list_models()
    report["installed_models"] = models
    if not any(m == args.model or m.startswith(args.model + ":") for m in models):
        print(f"[中止] 模型 {args.model} 未就绪。已装：{models}")
        return 2
    print(f"[1/4] 服务与模型 OK：{args.model}")

    registry = build_registry()
    schemas = registry.schemas()
    report["registered_tools"] = registry.names()
    sampler = MemorySampler(interval=1.0)
    sampler.start()

    # 冷启动：一次普通对话，实测加载耗时
    t0 = time.perf_counter()
    try:
        cold = client.chat([{"role": "user", "content": "只回复两个字：就绪"}], temperature=0.0)
        report["cold_start"] = {
            "elapsed_s": round(time.perf_counter() - t0, 2),
            "ok": True, "reply": cold.content[:80],
        }
        print(f"[2/4] 冷启动加载 {report['cold_start']['elapsed_s']}s，回复：{cold.content[:40]!r}")
    except Exception as exc:  # noqa: BLE001
        report["cold_start"] = {"elapsed_s": round(time.perf_counter() - t0, 2),
                               "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(f"[2/4] 冷启动失败：{report['cold_start']['error']}")

    # 真 TTFT
    if args.no_ttft or not report["cold_start"]["ok"]:
        report["ttft"] = {"status": "skipped", "note": "显式跳过或冷启动失败"}
    else:
        report["ttft"] = probe_ttft_ms(args.host, args.model, timeout=300, repeats=3)
    print(f"[3/4] TTFT：{report['ttft'].get('value_s')} s（{report['ttft'].get('status')}）")

    # function calling 稳定性
    if report["cold_start"]["ok"]:
        report["tool_calls"] = probe_tools(client, schemas, set(registry.names()))
        ok_n = sum(1 for r in report["tool_calls"] if r["ok"])
        report["tool_call_summary"] = f"{ok_n}/{len(report['tool_calls'])} 条成功"
        print(f"[4/4] function calling：{report['tool_call_summary']}")
        for r in report["tool_calls"]:
            print(f"      {r['case']:14s} -> {r['called']} "
                  f"{'' if r['ok'] else '| ' + r.get('diagnosis', '')}")
    else:
        report["tool_calls"] = []
        report["tool_call_summary"] = "skipped（冷启动失败）"

    report["memory"] = sampler.stop()
    print(f"      内存峰值：{report['memory'].get('value_gb')} GB（{report['memory'].get('status')}）")
    print(f"      {report['memory'].get('note', '')}")

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / "real_model_probe.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n诊断记录：{out.relative_to(ROOT).as_posix()}")
    return 0 if report["cold_start"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
