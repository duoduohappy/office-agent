# 阶段 3 国产适配：范围、已做项与实机验证清单

> **先读结论**：截至本次提交，HAL 抽象层、aarch64 依赖审计、交叉编译脚本、
> 目标机兼容性探针均已实现并在 x86_64 Windows 开发机上真实跑通；
> **但麒麟 / UOS 实机、飞腾 / 鲲鹏 ARM 真机、WPS Linux 版式验证三项均未执行**。
> 本项目当前不能对外表述为「已完成国产适配」或「通过信创兼容性验证」。

## 一、方案 7.4 四步骤与当前状态

| 步骤 | 方案要求 | 已完成 | 未完成 |
|---|---|---|---|
| 1 开发期可移植性 | 上层不调 Windows 专有 API，经抽象层访问系统能力 | `adapt/hal.py`；`eval/metrics.py` 内存采样、`tools/doc_tools.py` 字体、`agents/desktop_agent.py` GUI 探测、`main.py` 环境变量读取均已接入；Windows 行为与两轮评测保持一致 | — |
| 2 编译验证 | ARM Linux 上交叉编译 llama.cpp，跑通 Qwen2.5-7B Q4 | CMake toolchain、构建脚本、aarch64 wheel 可用性审计（真实执行） | 本机无 aarch64 工具链 / ARM Linux / QEMU，**交叉编译与推理探针未执行** |
| 3 真机适配 | 飞腾 D3000M + 麒麟 OS 性能调优 | 目标机一键探针 `adapt/compat_check.py`、字体/AT-SPI/桌面环境抽象 | 无真机，性能基线（内存、TTFT、GUI 稳定性）未采集 |
| 4 生态验证 | WPS Linux / 永中 Office 文档读写兼容 | OOXML 标准解析器回读校验（docx/xlsx 真实生成并回读） | 未安装 WPS Linux，**版式渲染一致性未比对**，需人工目视验收 |

## 二、为什么「纯 Python 项目」没有可交叉编译的东西

方案 7.4 步骤 2 的编译对象是**推理后端 llama.cpp / Ollama（C++）**，不是本项目代码。
因此 `scripts/cross_build_llamacpp.sh` 生成的是给构建机用的 llama.cpp 交叉编译流程，
本仓库自身产出的是跨平台 `.py` 与 `py3-none-any` wheel。

真实执行的 aarch64 审计结论（`adapt/reports/aarch64_wheel_audit.json`）：

- `python-docx` / `openpyxl` / `python-pptx` / `pypdf` → `py3-none-any`，与架构无关，**不是 ARM 编译产物**
- `chromadb 1.5.9` → `cp39-abi3-manylinux_2_17_aarch64`，是真正的 ARM 原生 wheel
- 审计用了 `--no-deps`，**依赖链仍需目标机完整 `pip install` 复验**；尤其是 Chroma 的传递依赖

一个必须提前处理的阻塞项：麒麟 V10 / UOS V20 自带 Python 常为 3.7/3.8，
本项目语法下限是 **3.10**，目标机需另装 CPython 3.10+。

## 三、目标机操作清单（拿到飞腾/鲲鹏机器后按序执行）

```bash
# 0. 基础信息
uname -m && cat /etc/os-release && python3 --version

# 1. 安装 CPython >= 3.10 与依赖
python3.10 -m venv .venv
.venv/bin/pip install -r requirements-aarch64.txt \
    -i https://mirrors.cloud.tencent.com/pypi/simple/

# 2. 跑兼容性探针，生成 PASS/WARN/FAIL/SKIP 报告
.venv/bin/python -m adapt.compat_check --out data/outbox/kylin-v10

# 3. 交叉编译推理后端（在带 aarch64 工具链的 Linux 构建机上）
LLAMA_CPP_SRC=/path/to/llama.cpp bash scripts/cross_build_llamacpp.sh
#    把 build-aarch64/bin/llama-cli 拷到目标机后跑最小推理：
#    ./llama-cli -m qwen2.5-7b-instruct-q4_k_m.gguf -p '国产化适配测试' -n 32

# 4. 纯 CPU 口径复测（消掉历史报告 GPU 混合 offload 的口径问题）
.venv/bin/python -m eval.run_eval

# 5. WPS 版式人工验收
#    用 WPS 文字打开公文纪要 docx：
#    标题二号小标宋、正文三号仿宋、行距 28 磅、页边距 3.7/3.5/2.8/2.6 cm、
#    落款「办公室」；截图留证。命令行转换不能替代目视验收。
```

### 判据

| 项 | 通过条件 |
|---|---|
| OS / 架构 | `compat_baseline.json` 中 `real_target_verified=true`，且 arch=aarch64、distro_id 为 kylin/uos |
| 字体 | fc-list 能找到 方正仿宋_GBK / 方正小标宋_GBK 等等价字体；否则公文回退宋体，版式判不通过 |
| GUI | `pyatspi` 可导入，`XDG_CURRENT_DESKTOP` 为 UKUI/DDE，UI 树可读 |
| WPS | 人工在 WPS 打开后版式与 GB/T 9704-2012 一致，且 xlsx 待办表可正常打开 |
| 性能 | 纯 CPU 下 `eval.run_eval` 的内存峰值 <6GB、TTFT <1.5s（含冷启动样本） |
| 推理 | llama-cli 在目标机输出非空结果，且 `file` 确认是 aarch64 ELF |

## 四、对外话术边界

可以说：

> 「阶段 3 做了 HAL 抽象，把内存采样、字体、GUI 后端、环境变量读取收敛到统一接口；
> aarch64 依赖审计确认 4 个文档包是通用 wheel、Chroma 有 ARM 原生 wheel；
> 交叉编译脚本和麒麟/UOS/WPS 兼容性探针已就绪。」

**不能说**：已完成国产适配 / 已在麒麟 UOS 验证通过 / ARM 交叉编译完成 / WPS 兼容已验证 / `<6GB` 在信创环境达标。

`adapt/reports/compat_baseline.md` 是开发机基线，本机结果为 **PASS 4 / SKIP 8 / real_target_verified=false**，
SKIP 一律表示「本环境无法验证」，不是通过。

## 五、与评测体系的关系

本阶段**没有改动** `eval/benchmark_tasks.json` 的 `requires`、grading 规则、关键字或 70% 阈值，
新模块也未注册进评测器的 ToolRegistry。两轮真实评测（50% / 75%）仍可复现，
国产适配相关能力**不计入任何验收成绩**。要纳入成绩必须另有评测集设计与全量重跑。
