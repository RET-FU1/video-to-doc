# CLAUDE.md

## 项目概述

Video-to-Doc 是一键视频/音频转文档工具：下载 → Whisper 语音转文字 → LLM 标点分段 → AI 总结 → 多格式输出。

- **入口**: `main.py`（CLI）、`gui.py`（tkinter 图形界面）
- **配置**: `config.yaml`（运行时参数）、`.env`（API Key）
- **依赖**: yt-dlp、faster-whisper、openai、pyannote.audio（可选）
- **Python**: 3.10+

## 核心模块与职责

| 文件 | 职责 |
|------|------|
| `main.py` | CLI 入口，argparse 解析参数，调用 Pipeline |
| `gui.py` | tkinter 图形界面，多任务队列，subprocess 调用 main.py |
| `pipeline.py` | **编排器** — 串联下载→转写→抛光→总结，支持断点续跑 |
| `downloader.py` | yt-dlp 封装，在线下载 + 本地文件导入 |
| `transcriber.py` | faster-whisper 语音转文字 + 可选 pyannote 说话人分离 |
| `summarizer.py` | OpenAI 兼容 API 总结器，5 种风格，长文本自动分段+汇总，含 polish() 标点分段 |
| `format_converter.py` | Markdown → txt/html 转换，html 基于 md2html 模板 |
| `utils.py` | 文件名清理、ffmpeg 查找、venv 路径、断点状态管理、文本分段（含带重叠的切分） |
| `template.html` | md2html 模板（CSS/JS），暗色模式、TOC 导航、代码复制 |
| `config.yaml` | 默认配置（Whisper 参数、API 设置、输出格式等） |
| `.env` | API Key（`API_KEY=sk-xxx`），不入 git |

## 数据流

```
URL/本地文件
  → Downloader.download()      → {标题}.mp4 + meta
  → Transcriber.transcribe()   → {标题}.txt（原始转写）
  → Pipeline._polish_transcript() → LLM 标点+分段（并行 + 重叠上下文）
  → save_formats(转写, formats)   → {标题}.md/html/txt
  → Summarizer.summarize()     → 总结文本（Markdown）
  → save_formats(总结, formats)   → {标题}-总结.md/html/txt
  → Pipeline._collect_outputs()   → 转写汇总/ + 总结汇总/（仅批量模式）
```

每步完成后记录状态到 `.pipeline_state`，中断后可从断点继续。

## 关键约定

### 输出结构
- 单视频: `output/{标题}/{标题}.{fmt}` + `{标题}-总结.{fmt}`
- 播放列表/文件夹: `output/{合集名}/{视频标题}/...` + `转写汇总/` + `总结汇总/`
- 原始转写中间文件统一为 `.txt`（非用户输出）
- 最终输出格式由用户选择（md/txt/html），通过 `save_formats()` 统一生成

### 转写
- 模型: `faster-whisper-large-v3-turbo`，GPU CUDA/float16，回退 CPU/int8
- 语言: `config.yaml` → `whisper.language`（默认 auto 自动检测）
- 输出: 无标点的纯文本片段，标点和分段交给 LLM 后处理
- 说话人分离: `diarization.enabled: true` + pyannote.audio + HF_TOKEN
- 音频提取: `_ensure_audio()` 上下文管理器统一处理，自动清理临时文件
- 前置检查: `_preflight_diarization()` 在真正处理前检查 HF_TOKEN 和模型授权状态

### 总结
- Provider: OpenAI 兼容接口（默认 DeepSeek `deepseek-v4-pro`）
- 5 种风格: `auto` / `knowledge_points` / `steps` / `core_ideas` / `expert`
- 长文本: 超过 `max_chunk_chars`（80000）自动分段总结后汇总
- 抛光: `polish()` 方法调用 LLM 添加标点+分段；大文本自动切分 + 并行处理（ThreadPoolExecutor 最多 4 并发）+ 重叠上下文（300 字符）避免边界标点错误
- 独立抛光模型: `polish_model` 配置项可指定便宜模型（如 flash），留空则复用主模型

### HTML 模板
- `template.html` 来自 md2html 项目，使用 `{{PLACEHOLDER}}` 占位符
- `format_converter.py` 的 `_build_toc_and_body()` 解析 MD 并填充模板
- 当文档无 H2/H3 标题时（如纯转写），JS 自动隐藏 TOC 侧栏并切换全宽布局
- CSS 自定义属性在 `:root` 和 `[data-theme="dark"]` 中定义

### GUI
- `gui.py` 通过 subprocess 调用 `main.py`，实时捕获 stdout 显示在日志区
- 多任务串行执行，每任务独立进程
- 日志颜色: 黄色=警告，红色=错误，绿色=成功，蓝色=阶段标记

### 断点续跑
- 状态文件 `.pipeline_state` 记录: `"downloaded"` → `"transcribed"` → `"done"`
- `get_state(folder)` / `set_state(folder, state)` 在 `utils.py`
- Transcriber: 检查 `.txt` 文件存在 + 状态 ≥ "transcribed" 则跳过
- Downloader: `is_done()` 检查状态 ∈ {downloaded, transcribed, done}

## 行为准则

**Tradeoff:** 以下准则偏向谨慎。微不足道的任务可用自己的判断。

### 1. 先想再做

**不假设、不隐藏困惑、暴露权衡。**

实施之前:
- 明确陈述你的假设。如果不确定，询问。
- 如果存在多种解释，呈现它们 — 不要默默选择。
- 如果存在更简单的方法，说出来。在有理由时反对。
- 如果某事不清楚，停下来。说出困惑之处。询问。

### 2. 简单至上

**解决问题的最少代码。不写投机性代码。**

- 不添加超出需求的特性。
- 不为一次性的代码创建抽象。
- 不做未被要求的"灵活性"或"可配置性"。
- 不做不可能发生场景的错误处理。
- 如果你写了 200 行但 50 行就能解决，重写它。

### 3. 精准修改

**只动必须碰的。只清理自己造成的杂乱。**

编辑现有代码时:
- 不要"改进"相邻的代码、注释或格式。
- 不重构没坏的东西。
- 匹配现有风格，即使你会做得不同。
- 如果你注意到无关的死代码，提出来 — 不要删除它。
- 删除你的修改造成的未使用导入/变量/函数。

### 4. 目标驱动

**定义成功标准。循环直到验证通过。**

强成功标准让你能独立循环。弱标准（"让它工作"）需要不断澄清。
