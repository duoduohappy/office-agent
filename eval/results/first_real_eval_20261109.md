# 评测报告 eval_20261109_182104

- 时间：2026-11-09T18:27:24
- 模型：`qwen2.5:7b-instruct-q4_K_M` @ http://127.0.0.1:11434
- 平台：Windows 11 / Python 3.13.14
- 任务数：20（全量 20 条）
- 产出目录：`data/outbox/eval_20261109_182104`

## 七项指标（方案 8.2）

| 指标 | 实测 | 目标 | 判定 | 口径 / 备注 |
|---|---|---|---|---|
| 任务完成率 | 50.0% | >=70% | ❌ | 10/20；跳过 7 条计入未完成 |
| 工具调用准确率 | 89.7% | >=85% | ✅ | LCS 对齐 26/29 次调用；recall=0.9285714285714286 |
| 格式正确率 | 100.0% | >=80% | ✅ | 6/6 个格式检查项 |
| 平均完成步数 | 3.0step | <=6step | ✅ | 统计 13 条已执行任务的 LLM 循环轮数 |
| 平均耗时 | 28.8s | <=180s | ✅ | 简单任务均值 10.77s（目标 ≤30s） |
| 首 Token 延迟 | 0.163s | <=1.5s | ✅ | stream=True 探测 3/3 次，取中位数 |
| 内存峰值 | 2.493GB | <=6.0GB | ✅ | 258 次采样；Ollama 系列进程峰值 2489MB；tasklist(ollama*) + GetProcessMemoryInfo(python)；GPU 显存峰值 5569MB 未计入 RSS 指标（nvidia-smi，1 张卡）；混合推理口径需在纯 CPU 环境复测 |

**达标 6/7 项，其中已测 7 项**；任务状态分布：{'completed': 10, 'failed': 3, 'skipped': 7}；简单任务平均耗时 10.77s（目标 ≤30s）。

口径说明：
- 任务完成率分母为 20 条；依赖未实现能力而跳过的任务记为未完成。
- 工具准确率口径 = LCS 对齐后的 micro precision；recall 另列，防止少调工具刷分。
- 平均完成步数 = LLM chat 调用轮数（Agent 循环步），不含纯工具内部处理。
- 跳过的任务不进入工具准确率 / 格式 / 步数 / 耗时的分母，避免摊薄。

## 逐条任务

| ID | 难度 | 状态 | 耗时 | 循环步 | 工具调用 | 说明 |
|---|---|---|---|---|---|---|
| S01 | simple | ✅ completed | 18.06s | 2 | read_document | — |
| S02 | simple | ✅ completed | 5.42s | 2 | list_dir | — |
| S03 | simple | ✅ completed | 5.88s | 2 | read_file | — |
| S04 | simple | ✅ completed | 17.54s | 3 | read_file → read_document | — |
| S05 | simple | ✅ completed | 6.92s | 2 | read_document | — |
| M01 | medium | ✅ completed | 44.37s | 4 | read_document → write_minutes_docx → write_todos_xlsx → write_notice_txt | — |
| M02 | medium | ❌ failed | 21.37s | 3 | read_document → write_file | 未通过：文件内容关键字 |
| M03 | medium | ✅ completed | 26.38s | 3 | read_file → write_todos_xlsx | — |
| M04 | medium | ❌ failed | 21.7s | 3 | read_file → write_notice_txt | 未通过：文件内容关键字 |
| M05 | medium | ✅ completed | 46.65s | 3 | read_document → read_document → write_notice_txt | — |
| M06 | medium | ✅ completed | 16.85s | 2 | read_document → write_minutes_docx | — |
| M07 | medium | ✅ completed | 96.54s | 6 | read_document → write_minutes_docx → write_todos_xlsx → write_notice_txt | — |
| M08 | medium | ❌ failed | 46.66s | 4 | read_document → write_minutes_docx → write_todos_xlsx → write_notice_txt | 未通过：文件内容关键字 |
| C01 | complex | ❌ skipped | 0.0s | 0 | — | 依赖未实现能力：asr、email_send、gui |
| C02 | complex | ❌ skipped | 0.0s | 0 | — | 依赖未实现能力：rag、citation |
| C03 | complex | ❌ skipped | 0.0s | 0 | — | 依赖未实现能力：asr、email_send、gui |
| C04 | complex | ❌ skipped | 0.0s | 0 | — | 依赖未实现能力：chart_gen、pptx_gen |
| C05 | complex | ❌ skipped | 0.0s | 0 | — | 依赖未实现能力：rag、vector_store |
| C06 | complex | ❌ skipped | 0.0s | 0 | — | 依赖未实现能力：gui、wps_adapter |
| C07 | complex | ❌ skipped | 0.0s | 0 | — | 依赖未实现能力：a2a、edge、mcp_server |

## 能力缺口归因

以下任务依赖尚未实现的能力，计入未完成：

- **C01**：依赖未实现能力：asr、email_send、gui
- **C02**：依赖未实现能力：rag、citation
- **C03**：依赖未实现能力：asr、email_send、gui
- **C04**：依赖未实现能力：chart_gen、pptx_gen
- **C05**：依赖未实现能力：rag、vector_store
- **C06**：依赖未实现能力：gui、wps_adapter
- **C07**：依赖未实现能力：a2a、edge、mcp_server

> 当前能力上限：最多 13/20 条可完成（65%）。若该上限低于 70% 目标，补齐能力之前无论怎么调优模型都无法达标。

## 环境备注

- 首 Token 探测：stream=True 探测 3/3 次，取中位数
- 内存采样：258 次采样；Ollama 系列进程峰值 2489MB；tasklist(ollama*) + GetProcessMemoryInfo(python)；GPU 显存峰值 5569MB 未计入 RSS 指标（nvidia-smi，1 张卡）；混合推理口径需在纯 CPU 环境复测
