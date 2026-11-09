# 端侧信创办公智能体（office-agent）

完全跑在 PC 端的政企办公 Agent：端侧 7B 量化模型 + 多智能体协作，
完成「文档处理 → 会议纪要 → 公文撰写 → 邮件通知」的办公闭环，数据不出本机。

完整技术方案见 `docs/tech-plan.md`。
许可：MIT，见 `LICENSE`。

> **项目状态（请先读）**：这是一个**阶段性 PoC**，不是已交付的商用系统。
> 已在真实模型 `qwen2.5:7b-instruct-q4_K_M` 上跑过**两轮全量 20 条评测**：
> 第一轮 **6/7 达标、完成率 50%（10/20）**；第二轮（补齐工程闭环 + 本地检索后）
> **7/7 达标、完成率 75%（15/20）**。当前离线断言 **266/266** 全部通过。
> 但请务必注意：**这是项目自评测，不是第三方验收**；仍有 5 条复杂任务被跳过，
> 且内存指标在 GPU 混合推理下取得、尚未在纯 CPU 信创环境复测。
>
> 本轮另外实现了 **MCP 服务端、A2A 协议、Chroma 向量库与引用溯源、端边云协同**
> （见「协议层与端边云协同」）。这四块**尚未纳入 20 条评测集**：两轮成绩不受影响，
> 也不代表验收范围扩大。两轮报告与失败归因见下方「真实评测」章节。

## 快速体验（不需要装 Ollama）

```powershell
.venv\Scripts\python.exe scripts\demo_offline.py      # 完整闭环演示，产出真实 Word/Excel 文件
.venv\Scripts\python.exe tests\test_harness_offline.py  # 24 项
.venv\Scripts\python.exe tests\test_pipeline_offline.py # 47 项
.venv\Scripts\python.exe tests\test_eval_offline.py    # 92 项（只验证评测器逻辑，不产生指标实测值）
.venv\Scripts\python.exe tests\test_protocols_edge_offline.py  # 103 项（协议层/向量库/端边云，含真实 HTTP 往返）
```

协议层与协同层也不需要 Ollama：

```powershell
.venv\Scripts\python.exe main.py --mcp                     # MCP stdio 服务端
.venv\Scripts\python.exe main.py --mcp-http 127.0.0.1:8765
.venv\Scripts\python.exe main.py --a2a 127.0.0.1:8770     # Agent Card: /.well-known/agent.json
.venv\Scripts\python.exe main.py --edge-selftest           # 端边云三节点本地联调
.venv\Scripts\python.exe main.py --index-vectors           # 重建 Chroma 索引
```

需要 Ollama + 模型的真实链路（模型默认落在项目同级 `.cache\ollama`，由脚本按盘符自动推导）：

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
| `core/harness.py` | 3.2 主循环 | ✅ 工具调用 / 错误回灌 / **写后回读校验** / 空回答续跑 / 步数耗尽收尾 |
| `core/llm_client.py` | 5 端侧推理 | ✅ Ollama 客户端，纯标准库 urllib，零依赖 |
| `core/tool_registry.py` | MCP 工具层 | ✅ JSON Schema 自动导出、参数校验、异常收敛 |
| `core/output_parser.py` | 输出解析 | ✅ 三级降级（直接 parse → 剥 fence → 括号平衡扫描） |
| `core/prompt_builder.py` | 3.2 | ✅ 角色 + 工具清单 + 记忆摘要 |
| `agents/planner.py` | 4.1 | ✅ PlanAct + 动态重规划（replan） |
| `agents/meeting_agent.py` | 4.2 | ✅ Map-Reduce 摘要、结构化待办提取、短文本跳过 Map |
| `agents/doc_agent.py` + `tools/doc_tools.py` | 4.3 | ✅ 五类格式读取、GB/T 9704-2012 公文格式纪要、待办 Excel |
| `agents/email_agent.py` | 4.4 | ✅ 草稿生成、SQLite 通讯录收件人查询、收件部门透传到通知正文；**不自动发送** |
| `tools/knowledge_tools.py` | 4.3 / 8.1 | 🟡 **纯标准库本地关键词检索**（中文 bigram + 片段级 BM25）；评测集 C02/C05 用的仍是这个 |
| `memory/vector_store.py` | 4.3 / 4.6 | ✅ 真实 Chroma 持久化 + HNSW cosine 检索、去重、时间衰减、**结构化 Citation 与引文哈希核验**；🟡 embedding 是本地字面特征，非语义模型 |
| `tools/rag_tools.py` | 4.3 | ✅ `vector_search` / `verify_quote` / `vector_index_status`，返回 [n] 编号、来源、sha256；**未注册进评测器** |
| `agents/desktop_agent.py` | 4.5 | 🟡 白名单 + 敏感路径拦截 + 后端能力探测；GUI 执行需人工确认 |
| `agents/reflection.py` | 4.7 | ✅ 确定性规则优先（格式/逻辑/事实/安全敏感信息） |
| `agents/orchestrator.py` | 2.2 | ✅ 完整会议闭环，含两次反思闸门 |
| `memory/structured_db.py` | 4.6 | ✅ SQLite 审计日志、用户偏好、本地通讯录 |
| `eval/` | 8 评测方案 | ✅ 20 条评测集、三层评分器、7 项指标采集、JSON/Markdown 报告；**两轮全量真实模型评测，第二轮 7/7 达标** |
| `protocols/mcp_server.py` | 5 / 7 | ✅ MCP JSON-RPC 2.0（stdio + HTTP）：tools/list / tools/call / resources；dangerous 工具默认策略拒绝 |
| `protocols/a2a.py` | 3.3 / 7 | ✅ A2A Agent Card + `message/send` / `tasks/get` / `tasks/cancel` + 纯标准库客户端；🟡 无 SSE 流式 / push / 鉴权 |
| `edge/coordination.py` | 3.1 | ✅ 三级路由、上送脱敏、离线队列、端↔边 A2A 同步、云侧只下发权重；🟡 三节点在本机模拟，无 32B 模型 |
| `adapt/` | 7 阶段 3 | ⬜ 仍为占位：HAL / ARM 交叉编译 / 麒麟 UOS 适配未开始 |

