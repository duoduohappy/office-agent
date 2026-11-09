"""评测体系（方案第 8 章）。

- benchmark_tasks.json  20 条评测任务（简单 5 / 中等 8 / 复杂 7）
- fixtures.py           可复现输入素材生成
- metrics.py            7 项指标采集与聚合
- evaluator.py          评分器 + 执行器 + 报告生成
- run_eval.py           CLI 入口
"""

from __future__ import annotations
