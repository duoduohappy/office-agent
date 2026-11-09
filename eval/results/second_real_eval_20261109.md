# 评测报告 eval_20261109_192019

- 时间：2026-11-09T19:27:44
- 模型：`qwen2.5:7b-instruct-q4_K_M` @ http://127.0.0.1:11434
- 平台：Windows 11 / Python 3.13.14
- 任务数：20（全量 20 条）
- 产出目录：`data/outbox/eval_20261109_192019`

## 七项指标（方案 8.2）

| 指标 | 实测 | 目标 | 判定 | 口径 / 备注 |
|---|---|---|---|---|
| 任务完成率 | 75.0% | >=70% | ✅ | 15/20；跳过 5 条计入未完成 |
| 工具调用准确率 | 87.9% | >=85% | ✅ | LCS 对齐 29/33 次调用；recall=0.8787878787878788 |
| 格式正确率 | 100.0% | >=80% | ✅ | 6/6 个格式检查项 |
| 平均完成步数 | 3.07step | <=6step | ✅ | 统计 15 条已执行任务的 LLM 循环轮数 |
| 平均耗时 | 29.08s | <=180s | ✅ | 简单任务均值 9.45s（目标 ≤30s） |
| 首 Token 延迟 | 0.159s | <=1.5s | ✅ | stream=True 探测 3/3 次，取中位数 |
| 内存峰值 | 2.265GB | <=6.0GB | ✅ | 303 次采样；Ollama 系列进程峰值 2251MB；tasklist(ollama*) + GetProcessMemoryInfo(python)；GPU 显存峰值 5420MB 未计入 RSS 指标（nvidia-smi，1 张卡）；混合推理口径需在纯 CPU 环境复测 |

**达标 7/7 项，其中已测 7 项**；任务状态分布：{'completed': 15, 'skipped': 5}；简单任务平均耗时 9.45s（目标 ≤30s）。

口径说明：
- 任务完成率分母为 20 条；依赖未实现能力而跳过的任务记为未完成。
- 工具准确率口径 = LCS 对齐后的 micro precision；recall 另列，防止少调工具刷分。
- 平均完成步数 = LLM chat 调用轮数（Agent 循环步），不含纯工具内部处理。
- 跳过的任务不进入工具准确率 / 格式 / 步数 / 耗时的分母，避免摊薄。

## 逐条任务

| ID | 难度 | 状态 | 耗时 | 循环步 | 工具调用 | 说明 |
|---|---|---|---|---|---|---|
| S01 | simple | ✅ completed | 18.55s | 2 | read_document | — |
| S02 | simple | ✅ completed | 4.27s | 2 | list_dir | — |
| S03 | simple | ✅ completed | 6.07s | 2 | read_file | — |
| S04 | simple | ✅ completed | 11.21s | 2 | read_document | — |
| S05 | simple | ✅ completed | 7.15s | 2 | read_document | — |
| M01 | medium | ✅ completed | 49.64s | 4 | read_document → write_minutes_docx → write_todos_xlsx → write_notice_txt | — |
| M02 | medium | ✅ completed | 19.64s | 3 | read_document → write_file | — |
| M03 | medium | ✅ completed | 27.45s | 4 | read_file → read_file → write_todos_xlsx | 工具失败：read_file |
| M04 | medium | ✅ completed | 41.36s | 5 | read_file → read_file → write_notice_txt | 工具失败：read_file |
| M05 | medium | ✅ completed | 32.1s | 4 | read_document → read_document → write_notice_txt | — |
| M06 | medium | ✅ completed | 14.09s | 2 | read_document → write_minutes_docx | — |
| M07 | medium | ✅ completed | 89.51s | 6 | read_document → write_minutes_docx → write_todos_xlsx → write_notice_txt | — |
| M08 | medium | ✅ completed | 47.61s | 4 | read_document → write_minutes_docx → write_todos_xlsx → write_notice_txt | — |
| C01 | complex | ❌ skipped | 0.0s | 0 | — | 依赖未实现能力：asr、email_send、gui |
| C02 | complex | ✅ completed | 41.03s | 2 | read_document → search_knowledge_base | — |
| C03 | complex | ❌ skipped | 0.0s | 0 | — | 依赖未实现能力：asr、email_send、gui |
| C04 | complex | ❌ skipped | 0.0s | 0 | — | 依赖未实现能力：chart_gen、pptx_gen |
| C05 | complex | ✅ completed | 26.57s | 2 | search_knowledge_base | — |
| C06 | complex | ❌ skipped | 0.0s | 0 | — | 依赖未实现能力：gui、wps_adapter |
| C07 | complex | ❌ skipped | 0.0s | 0 | — | 依赖未实现能力：a2a、edge、mcp_server |

## 能力缺口归因

以下任务依赖尚未实现的能力，计入未完成：

- **C01**：依赖未实现能力：asr、email_send、gui
- **C03**：依赖未实现能力：asr、email_send、gui
- **C04**：依赖未实现能力：chart_gen、pptx_gen
- **C06**：依赖未实现能力：gui、wps_adapter
- **C07**：依赖未实现能力：a2a、edge、mcp_server

> 当前能力上限：最多 15/20 条可完成（75%）。该上限已高于 70% 目标；达标不等于能力齐备，剩余跳过项仍是明确缺口。

## 环境备注

- 首 Token 探测：stream=True 探测 3/3 次，取中位数
- 首 Token 全部样本：[123.8, 159.5, 6941.0] ms（中位数会掩盖冷启动离群值，故列出全部样本）
- 内存采样：303 次采样；Ollama 系列进程峰值 2251MB；tasklist(ollama*) + GetProcessMemoryInfo(python)；GPU 显存峰值 5420MB 未计入 RSS 指标（nvidia-smi，1 张卡）；混合推理口径需在纯 CPU 环境复测
- 模型空响应重试：2 次（qwen2.5 tools 模板偶发返回空 content 且 tool_calls 为空，客户端原样重试，payload 与评分口径不变）