**技术决策**：PDF 解析用纯 Python 的 `pypdf` 替代方案中的 PyMuPDF——
国产 ARM 平台上 PyMuPDF 的预编译 wheel 经常缺失，纯 Python 实现部署成功率更高，
这一点本身也是信创适配的论据。

## 协议层、向量库与端边云协同（本轮实现）

四块能力都只用标准库 + `chromadb` 客户端实现，**没有引入 LangChain / LlamaIndex /
MCP SDK / A2A SDK / FastAPI**：MCP 与 A2A 服务端是 `json` + `http.server`，
客户端是 `urllib`，离线队列是 `sqlite3`。

| 能力 | 实现要点 |
|---|---|
| **MCP 服务端** | JSON-RPC 2.0 over stdio（换行分隔帧）+ 简化 HTTP POST；`initialize` / `ping` / `tools/list` / `tools/call` / `resources/list` / `resources/read`；直接把现有 `ToolRegistry` 映射成 MCP inputSchema，**不重复实现工具**；`dangerous=True` 工具列出但默认拒绝，需 `--allow-dangerous`；resources 暴露知识库原文供客户端核验引文 |
| **A2A 协议** | `GET /.well-known/agent.json` Agent Card；`message/send`（v0.3）+ `tasks/send`（v0.2 别名）、`tasks/get`、`tasks/cancel`；Task 状态机 submitted → working → completed / failed / canceled，带 artifacts 与 history；`A2AClient` 给端↔边同步复用；消息 parts 兼容 `kind` 与 `type` |
| **Chroma + 引用溯源** | `chromadb.PersistentClient` + HNSW cosine 集合，持久化在 `data/workspace/chroma`；文档分块 → embedding → 幂等 upsert（按 sha256 去重）→ 相似度 × 时间衰减召回；每条 Citation 带来源、片段号、原文、sha256；`verify()` 重读源文件核对原文与哈希，`verify_answer()` 拦截 `[n]` 幻觉编号；工具 `vector_search` / `verify_quote` / `vector_index_status` |
| **端边云协同** | 三级 `NodeDescriptor`；按隐私等级 / 复杂度 / 能力 / 在线状态路由；上送边侧前规则化脱敏（邮箱、手机、证件号、银行卡、IP、本机路径）；SQLite 离线队列 + 恢复后 `flush()`；端↔边复用 A2A 通道；云侧只下发模型权重清单并可校验 sha256，**业务数据上传在构造、路由、API 三层强制拒绝** |

**本轮实测**（2026-11-09，全部不需要 Ollama，HTTP 部分是真实本机 socket 往返）：

