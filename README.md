# Video-to-Doc

一键将网络视频（B站、YouTube 等）或本地视频/音频文件转写为文字文档，并用 AI 自动生成内容总结。

## 功能

- **图形界面** — 双击启动，粘贴链接即可，支持多任务队列、实时彩色日志、随时停止
- **视频下载** — 基于 yt-dlp，支持 B站、YouTube 等 1000+ 平台，可仅下载不做转写
- **本地文件** — 支持本地视频/音频文件，也支持整个文件夹批量处理
- **语音转文字** — 基于 faster-whisper，本地 GPU 加速，无需联网
- **标点与分段** — LLM 自动为转写文本添加标点符号并按语义分段
- **说话人分离** — 可选 whisperX 引擎，区分多人对话并标记发言人
- **AI 总结** — 兼容 OpenAI 接口（默认 DeepSeek），4 种总结风格，强调洞察提炼和原文亮点保留
- **多格式输出** — 转写和总结可按需输出 `.md` `.txt` `.html`，HTML 支持暗色模式、TOC 导航、自适应全宽
- **播放列表** — 支持 B站合集、YouTube 播放列表等批量处理
- **断点续跑** — 中断后重新运行自动跳过已完成步骤
- **跨平台** — Windows / Mac / Linux

## 系统要求

- Python 3.10+
- ffmpeg（音频提取）
- NVIDIA 显卡（推荐，CPU 也可运行但较慢）

## 快速开始

### 1. 安装 ffmpeg

```bash
# Windows
winget install Gyan.FFmpeg

# Mac
brew install ffmpeg

# Linux
sudo apt install ffmpeg
```

### 2. 克隆并初始化

```bash
cd video-to-doc
python setup.py
```

首次运行会下载 Whisper 模型（约 1.6GB），仅此一次。

### 3. 配置 API Key

```bash
cp .env.example .env
```

编辑 `.env`，填入一行：

```
API_KEY=sk-your-api-key
```

AI 总结和标点分段功能依赖 API。转写功能不依赖 API（仅 faster-whisper 本地运行）。

