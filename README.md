# 端侧信创办公智能体（office-agent）

完全跑在 PC 端的政企办公 Agent：端侧 7B 量化模型 + 多智能体协作，
完成「文档处理 → 会议纪要 → 公文撰写 → 邮件通知」的办公闭环，数据不出本机。

完整技术方案见 `docs/tech-plan.md`。
许可：MIT，见 `LICENSE`。

> **项目状态（请先读）**：这是一个**阶段性 PoC，尚未通过方案 8.2 验收**。
> 已在真实模型 `qwen2.5:7b-instruct-q4_K_M` 上跑过**全量 20 条评测**：
> **7 项指标 7/7 已实测、6/7 达标**，唯一未达标的是任务完成率 **50%（10/20）**。
> 143 条离线断言全部通过。下文表格里的 ✅ 表示模块已实现且有验证，
> **不代表整体验收通过**。完整数据与失败归因见「第一轮真实评测」一节。

## 快速体验（不需要装 Ollama）

```powershell
.venv\Scripts\python.exe scripts\demo_offline.py      # 完整闭环演示，产出真实 Word/Excel 文件
.venv\Scripts\python.exe tests\test_harness_offline.py  # 24 项
.venv\Scripts\python.exe tests\test_pipeline_offline.py # 47 项
.venv\Scripts\python.exe tests\test_eval_offline.py    # 72 项（只验证评测器逻辑，不产生指标实测值）
```

需要 Ollama + 模型的真实链路（模型默认落在 `D:\Projects\.cache\ollama`）：

```powershell
.venv\Scripts\python.exe main.py --check                  # 环境自检
.venv\Scripts\python.exe scripts\probe_real_model.py     # 最小诊断：加载 / TTFT / function calling / 内存
.venv\Scripts\python.exe -m eval.run_eval                 # 全量 20 条真实评测
.venv\Scripts\python.exe -m eval.run_eval --tasks S01,M01 # 小样本
```

演示用脚本化 LLM 模拟 Qwen2.5-7B 的 6 次调用（plan → map×2 → reduce → todos → email），
走通 Planner → MeetingAgent → DocAgent → Reflection → EmailAgent → Reflection → DesktopAgent，
并在 `data/outbox/demo_<时间戳>/` 下产出会议纪要 docx、待办清单 xlsx、会议通知草稿
（独立目录，不会覆盖正在被 Word / 预览器打开的文件）。

## 架构与实现状态

| 目录 | 方案章节 | 状态 |
|---|---|---|
| `core/harness.py` | 3.2 主循环 | ✅ 工具调用 / 错误回灌 / 步数耗尽收尾 |
| `core/llm_client.py` | 5 端侧推理 | ✅ Ollama 客户端，纯标准库 urllib，零依赖 |
| `core/tool_registry.py` | MCP 工具层 | ✅ JSON Schema 自动导出、参数校验、异常收敛 |
| `core/output_parser.py` | 输出解析 | ✅ 三级降级（直接 parse → 剥 fence → 括号平衡扫描） |
| `core/prompt_builder.py` | 3.2 | ✅ 角色 + 工具清单 + 记忆摘要 |
| `agents/planner.py` | 4.1 | ✅ PlanAct + 动态重规划（replan） |
| `agents/meeting_agent.py` | 4.2 | ✅ Map-Reduce 摘要、结构化待办提取、短文本跳过 Map |
| `agents/doc_agent.py` + `tools/doc_tools.py` | 4.3 | ✅ 五类格式读取、GB/T 9704-2012 公文格式纪要、待办 Excel |
| `agents/email_agent.py` | 4.4 | ✅ 草稿生成、SQLite 通讯录收件人查询；**不自动发送** |
| `agents/desktop_agent.py` | 4.5 | 🟡 白名单 + 敏感路径拦截 + 后端能力探测；GUI 执行需人工确认 |
| `agents/reflection.py` | 4.7 | ✅ 确定性规则优先（格式/逻辑/事实/安全敏感信息） |
| `agents/orchestrator.py` | 2.2 | ✅ 完整会议闭环，含两次反思闸门 |
| `memory/structured_db.py` | 4.6 | ✅ SQLite 审计日志、用户偏好、本地通讯录 |
| `eval/` | 8 评测方案 | ✅ 20 条评测集、三层评分器、7 项指标采集、JSON/Markdown 报告；**已完成全量真实模型评测，6/7 达标** |
| `protocols/` `edge/` `adapt/` | 3.1 / 7 | ⬜ 接口占位，下一阶段 |

**技术决策**：PDF 解析用纯 Python 的 `pypdf` 替代方案中的 PyMuPDF——
国产 ARM 平台上 PyMuPDF 的预编译 wheel 经常缺失，纯 Python 实现部署成功率更高，
这一点本身也是信创适配的论据。

