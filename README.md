# 端侧信创办公智能体（office-agent）

完全跑在 PC 端的政企办公 Agent：端侧 7B 量化模型 + 多智能体协作，
完成「文档处理 → 会议纪要 → 公文撰写 → 邮件发送」的办公闭环，数据不出本机。

完整技术方案见 `docs/tech-plan.md`。

## 架构

- **core/** — Agent Harness：主循环、Ollama 客户端、工具注册表（MCP 风格）、
  输出解析、Prompt 构造
- **agents/** — Planner（PlanAct + 动态重规划）、会议/文档/邮件/桌面专职 Agent
- **tools/** — 文件工具集（沙箱边界），后续：doc/audio/email/desktop
- **memory/** — 双层记忆：SQLite 结构化记忆（审计日志+偏好）+ 向量库（待接入）
- **protocols/ edge/ eval/ adapt/** — MCP 服务端、A2A、端边协同、评测、国产适配（路线图阶段 2-4）

## 快速启动

```powershell
# 1. 把模型/缓存重定向到 D 盘（一次性，防 C 盘爆掉）
powershell -ExecutionPolicy Bypass -File scripts\set_disk_env.ps1
# 然后重开终端

# 2. 安装 Ollama（Windows）：https://ollama.com/download
ollama pull qwen2.5:7b-instruct-q4_K_M   # ~4.7GB，落在 D:\Projects\.cache\ollama

# 3. 环境（Python 3.10+）
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 4. 自检
python main.py --check

# 5. 跑起来
python main.py                          # 交互模式
python main.py "列出 data/inbox 里的文件并总结内容"
```

## 离线测试（不需要 Ollama）

```powershell
python tests\test_harness_offline.py
```

用 FakeLLM 驱动完整 harness 循环，验证工具调用、错误回灌、步数控制。

## 评测指标（方案 8.2）

| 指标 | 目标 |
|---|---|
| 任务完成率（20 条评测集） | ≥70% |
| 工具调用准确率 | ≥85% |
| 首 Token 延迟 | <1.5s |
| 内存峰值 | <6GB |
