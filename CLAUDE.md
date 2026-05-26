# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Video-to-Doc 是一键视频/音频转文档工具：下载 → Whisper 语音转文字 → LLM 标点分段 → AI 总结 → 多格式输出。

- **入口**: `main.py`（CLI）、`gui.py`（tkinter 图形界面）
- **配置**: `config.yaml`（运行时参数）、`.env`（API Key）
- **依赖**: yt-dlp、faster-whisper、openai、pyannote.audio（可选）
- **Python**: 3.10+
- **无测试覆盖** — 项目目前没有自动测试，修改后需手动验证

## 常用命令

```bash
# 初始化环境
python setup.py                                  # 创建 venv + 安装依赖 + 下载模型

# 程序自动使用项目 venv，无需手动激活

# 环境诊断
python main.py --check

# 基础用法
python main.py <URL或本地文件路径>                # 单视频处理
python main.py <URL> --playlist                  # 播放列表
python main.py --folder <目录路径>               # 文件夹批量处理

# 常用选项
python main.py <URL> --skip-summary              # 仅转写，不做总结
python main.py <URL> --download-only             # 仅下载
python main.py <URL> --translate --srt           # 翻译 + 中文字幕
python main.py <URL> --srt                       # 仅生成字幕（不翻译）
python main.py <URL> --diarize                   # 启用说话人分离
python main.py <URL> -o ./my-output              # 自定义输出目录
python main.py <URL> --output-formats md,txt,html

# 启动 GUI
python gui.py
```

## 核心模块与职责

| 文件 | 职责 |
|------|------|
| `main.py` | CLI 入口，argparse 参数解析，调用 Pipeline |
| `gui.py` | tkinter 图形界面，直接导入 Pipeline 使用（非 subprocess），多任务串行执行 |
| `pipeline.py` | **编排器** — 串联下载→转写→翻译→抛光→总结→字幕，支持断点续跑 |
| `downloader.py` | yt-dlp 封装，在线下载 + 本地文件导入 |
| `transcriber.py` | faster-whisper 语音转文字 + 可选 pyannote 说话人分离 |
| `summarizer.py` | OpenAI 兼容 API 总结器，5 种风格，长文本自动分段+汇总，含 `polish()` 标点分段 |
| `translator.py` | LLM 逐行翻译器，复用总结器的 OpenAI 客户端，保持行结构对齐时间戳 |
| `subtitle_extractor.py` | 字幕解析（SRT/VTT） + 质量评估（覆盖率/噪音/密度/语言匹配） + 格式转换 |
| `subtitle.py` | 从 whisper 段落时间戳 + 翻译/原文生成 SRT 字幕，含短句合并逻辑 |
| `format_converter.py` | Markdown → txt/html 转换，html 基于 md2html 模板 |
| `utils.py` | 文件名清理、ffmpeg 查找、venv 路径、断点状态管理、文本分段（含带重叠切分） |
| `template.html` | md2html 模板（CSS/JS），暗色模式、TOC 导航、代码复制 |
| `config.yaml` | 默认配置（Whisper 参数、API 设置、字幕评估、输出格式等） |
| `.env` | API Key（`API_KEY=sk-xxx`），不入 git，由 `utils.load_env()` 加载到 `os.environ` |

## 数据流

