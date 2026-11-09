# 国产适配兼容性报告

- 生成时间：2026-11-09 21:20:19
- 平台：`Windows 11` / `x86_64` / Python `3.13.14` / 发行版 `未识别` / 桌面 `windows`
- 目标机真实验证：**否**（模拟平台=False）
- 交叉编译已执行：否；ARM 推理已验证：否
- 汇总：PASS 4 / WARN 0 / FAIL 0 / **SKIP 8**

> SKIP 表示本环境无法验证，不是通过。只有同时满足「非模拟探测 + 麒麟/UOS Linux + aarch64」时 real_target_verified 才为 true；并且仍需人工在 WPS 中打开公文做版式目视验收。

## 逐项结果

| 项 | 状态 | 说明 |
|---|---|---|
| 信创 OS 识别 | SKIP | 当前为开发机 Windows 11，需在麒麟 V10 / UOS V20 目标机执行 |
| CPU 架构 | SKIP | 归一化架构 x86_64（原始 AMD64） |
| Python 版本 | PASS | 3.13.14；本项目语法下限 3.10，麒麟/UOS 自带 3.7/3.8 通常需另装 |
| 核心文档依赖可导入 | PASS | python-docx / openpyxl / python-pptx / pypdf |
| Chroma 向量库 | PASS | 真实 chromadb 可导入 |
| 公文中文字体 | SKIP | 开发机按 OOXML 字体族名写入 仿宋_GB2312；目标机需另验方正仿宋_GBK / FandolFangSong |
| AT-SPI / UKUI / DDE 桌面自动化 | SKIP | 开发机后端 windows-uia（available=False）；需在麒麟 UKUI / UOS DDE 实机验证 |
| WPS Linux / 永中 Office 安装 | SKIP | 未检测到 wps/et/wpp/永中，无法做国产办公软件渲染兼容性验证 |
| 公文 docx / 待办 xlsx 回读 | PASS | 用 python-docx / openpyxl 回读本项目的 OOXML 产出；仅证明标准解析器可读，不等价于 WPS 渲染一致 |
| WPS 版式渲染比对 | SKIP | 未检测到 WPS 或 LibreOffice 命令行转换接口，未做版式渲染比对 |
| 端侧推理后端 | SKIP | 仅探测可执行文件，未验证模型推理；ARM 实机需另跑 llama.cpp 探针 |
| 纯 CPU 内存 / TTFT 基线 | SKIP | 方案 8.2 的 <6GB 与 <1.5s 需在纯 CPU 信创机复测；开发机为 GPU 混合 offload，历史报告数值不作为信创口径 |

## 交叉工具链

- 检测到：无
- 缺失：aarch64-linux-gnu-gcc, aarch64-linux-gnu-g++, cmake, ninja, clang, zig, docker, qemu-aarch64-static, file
- GNU 交叉工具链可用：False；本机能交叉编译：False

## 边界

- WPS 版式一致性仍需人工在图形界面打开公文目视核对，命令行转换不能替代；
- `py3-none-any` wheel 的 aarch64 可用性不等于依赖链可安装，需目标机完整 `pip install` 复验；
- 本报告不改动 20 条评测集的 grading / requires / 阈值，不计入任何验收成绩。
