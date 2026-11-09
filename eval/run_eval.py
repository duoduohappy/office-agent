"""评测 CLI 入口。

    # 真实模型全量评测（必须先装好 Ollama + 模型）
    .venv\\Scripts\\python.exe -m eval.run_eval

    # 只跑指定任务 / 连依赖缺失的复杂任务也尝试执行
    .venv\\Scripts\\python.exe -m eval.run_eval --tasks S01,S02,M01
    .venv\\Scripts\\python.exe -m eval.run_eval --include-unsupported

设计上刻意**不允许**用脚本化 LLM 冒充实测：EvalRunner 会检测 mock LLM 并拒绝，
离线自测必须显式 allow_mock，且报告顶部会标注"不构成指标实测"。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import config  # noqa: E402
from core.llm_client import OllamaClient  # noqa: E402
from eval.evaluator import BENCHMARK_PATH, EvalRunner  # noqa: E402


def main() -> int:
    import json

    parser = argparse.ArgumentParser(description="方案 8.2 评测：20 条任务 / 7 项指标")
    parser.add_argument("--model", default=config.model, help="Ollama 模型名")
    parser.add_argument("--host", default=config.ollama_host, help="Ollama 服务地址")
    parser.add_argument("--tasks", default="", help="只跑指定 ID，逗号分隔，如 S01,M01")
    parser.add_argument("--include-unsupported", action="store_true",
                        help="连依赖未实现能力的复杂任务也实际执行（用于看真实失败原因）")
    parser.add_argument("--no-ttft", action="store_true", help="跳过首 Token 探测")
    parser.add_argument("--max-steps", type=int, default=12, help="通用任务最大 Agent 循环步数")
    parser.add_argument("--list", action="store_true", help="只列出评测集任务，不执行")
    args = parser.parse_args()

    benchmark = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    if args.list:
        for t in benchmark["tasks"]:
            req = ("、".join(t["requires"])) or "—"
            print(f"{t['id']:4s} {t['difficulty']:8s} 依赖缺失能力={req:24s} {t['instruction'][:56]}")
        print(f"\n共 {len(benchmark['tasks'])} 条")
        return 0

    client = OllamaClient(model=args.model, host=args.host,
                          temperature=config.temperature, num_ctx=config.num_ctx,
                          timeout=config.request_timeout)
    if not client.is_available():
        print(f"[中止] 连不上 Ollama（{args.host}）。评测必须由真实模型产生，请先启动 ollama serve。")
        print("        只想验证评测器逻辑，请运行：.venv\\Scripts\\python.exe tests\\test_eval_offline.py")
        return 2
    models = client.list_models()
    if not any(m == args.model or m.startswith(args.model + ":") for m in models):
        print(f"[中止] 模型 {args.model} 未拉取。已装：{', '.join(models) or '（无）'}")
        print(f"        ollama pull {args.model}")
        return 2

    config.ensure_dirs()
    ids = [x.strip() for x in args.tasks.split(",") if x.strip()] or None
    runner = EvalRunner(
        client,
        task_ids=ids,
        include_unsupported=args.include_unsupported,
        max_steps=args.max_steps,
        probe_ttft=not args.no_ttft,
    )
    report = runner.run()
    s = report["summary"]
    print(f"\n达标 {s['passed']}/{s['total']} 项（已测 {s['measured']} 项），"
          f"完成任务 {s['completed']}/{report['task_count']}，跳过 {s['skipped']} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