## 接入真实模型

下面是**换一台机器的通用安装流程**。本机因 C 盘空间不足且会话非管理员，
实际采用**官方免安装 zip**（v0.40.15）解压到 `D:\Projects\.cache\ollama\app`，
并用环境变量把模型、TEMP、TMP 全指向 D 盘后启动服务；C 盘未被占用。
第一轮真实评测即在该配置下完成。

```powershell
# 1. 缓存/模型权重重定向到 D 盘（已在本机配置，换机器需重跑一次）
powershell -ExecutionPolicy Bypass -File scripts\set_disk_env.ps1
# 重开终端后生效

# 2. 安装 Ollama：程序本体和模型权重都要指定到 D 盘
#    先下载 https://ollama.com/download/windows，然后：
powershell -ExecutionPolicy Bypass -File scripts\install_ollama.ps1 -Installer "<OllamaSetup.exe 完整路径>"
#    脚本内部用官方 /DIR 参数把程序装到 D:\Projects\.cache\ollama\app，
#    并再次确认 OLLAMA_MODELS=D:\Projects\.cache\ollama\models
#    ⚠️ 装完必须重开终端；若托盘进程已在跑，先退出再重启，否则它读不到环境变量

# 3. 拉模型（~4.7GB，落在 D:\Projects\.cache\ollama\models\blobs）
ollama pull qwen2.5:7b-instruct-q4_K_M
#    验证真的落在 D 盘：
#    powershell: Get-ChildItem D:\Projects\.cache\ollama\models\blobs | Measure-Object Length -Sum

# 4. 依赖
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 5. 自检 + 运行
python main.py --check
python main.py --pipeline data/inbox/sample_meeting.txt --title "2026年第11次部门例会纪要"
python main.py                      # 交互模式
python main.py "列出 data/inbox 下的文件并总结内容"   # 通用任务（PlanAct + 工具循环）
```

## 评测体系（方案第 8 章）

| 文件 | 职责 |
|---|---|
| `eval/benchmark_tasks.json` | 20 条任务（简单 5 / 中等 8 / 复杂 7），含标准答案工具序列、评分规则、能力依赖标注 |
| `eval/fixtures.py` | 可复现素材：docx / xlsx / 手写 PDF / txt / 长短会议转写稿 |
| `eval/metrics.py` | LLM 与工具调用埋点、真 TTFT 探测、运行时 RSS 采样、LCS 工具准确率、7 项指标聚合 |
| `eval/evaluator.py` | 执行器 + 三层评分器 + JSON/Markdown 报告 |
| `eval/run_eval.py` | CLI 入口；`eval/results/` 存每轮结果 |

```powershell
.venv\Scripts\python.exe -m eval.run_eval --list     # 查看 20 条任务及能力缺口
.venv\Scripts\python.exe -m eval.run_eval            # 真实模型全量评测（需 Ollama + 模型）
.venv\Scripts\python.exe -m eval.run_eval --tasks S01,S02,M01
.venv\Scripts\python.exe -m eval.run_eval --include-unsupported  # 复杂任务也实跑，看真实失败原因
.venv\Scripts\python.exe tests\test_eval_offline.py  # 离线验证评测器逻辑
```

**评分口径**：确定性规则，不用 LLM-as-judge（端侧只有 7B，让模型自评既不可信也不可复现）。
三层检查：最终回答关键字/长度 → 产出文件存在、体积、内容 → 文档格式
（GB/T 9704 页边距/字号/中文字体/行距、Excel 表头）。
工具准确率用 LCS 对齐后的 micro precision，recall 另列，防止少调工具刷分。

**防自欺设计**：
- 默认拒绝脚本化 LLM；离线自测须显式 `allow_mock`，报告顶部标注「不构成指标实测」
- 首 Token 延迟单独发 `stream=True` 探测，逐块读到第一个非空 content 才停表。
  产品链路用 `stream=False`，整段响应时间不是 TTFT，不能拿来充数
- 内存峰值采 Ollama 系列进程（含各版本 runner 名 `ollama.exe` / `llama-server.exe`）
  + Python 进程 RSS；只采到 serve 进程（<300MB）就标 `unavailable`，
  不把 20MB 的 Python 内存当成「0.02GB 达标」。GPU 显存单独记录、**不混入 RSS 指标**
- 依赖未实现能力而跳过的任务**计入完成率分母**（算未完成），但不摊薄工具/格式/步数指标
- 每条评测任务用独立通讯录 DB，避免跨轮持久化状态污染评测结果

> **完成率硬约束**：复杂 7 条全依赖未实现能力，即使另外 13 条全对也只有
> 13/20 = 65% < 70%。详见「当前缺失项」。

## 当前缺失项（如实记录）