```text
MCP       : 13 个 tools / 7 个 resources；只读调用成功；dangerous 调用被 MCP_POLICY 拒绝；
            --allow-dangerous 后可写；目录穿越被拒；stdio 帧与 HTTP POST 往返通过
A2A       : 2 个 skills；message/send → completed（1 artifact）；tasks/get 命中；
            tasks/cancel → canceled；message/stream 如实返回 -32601
Chroma    : chromadb 1.5.9，backend=chroma（真实 HNSW）；扫描 23 片段 → 索引 14（去重 9）；
            检索 3 命中，引文 3/3 可核验；[9] 幻觉编号与篡改引文均被拦截
端边云    : local_only 再复杂也不出机；复杂任务脱敏上送边侧（email 1 / phone 1）；
            云侧业务数据在路由与 API 层双重拒绝；离线排队 1 条 → 恢复后 flush 同步 1 条
离线断言  : 新增 103 条，连同原有 163 条，共 266/266 通过
```

**边界（必须和上面一起说）**：

- **embedding 不是语义模型**：为不再加载一个 embedding 模型（端侧 7B + 显存预算已紧），
  用纯标准库中文 bigram hashing trick 生成 256 维向量，只编码字面特征，
  同义/近义改写召回不到。`embed(texts)` 接口可替换成真模型。
  `chromadb` 缺失时降级到标准库余弦索引，并在所有结果里标注
  `backend=fallback-hash-cosine` 与降级原因，**绝不冒充 Chroma**。
- **A2A 只实现核心子集**：未做 SSE 流式订阅、push notification、鉴权签名；
  Card 里 `streaming: false`，调用流式方法返回 -32601。Task 仅内存存储，不持久化。
- **MCP HTTP 是简化 JSON-RPC over HTTP**，不是完整 Streamable HTTP（无 SSE / session 协商）。
- **三节点在本机不同端口模拟**，未在飞腾/鲲鹏/私有云机器部署；边侧没有 32B 模型，
  默认执行确定性本地检索并在返回里明确标 `executor=local_tool:search_knowledge_base`，
  **不伪造大模型推理**。云侧只做权重清单下发，训练 / SFT 未实现。
- 脱敏是正则规则覆盖，**不能替代正式 DLP**，中文姓名等非规则化 PII 未覆盖。
- **四块能力尚未纳入 20 条评测集**：评测器使用固定 registry，本轮新增工具只注册到
  CLI 与 MCP；`benchmark_tasks.json` 的 `requires` 一条未删、grading 一个字未改。
  因此 50% / 75% 两轮成绩仍可复现，也**不代表验收范围扩大**。

## 接入真实模型

下面是**换一台机器的通用安装流程**。本机因系统盘空间不足且会话非管理员，
实际采用**官方免安装 zip**（v0.40.15）解压到「项目同级目录 `.cache\ollama\app`」，
并用环境变量把模型、TEMP、TMP 全指向非系统盘后启动服务；系统盘未被占用。
第一轮真实评测即在该配置下完成。