```
URL/本地文件
  → Downloader.download()         → {标题}.mp4 + meta + [_subtitle.srt, _subtitle_info.json]
  → Pipeline._get_transcript()    → 优先字幕 → 回退 Whisper
      ├─ 字幕达标: parse_subtitle_file() → assess_quality() → write_transcript_output()
      └─ 字幕不可用: Transcriber.transcribe() → {标题}.txt + _segments.json
  → [Translator.translate()]      → {标题}_zh.txt（可选，翻译后文本用于后续步骤）
  → Pipeline._polish_transcript() → LLM 标点+分段（并行 + 重叠上下文，5000 字符自动分块）
    若启用说话人分离则走 _polish_diarized_transcript()（逐说话人独立抛光）
  → save_formats(转写, formats)   → {标题}.md/html/txt
  → Summarizer.summarize()        → 总结文本（Markdown）
  → save_formats(总结, formats)   → {标题}-总结.md/html/txt
  → [generate_srt()]              → {标题}.srt（可选，翻译模式下用译文生成字幕）
  → 清理中间文件（.txt(非输出格式时), .pipeline_state, _segments.json, _subtitle.srt, _subtitle_info.json, _zh.txt）
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
- 模型: `faster-whisper-large-v3-turbo`，ModelScope 下载缓存到 `models/`
- GPU CUDA/float16，回退 CPU/int8；`init_cuda()` 在 Windows 下自动搜索 DLL 路径
- 语言: `config.yaml` → `whisper.language`（默认 auto 自动检测）
- 输出: 无标点的纯文本片段，标点和分段交给 LLM 后处理
- 说话人分离: `diarization.enabled: true` + pyannote.audio + HF_TOKEN
- 音频提取: `_ensure_audio()` 上下文管理器统一处理，自动清理临时文件
- 前置检查: `_preflight_diarization()` 检查 HF_TOKEN 和 3 个 pyannote 模型的授权状态

### 字幕优先提取
- `downloader.py` 在视频下载后自动调用 `_try_download_subtitles()` 下载平台字幕
- 输出 `{stem}_subtitle.srt` + `{stem}_subtitle_info.json`（记录 source/language）
- `pipeline.py._get_transcript()` 决定走字幕还是 Whisper 路径
- `subtitle_extractor.py`:
  - `parse_subtitle_file()` — 自动检测 SRT/VTT，清理 HTML 标签，返回 `[{start, end, text}]`
  - `assess_quality()` — 人工字幕仅检查覆盖率 > 50%；自动字幕需全维度达标（覆盖率、噪音、密度、语言匹配）
  - `write_transcript_output()` — 写入 .txt 和 _segments.json，与 Whisper 输出格式一致
- 质量不达标或字幕不可用时自动回退 Whisper（`fallback_to_whisper: true`）
- `subtitle_extractor.py` 不导入任何第三方模块，仅 stdlib

### 说话人分离
- pyannote `speaker-diarization-3.1` 模型区分说话人，英文效果优于中文
- `min_turn_duration`（默认 1.5s）控制最短 turn：短于此且被同说话人包围的 turn 会被平滑合并
- `_smooth_turns()` 执行两步：短 turn 合并 → 相邻同说话人 turn 合并
- `_assign_speakers()` 将每个 whisper 段落匹配到重叠时长最长的 pyannote turn
- 不支持重叠语音分离，不支持真实人名标注

### 翻译（可选）
- 在抛光之前执行，翻译后的文本用于后续抛光、总结和字幕生成
- 逐行翻译保持行结构，确保与 whisper 段落时间戳一一对应
- 说话人分离模式下：保留 speaker 头不变，只翻译正文
- 长文本自动分块（每块 80 行），翻译后按原始行结构重组

### 总结
- Provider: OpenAI 兼容接口（默认 DeepSeek `deepseek-v4-pro`）
- 5 种风格: `auto` / `knowledge_points` / `steps` / `core_ideas` / `expert`
- 长文本: 超过 `max_chunk_chars`（80000）自动分段总结后汇总
- 抛光: `polish()` 调用 LLM 添加标点+分段；大文本自动切分 + 并行处理（ThreadPoolExecutor 最多 4 并发）+ 重叠上下文（300 字符）避免边界标点错误
- 独立抛光模型: `polish_model` 配置项可指定便宜模型（如 flash），留空则复用主模型
- 带说话人标签的文本: `_polish_diarized_transcript()` 按说话人独立抛光，避免跨说话人合并段落

### SRT 字幕（可选）
- 从 `_segments.json` 提取时间戳，与原文或翻译文本对齐
- 短片段自动合并（同说话人 + 持续 < max_duration）+ 说话人前缀标注
- 最多 2 行字幕避免遮挡屏幕

### HTML 模板
- `template.html` 使用 `{{PLACEHOLDER}}` 占位符
- `format_converter.py` 的 `_build_toc_and_body()` 解析 MD 并填充模板
- 当文档无 H2/H3 标题时，JS 自动隐藏 TOC 侧栏并切换全宽布局

### GUI
- 直接导入并调用 `Pipeline`（非 subprocess），后台线程执行以避免阻塞 UI
- 通过自定义 `_GuiLogHandler` 将 logging 消息桥接到 tkinter 日志窗口
- 多任务串行执行，支持随时停止
- 日志颜色: 黄色=警告/失败/跳过，红色=错误/异常，绿色=完成，蓝色=阶段标记

### venv 自动重定向
- `main.py` 和 `gui.py` 入口顶部有重定向逻辑：比较 `sys.executable` 与项目 `venv/` 中的 Python
- 不在 venv 中时自动 `subprocess.run` 用 venv Python 重新执行，然后 `sys.exit(0)`
- 使用 `Path.resolve()` 比较，避免路径格式差异导致死循环
- 校验文件都在 stdlib 中（`sys`, `pathlib.Path`, `subprocess`），不依赖第三方模块

### 输出目录
- 默认 `./output`，来自 `config.yaml` 的 `output_dir`
- CLI: `-o` / `--output-dir` 覆盖，空格和空字符串被忽略
- GUI: 输入框 + "浏览..." 按钮，存储在 `self.output_dir_var`，启动时传给 `config["output_dir"]`
- `open_output()` 也跟随用户选择的目录，空值时 fallback 到 `PROJECT_ROOT / "output"`

### 断点续跑
- 状态文件 `.pipeline_state` 记录: `"downloaded"` → `"transcribed"` → `"done"`
- `get_state(folder)` / `set_state(folder, state)` 在 `utils.py`
- Transcriber: 检查 `.txt` 文件存在 + 状态 ≥ "transcribed" 则跳过
- Downloader: `is_done()` 检查状态 ∈ {downloaded, transcribed, done}
- `_process_one()` 最后一步通过检查 `{stem}-总结.{fmt}` 是否存在判断跳过

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