默认使用 **DeepSeek** API（国内可直接访问，[获取 Key](https://platform.deepseek.com/api_keys)）。在 `config.yaml` 的 `summarizer` 段可切换其他服务商：

| 服务商 | base_url | model 示例 |
|--------|----------|-----------|
| **DeepSeek**（默认） | `https://api.deepseek.com` | `deepseek-v4-pro` / `deepseek-v4-flash` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-turbo` |
| 月之暗面 | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| Ollama 本地 | `http://localhost:11434/v1` | 本地模型名 |

### 4. 使用

**图形界面（推荐）：**

Windows 双击 `启动.vbs`（无窗口）或 `启动.bat`（有终端）。其他系统：

```bash
venv/bin/python gui.py      # Mac / Linux
venv\Scripts\python gui.py  # Windows
```

GUI 操作：
- 输入框填写视频链接、本地文件路径或文件夹路径（每行一个）
- 勾选模式：播放列表/合集、文件夹模式、仅下载
- 选择总结风格和输出格式
- 点"开始处理"，日志区实时显示进度
- 点"打开输出目录"查看结果

**命令行：**

```bash
# 激活虚拟环境
venv\Scripts\activate      # Windows
source venv/bin/activate   # Mac/Linux

# 在线视频
python main.py "https://www.bilibili.com/video/BV1xx411x7xx"

# 本地视频文件
python main.py "C:/videos/myvideo.mp4"

# 本地音频文件
python main.py "C:/audio/podcast.mp3"

# 文件夹批量处理
python main.py --folder "C:/videos/教程合集"

# 播放列表
python main.py "https://www.youtube.com/playlist?list=xxx" --playlist

# 仅下载，不做转写
python main.py "https://example.com/video" --download-only

# 指定总结风格
python main.py "https://example.com/video" --summary-style knowledge_points

# 指定输出格式
python main.py "https://example.com/video" --output-formats md,txt,html
```

## 输出结构

**单视频：**

```
output/
└── {标题}/
    ├── {标题}.mp4         # 原始视频（在线）或副本（本地）
    ├── {标题}.txt         # 原始转写（Whisper 输出，中间文件）
    ├── {标题}.md          # 转写文档（已加标点分段，仅选中 md 时）
    ├── {标题}.html        # 转写文档（仅选中 html 时）
    ├── {标题}-总结.md      # AI 总结（仅选中 md 时）
    ├── {标题}-总结.html    # AI 总结（仅选中 html 时）
    └── .pipeline_state    # 进度状态（自动管理，断点续跑）
```

**播放列表 / 文件夹批量处理：**

```
output/
└── {合集或文件夹名}/
    ├── {视频1标题}/
    │   ├── {视频1标题}.md
    │   ├── {视频1标题}-总结.md
    │   └── ...
    ├── {视频2标题}/
    │   └── ...
    └── ...
```

所有文件均以视频标题命名，可直接拷贝汇总管理而不会重名覆盖。

## 总结风格

| 风格 | CLI 参数 | 说明 |
|------|---------|------|
| 全面总结 | `auto` | 精炼文章式总结：核心观点 → 论证展开 → 关键收获（默认） |
| 知识点提取 | `knowledge_points` | 结构化列出全部知识点，含概念解释、重要性说明、原文例子 |
| 操作步骤 | `steps` | 按顺序拆解步骤：做什么、为什么必要、怎么做、常见坑点 |
| 核心观点 | `core_ideas` | 洞察提炼：拒绝话题罗列，每条都是让人「原来如此」的观点 |

## 支持的格式

| 类型 | 扩展名 |
|------|--------|
| 视频 | `.mp4` `.mkv` `.webm` `.flv` `.avi` `.mov` |
| 音频 | `.mp3` `.wav` `.m4a` `.flac` `.ogg` `.aac` `.opus` `.wma` |

音频文件直接送入 Whisper 转写，跳过 ffmpeg 提取步骤。

## 配置

编辑 `config.yaml`：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `output_dir` | 输出目录 | `./output` |
| `whisper.language` | 转写语言 | `zh` |
| `whisper.device` | 推理设备 | `cuda` |
| `whisper.compute_type` | 精度 | `float16`（GPU）/ `int8`（CPU） |
| `summarizer.provider` | API 类型 | `openai` |
| `summarizer.base_url` | API 地址 | DeepSeek |
| `summarizer.model` | 模型名 | `deepseek-v4-pro` |
| `summarizer.max_chunk_chars` | 长文本分段阈值 | `80000` |
| `summarizer.max_tokens` | 单次回复最大 token | `4096` |
| `summarizer.summary_style` | 默认总结风格 | `auto` |
| `summarizer.output_formats` | 输出格式 | `[md]` |
| `diarization.enabled` | 启用说话人分离 | `false` |
| `diarization.min_speakers` | 最少说话人数 | `2` |
| `diarization.max_speakers` | 最多说话人数 | `5` |
| `downloader.format` | 视频质量 | `bestvideo[height<=1080]+bestaudio/best` |
| `downloader.cookies_file` | Cookie 文件路径 | 空 |
| `downloader.timeout` | 下载超时（秒） | `7200` |

## 说话人分离（可选）

需要 `pip install whisperx`，并在 `.env` 中配置 HuggingFace Token：

```
HF_TOKEN=hf_your_token
```

> 从 [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) 创建 Read token，并先到 [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1) 和 [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0) 接受用户协议。

然后在 `config.yaml` 中开启：

```yaml
diarization:
  enabled: true
```

启用后转写文档会按发言人分段标记：

```markdown
# 视频标题

## SPEAKER_00 (00:00:00.000 - 00:01:23.456)
主持人开场介绍今天的主题。

## SPEAKER_01 (00:01:23.456 - 00:03:45.678)
我来分享一下技术架构的设计思路。
```

> **注意：** whisperX 在 Python 3.12+ 可能有依赖冲突，建议 Python 3.10-3.11。

## 常见问题

**Q: 启动 GUI 无反应？**
A: 确保已运行 `python setup.py` 完成初始化。用命令行 `venv\Scripts\python gui.py` 启动可看到错误信息。

**Q: GPU 不可用？**
A: 自动回退到 CPU。确保已安装 NVIDIA 驱动，且 `nvidia-cublas-cu12` 已安装。

**Q: 下载失败？**
A: 更新 yt-dlp：`venv/Scripts/pip install -U yt-dlp`

**Q: B站视频下载失败？**
A: 在 `config.yaml` 中设置 `downloader.cookies_file` 指向浏览器导出的 cookies.txt

**Q: 播放列表只下载了第一个？**
A: 确保加了 `--playlist` 参数，或在 GUI 中勾选"播放列表/合集"。

**Q: 说话人分离不生效？**
A: (1) `pip install whisperx` (2) `.env` 中 `HF_TOKEN` 已设置 (3) `config.yaml` 中 `diarization.enabled: true`。任何环节缺失会自动回退基础转写。

**Q: 转写文本标点不准？**
A: 标点由 LLM 自动添加（DeepSeek），如质量不佳可在 `config.yaml` 中换模型。纯本地转写不含标点。

## 技术栈

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — 视频下载
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — 语音转文字（CTranslate2 推理引擎）
- [whisperX](https://github.com/m-bain/whisperX) — 说话人分离（可选）
- [ModelScope](https://modelscope.cn) — 模型下载
- OpenAI 兼容 API — LLM 标点分段 + AI 总结（默认 DeepSeek）
- [md2html](https://github.com/haidang1810/md2html) — HTML 输出模板（暗色模式、TOC 侧栏、代码复制）
- tkinter — 图形界面（Python 内置）

## 许可

MIT