```powershell
# 1. 缓存/模型权重重定向到非系统盘（换机器需重跑一次）
powershell -ExecutionPolicy Bypass -File scripts\set_disk_env.ps1
#    脚本按所在位置自动推导「项目同级目录\.cache」，可用 -CacheRoot 覆盖；重开终端后生效

# 2. 安装 Ollama：程序本体和模型权重都要放到非系统盘
#    先下载 https://ollama.com/download/windows，然后：
powershell -ExecutionPolicy Bypass -File scripts\install_ollama.ps1 -Installer "<OllamaSetup.exe 完整路径>"
#    脚本用官方 /DIR 参数把程序装到 <缓存根>\ollama\app，
#    并把 OLLAMA_MODELS 指向 <缓存根>\ollama\models
#    ⚠️ 装完必须重开终端；若托盘进程已在跑，先退出再重启，否则它读不到环境变量

# 3. 拉模型（~4.7GB，落在 $env:OLLAMA_MODELS\blobs）
ollama pull qwen2.5:7b-instruct-q4_K_M
#    验证真的不在系统盘：
#    powershell: Get-ChildItem "$env:OLLAMA_MODELS\blobs" | Measure-Object Length -Sum

# 4. 依赖（chromadb 依赖较多，国内建议走镜像源）
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple/

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

> **完成率的分母口径**：第一轮 7 条复杂任务全部因 `requires` 非空被跳过，
> 上限锁死在 13/20 = 65% < 70%，数学上不可能达标。第二轮为 C02/C05
> 真实落地了本地检索工具（而非删掉 `requires` 假装实现），上限变为
> 15/20 = 75%，指标才具备可达性。**跳过项仍计入分母**，没有靠剔除难题抬高成绩。

## 当前缺失项（如实记录）

- **仍是自评测，不是第三方验收**：20 条任务、素材、评分规则、70% 阈值全部由本项目定义。75% 只说明自设标尺已达成
- **5 条复杂任务仍被跳过**（C01/C03/C04/C06/C07）：ASR 语音转写、邮件实际发送、GUI 操作 WPS、图表/PPT 生成仍未实现；C07 标注的「端边云协同」本轮虽已落地，但**评测任务没有解锁**，仍按未完成计入分母
- **C02/C05 的 RAG 仍是能力收缩实现**：评测集走的仍是本地 bigram/BM25 关键词检索，「按时间排序」依赖片段内日期线索由模型复述。本轮新增的 Chroma 向量库是**独立能力、未接入这两条任务**，且其 embedding 非语义模型（见下）
- **C01/C03 的标准答案与方案安全边界冲突**：`expected_tools` 含 `send_email`，但方案 4.7 明确规定有副作用操作须人工确认、系统只生成草稿。这两条后续应改评测集，而不是照着实现自动发送
- **口径限制**：本机有 RTX 2060，Ollama 走 CPU/GPU 混合 offload；「内存峰值 2.265GB」只含系统 RSS，另有 ~5.3GB 显存未计入。方案 8.2 的 <6GB 需在纯 CPU 信创环境复测才算严谨
- **首 Token 冷启动离群**：第二轮 3 次样本 `[123.8, 159.5, 6941.0] ms`，中位数 0.159s 达标，但冷启动 6.9s 不满足 <1.5s。报告已列出全部样本
- **工具准确率下降**：89.7% → 87.88%，因新增 2 条任务进入分母，不是模型退化；仍高于 85% 阈值
- **模型空响应重试**：第二轮 2 次。qwen2.5 的 tools 模板偶发返回空 `content` 且 `tool_calls` 为空（`done_reason=stop`、`eval_count>0`），客户端原样重试、不改 payload 与评分口径，重试次数写进报告
- whisper.cpp 未安装：`transcribe_audio` 会明确报缺失，可用 `load_transcript` 走文本转写稿
- Windows UIA / Linux AT-SPI 后端未安装，DesktopAgent 如实标记 `blocked`，不伪造发送成功
- **本轮新增四块能力未纳入评测集**：MCP / A2A / Chroma / 端边云只做了单元与联调验证，
  `benchmark_tasks.json` 的 `requires` 一条未删、grading 未改；要纳入成绩需单独授权并**全量重跑**
- **Chroma 的 embedding 不是语义模型**：本地 bigram hashing 特征，只编码字面匹配；
  「语义检索 + 引用溯源」里的「语义」二字不成立，能成立的是「向量索引 + 结构化引文核验」
- **协议与协同的实现边界**：MCP 无 Streamable HTTP / SSE；A2A 无流式订阅、push、鉴权，
  Task 不持久化；三节点在本机模拟、边侧无 32B 模型、云侧无训练 SFT；脱敏不能替代正式 DLP
- 语音实际转写、GUI 实际操作仍未实现
- 阶段 3 国产适配（HAL 抽象层、ARM 交叉编译、麒麟/UOS 与 WPS Linux 兼容性验证）未开始，`adapt/` 仍为占位

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

**工具调用准确率 89.7% 超过目标**——方案 10.2 曾估计 7B 原生只有约 60%，
实测得益于官方模型的 tools 模板 + 注册表 JSON Schema 约束；最小探针中 function calling 3/3 命中。

> **对第一轮失败归因的更正**：第二轮复核了三条失败任务的**实际落盘文件**，
> 发现首轮「均为模型侧缺陷」的定性不准确：
> - **M02** 文件内容数据完全正确，只是把源表行标签「合计」改写成了「总销售额」，
>   属于**评分器字面关键字的假阴性** + 回答与文件不一致；
> - **M04** 缺落款是 generic 路由没有 `EmailAgent.NOTICE_PROMPT` 的落款约束，
>   是**两条路由行为不一致**的工程缺陷；
> - **M08** 缺「技术科」是 `recipients_group` 参数传进编排器后**从未被使用**的确定性代码 Bug，7B 根本没机会知道要写部门名。
>
> 三条共享的系统性根因是 **generic 路由写完文件后没有任何回读校验**，
> 模型凭记忆复述出文件里不存在的字段。评分规则全程未改。

真实链路还暴露并修复了两个真 Bug：Ollama `format=json` 约束会让 7B 只输出单个对象、
待办被静默丢成 0 条；以及内存采样漏掉 `llama-server.exe` 导致把 0.117GB 误报成峰值。

## 第二轮真实评测（2026-11-09，7/7 达标）

- 报告：`eval/results/second_real_eval_20261109.md`（已脱敏入库；首轮报告保留作对照）
- 环境、模型与第一轮完全相同，全量 20 条，耗时 7m25s，**15 条完成 / 5 条跳过 / 0 条失败**

| 指标 | 第一轮 | 第二轮 | 目标 | 判定 |
|---|---|---|---|---|
| 任务完成率 | 50.0%（10/20） | **75.0%（15/20）** | ≥70% | ✅ |
| 工具调用准确率 | 89.7%（26/29） | **87.88%**（29/33，recall 0.879） | ≥85% | ✅ |
| 格式正确率 | 100%（6/6） | **100%**（6/6） | ≥80% | ✅ |
| 平均完成步数 | 3.0 | **3.07** | ≤6 | ✅ |
| 平均耗时 | 28.8s | **29.08s**（简单任务 9.45s） | ≤180s | ✅ |
| 首 Token 延迟 | 0.163s | **0.159s** | <1.5s | ✅ |
| 内存峰值 | 2.493GB | **2.265GB**（303 次采样） | <6GB | ✅ |

**这一轮做了什么（全部是产品代码层修复，未改任何 grading / 阈值 / 关键字）**：

1. `core/harness.py`：写工具成功后由框架直接回读真实落盘内容并回灌模型
   （不走 registry，不污染工具调用统计），抑制「回答与文件不一致」；通用规则要求保留源数据字段名与行标签。
2. `tools/doc_tools.py`：通知工具补 `signature` / `department` 参数与默认落款，
   收敛 generic 与 pipeline 两条路由的格式契约。
3. `agents/email_agent.py`：把 `recipients_group` 真正注入通知 prompt 与落盘内容。
4. `tools/knowledge_tools.py`（新增）：纯标准库中文 bigram + 片段级 BM25 本地检索，
   返回带来源路径的证据片段，解锁 C02 / C05；不引 embedding 模型或向量库。
5. `core/llm_client.py`：对 qwen2.5 tools 模板偶发的空响应做原样重试并计数披露。
6. `eval/benchmark_tasks.json`：C02 原指令「根据这份政策文件」无指代对象，补上
   `{fixture:policy_doc}` 占位符使任务自包含；C02/C05 的 `requires` 在能力真实落地后解除。

**没有做的事**：没放宽评分关键字、没删 `requires` 假装能力已实现、没把 skipped 剔出分母、
没为刷分实现自动发邮件、没改 70% 阈值。

## 验收口径（方案 8.2）

评测集 20 条：简单 5 条（单工具 / 文档处理）+ 中等 8 条（多步骤 / 文档+数据）+ 复杂 7 条（多 Agent / RAG / 桌面操作）。
以下为**第二轮实测值**；口径限制见「当前缺失项」。

| 指标 | 定义 | 目标 | 当前 | 采数方式 |
|---|---|---|---|---|
| 任务完成率 | 最终产出符合用户要求的任务比例 | ≥70% | ✅ 75.0% | 20 条分母固定，跳过算未完成，三层评分全通过才算完成 |
| 工具调用准确率 | 工具选择是否正确（对照标准答案） | ≥85% | ✅ 87.88% | 注册表埋点 + LCS 对齐 micro precision |
| 格式正确率 | 生成文档是否符合模板格式规范 | ≥80% | ✅ 100% | python-docx / openpyxl 读回版式校验 |
| 平均完成步数 | 完成一个任务平均 Agent 循环步数 | ≤6 步 | ✅ 3.07 步 | LLM 调用次数埋点 |
| 平均耗时 | 端到端耗时（简单任务 ≤30 秒） | ≤3 分钟 | ✅ 29.08s | wall clock，简单任务单独统计 |
| 首 Token 延迟 | 模型第一次输出的等待时间 | <1.5 秒 | ✅ 0.159s | `stream=True` 独立探测，3 次取中位数；全部样本已列出 |
| 内存峰值 | 运行时最大内存占用 | <6GB | ✅ 2.265GB | 1s 轮询 Ollama 系列 + Python 进程 RSS；显存另列，纯 CPU 待复测 |
