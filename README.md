# 端侧信创办公智能体（office-agent）

完全跑在 PC 端的政企办公 Agent：端侧 7B 量化模型 + 多智能体协作，
完成「文档处理 → 会议纪要 → 公文撰写 → 邮件通知」的办公闭环，数据不出本机。

完整技术方案见 `docs/tech-plan.md`。

## 快速体验（不需要装 Ollama）

```powershell
.venv\Scripts\python.exe scripts\demo_offline.py      # 完整闭环演示，产出真实 Word/Excel 文件
.venv\Scripts\python.exe tests\test_harness_offline.py  # 24 项
.venv\Scripts\python.exe tests\test_pipeline_offline.py # 47 项
```

演示用脚本化 LLM 模拟 Qwen2.5-7B 的 6 次调用（plan → map×2 → reduce → todos → email），
走通 Planner → MeetingAgent → DocAgent → Reflection → EmailAgent → Reflection → DesktopAgent，
并在 `data/outbox/` 下产出会议纪要 docx、待办清单 xlsx、会议通知草稿。

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
| `protocols/` `edge/` `eval/` `adapt/` | 3.1 / 8 / 7 | ⬜ 接口占位，下一阶段 |

**技术决策**：PDF 解析用纯 Python 的 `pypdf` 替代方案中的 PyMuPDF——
国产 ARM 平台上 PyMuPDF 的预编译 wheel 经常缺失，纯 Python 实现部署成功率更高，
这一点本身也是信创适配的论据。

## 接入真实模型

```powershell
# 1. 缓存/模型权重重定向到 D 盘（已在本机配置，换机器需重跑一次）
powershell -ExecutionPolicy Bypass -File scripts\set_disk_env.ps1
# 重开终端后生效

# 2. 安装 Ollama 并拉模型（~4.7GB，落在 D:\Projects\.cache\ollama）
ollama pull qwen2.5:7b-instruct-q4_K_M

# 3. 依赖
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 4. 自检 + 运行
python main.py --check
python main.py --pipeline data/inbox/sample_meeting.txt --title "2026年第11次部门例会纪要"
python main.py                      # 交互模式
python main.py "列出 data/inbox 下的文件并总结内容"   # 通用任务（PlanAct + 工具循环）
```

## 当前缺失项（如实记录）

- Ollama / Qwen2.5-7B 模型未安装，真实模型链路尚未端到端验证
- whisper.cpp 未安装：`transcribe_audio` 会明确报缺失，可用 `load_transcript` 走文本转写稿
- Windows UIA / Linux AT-SPI 后端未安装，DesktopAgent 如实标记 `blocked`，不伪造发送成功
- MCP 服务端、A2A 协议、Chroma 向量库、端边云协同、20 条评测集、国产 CPU/OS 实机适配均未实现
- 所有离线测试使用脚本化 LLM，**指标（完成率≥70%、工具调用准确率≥85%、首 Token<1.5s）尚未实测**

## 评测指标（方案 8.2，目标值）

| 指标 | 目标 |
|---|---|
| 任务完成率（20 条评测集） | ≥70% |
| 工具调用准确率 | ≥85% |
| 首 Token 延迟 | <1.5s |
| 内存峰值 | <6GB |