- **任务完成率 50% 未达标**（目标 ≥70%）：3 条中等任务因模型产出与回答不一致 / 漏写落款 / 未写部门名而失败；7 条复杂任务依赖未实现能力被跳过
- **能力上限硬约束**：复杂 7 条全依赖 RAG / 语音转写 / GUI / A2A 等未实现能力，即使另外 13 条全对也只有 65% < 70%。补齐至少 2 条复杂任务能力前，模型调优无法让这项达标
- **口径限制**：本机有 RTX 2060，Ollama 走 CPU/GPU 混合 offload；报告的「内存峰值」只含系统 RSS，~5.5GB 显存另列未计入。方案 8.2 的 <6GB 需在纯 CPU 环境复测才算严谨
- whisper.cpp 未安装：`transcribe_audio` 会明确报缺失，可用 `load_transcript` 走文本转写稿
- Windows UIA / Linux AT-SPI 后端未安装，DesktopAgent 如实标记 `blocked`，不伪造发送成功
- MCP 服务端、A2A 协议、Chroma 向量库与引用溯源、端边云协同、语音实际转写、GUI 实际操作均未实现
- 阶段 3 国产适配（HAL 抽象层、ARM 交叉编译、麒麟/UOS 与 WPS Linux 兼容性验证）未开始

## 第一轮真实评测（2026-11-09）

- 环境：Windows 11 / Python 3.13.14 / i9-9900 / 16GB RAM / RTX 2060；Ollama v0.40.15 免安装版，模型与程序全部在 D 盘
- 模型：`qwen2.5:7b-instruct-q4_K_M`（7.6B，Q4_K_M，官方 `capabilities: ["completion","tools"]`）
- 报告：`eval/results/first_real_eval_20261109.md`（已脱敏并入库，含逐条任务与能力缺口归因；其余真实运行报告不入库）

| 指标 | 实测 | 目标 | 判定 |
|---|---|---|---|
| 任务完成率 | **50.0%**（10/20） | ≥70% | ❌ |
| 工具调用准确率 | **89.7%**（26/29，recall 0.929） | ≥85% | ✅ |
| 格式正确率 | **100%**（6/6） | ≥80% | ✅ |
| 平均完成步数 | **3.0 步** | ≤6 步 | ✅ |
| 平均耗时 | **28.8s**（简单任务 10.77s） | ≤180s | ✅ |
| 首 Token 延迟 | **0.163s**（stream=True ×3 中位数） | <1.5s | ✅ |
| 内存峰值 | **2.493GB**（258 次 RSS 采样） | <6GB | ✅ |

值得注意的两点：**工具调用准确率 89.7% 超过目标**——方案 10.2 曾估计 7B 原生只有约 60%，
实测得益于官方模型的 tools 模板 + 注册表 JSON Schema 约束；最小探针中 function calling 3/3 命中。
另外三条失败均为模型侧缺陷（M02 回答与写入文件不一致、M04 声称有落款但文件没有、M08 未写部门名），
**不是评分器 Bug，也没有为此放宽任何评分规则**。

真实链路还暴露并修复了两个真 Bug：Ollama `format=json` 约束会让 7B 只输出单个对象、
待办被静默丢成 0 条；以及内存采样漏掉 `llama-server.exe` 导致把 0.117GB 误报成峰值。

## 验收口径（方案 8.2）

评测集 20 条：简单 5 条（单工具 / 文档处理）+ 中等 8 条（多步骤 / 文档+数据）+ 复杂 7 条（多 Agent / RAG / 桌面操作）。

| 指标 | 定义 | 目标 | 当前 | 采数方式 |
|---|---|---|---|---|
| 任务完成率 | 最终产出符合用户要求的任务比例 | ≥70% | ❌ 50.0% | 20 条分母固定，三层评分全通过才算完成 |
| 工具调用准确率 | 工具选择是否正确（对照标准答案） | ≥85% | ✅ 89.7% | 注册表埋点 + LCS 对齐 micro precision |
| 格式正确率 | 生成文档是否符合模板格式规范 | ≥80% | ✅ 100% | python-docx / openpyxl 读回版式校验 |
| 平均完成步数 | 完成一个任务平均 Agent 循环步数 | ≤6 步 | ✅ 3.0 步 | LLM 调用次数埋点 |
| 平均耗时 | 端到端耗时（简单任务 ≤30 秒） | ≤3 分钟 | ✅ 28.8s | wall clock，简单任务单独统计 |
| 首 Token 延迟 | 模型第一次输出的等待时间 | <1.5 秒 | ✅ 0.163s | `stream=True` 独立探测，3 次取中位数 |
| 内存峰值 | 运行时最大内存占用 | <6GB | ✅ 2.493GB | 1s 轮询 Ollama 系列 + Python 进程 RSS；显存另列 |
