# Video-to-Doc

一键将网络视频（B站、YouTube 等）或本地视频/音频文件转写为文字文档，并用 AI 自动生成内容总结。

## 功能

- **图形界面** — 双击启动，粘贴链接即可，支持实时彩色日志、随时停止
- **视频下载** — 基于 yt-dlp，支持 B站、YouTube 等 1000+ 平台，可仅下载不做转写
- **本地文件** — 支持本地视频/音频文件，也支持整个文件夹批量处理
- **语音转文字** — 基于 faster-whisper，本地 GPU 加速，无需联网
- **标点与分段** — LLM 自动为转写文本添加标点符号并按语义分段
- **翻译** — 外文视频自动翻译为中文，生成汉化文档
- **字幕** — 生成 SRT 字幕文件，可配合翻译生成中文字幕
- **说话人分离** — 可选 pyannote.audio，区分多人对话并标记发言人
- **AI 总结** — 兼容 OpenAI 接口（默认 DeepSeek），5 种总结风格，强调准确性，忠于原文不编造
- **多格式输出** — 转写和总结可按需输出 `.md` `.txt` `.html`，HTML 支持暗色模式、TOC 导航、自适应全宽
- **播放列表** — 支持 B站合集、YouTube 播放列表等批量处理
- **断点续跑** — 中断后重新运行自动跳过已完成步骤
- **环境诊断** — `python main.py --check` 一键检查 ffmpeg、模型、API、GPU
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
- 勾选模式：播放列表/合集、文件夹模式、仅下载、说话人分离、翻译、字幕、跳过总结
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

# 翻译 + 生成中文字幕
python main.py "https://example.com/video" --translate --srt

# 仅转写，跳过总结
python main.py "https://example.com/video" --skip-summary

# 启用说话人分离
python main.py "https://example.com/video" --diarize

# 跳过下载（已有视频文件，直接转写）
python main.py "./output/视频标题/视频标题.mp4" --skip-download

# 环境诊断
python main.py --check
```

## 输出结构

**单视频：**

```
output/
└── {标题}/
    ├── {标题}.mp4         # 原始视频（在线）或副本（本地）
    ├── {标题}.md          # 转写文档（已加标点分段，仅选中 md 时）
    ├── {标题}.html        # 转写文档（仅选中 html 时）
    ├── {标题}.srt         # SRT 字幕（启用字幕时）
    ├── {标题}-总结.md      # AI 总结（仅选中 md 时）
    └── {标题}-总结.html    # AI 总结（仅选中 html 时）
```

中间文件（`.txt`、`.pipeline_state`、`_zh.txt`、`_segments.json`）在任务完成后自动清理。

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
    ├── 转写汇总/          # 所有视频的转写文件集中于此
    │   ├── {视频1标题}.md
    │   └── {视频2标题}.md
    └── 总结汇总/          # 所有视频的总结文件集中于此
        ├── {视频1标题}-总结.md
        └── {视频2标题}-总结.md
```

所有文件均以视频标题命名，可直接拷贝汇总管理而不会重名覆盖。

## 总结风格

| 风格 | CLI 参数 | 说明 |
|------|---------|------|
| 全面总结 | `auto` | 精炼文章式总结：核心观点 → 论证展开 → 关键收获（默认） |
| 知识点提取 | `knowledge_points` | 结构化列出全部知识点，含概念解释、重要性说明、原文例子 |
| 操作步骤 | `steps` | 按顺序拆解步骤：做什么、为什么必要、怎么做、常见坑点 |
| 核心观点 | `core_ideas` | 洞察提炼：拒绝话题罗列，每条都是让人「原来如此」的观点 |
| 专家深度 | `expert` | 世界级专家视角，自我核查事实，锐利批判思维，不编造不逢迎 |

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
| `whisper.language` | 转写语言 | `auto`（自动检测） |
| `whisper.device` | 推理设备 | `cuda` |
| `whisper.compute_type` | 精度 | `float16`（GPU）/ `int8`（CPU） |
| `summarizer.provider` | API 类型 | `openai` |
| `summarizer.base_url` | API 地址 | DeepSeek |
| `summarizer.model` | 模型名 | `deepseek-v4-pro` |
| `summarizer.polish_model` | 标点分段专用模型，留空复用 model | `deepseek-v4-flash` |
| `summarizer.max_chunk_chars` | 长文本分段阈值 | `80000` |
| `summarizer.max_tokens` | 单次回复最大 token | `4096` |
| `summarizer.timeout` | API 超时（秒） | `300` |
| `summarizer.max_retries` | API 失败重试次数 | `3` |
| `summarizer.summary_style` | 默认总结风格 | `auto` |
| `summarizer.output_formats` | 输出格式 | `[md]` |
| `diarization.enabled` | 启用说话人分离 | `false` |
| `diarization.min_speakers` | 最少说话人数 | `2` |
| `diarization.max_speakers` | 最多说话人数 | `5` |
| `translation.target_lang` | 翻译目标语言 | `zh`（中文） |
| `downloader.format` | 视频质量 | `bestvideo[height<=1080]+bestaudio/best` |
| `downloader.cookies_file` | Cookie 文件路径 | 空 |
| `downloader.timeout` | 下载超时（秒） | `7200` |

## 说话人分离（可选）

自动区分多人对话并标记发言人（如 SPEAKER_00、SPEAKER_01）。每个片段附带时间戳。

### 前置条件

说话人分离需要 HuggingFace 账号和 Token。**无需付费**，全程约 3 分钟完成一次性配置。

### 第 1 步：获取 HuggingFace Token

1. 访问 [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) 注册/登录
2. 点击 **"Create new token"**，类型选 **Read**，名称随意
3. 复制生成的 token（格式 `hf_xxx...`），写入项目 `.env` 文件：
   ```
   HF_TOKEN=hf_xxx
   ```

### 第 2 步：接受模型用户协议（3 个）

说话人分离依赖 3 个模型，**每个都需要单独在网页上点击"Agree"**：

| 模型 | 授权页面 | 说明 |
|------|---------|------|
| speaker-diarization-3.1 | [打开页面](https://huggingface.co/pyannote/speaker-diarization-3.1) | 主说话人分离模型 |
| segmentation-3.0 | [打开页面](https://huggingface.co/pyannote/segmentation-3.0) | 语音活动检测（VAD） |
| speaker-diarization-community-1 | [打开页面](https://huggingface.co/pyannote/speaker-diarization-community-1) | 说话人嵌入聚类 |

> 每个页面点击 **"Agree and access repository"** 即可。姓名、机构随意填写，不会被验证。

**注意**：程序启动时会自动检查这 3 个模型的授权状态。未授权时会在日志中给出明确提示和对应链接，不会静默失败。

### 第 3 步：安装依赖

```bash
pip install pyannote.audio
```

### 第 4 步：启用

在 `config.yaml` 中：

```yaml
diarization:
  enabled: true
  hf_token: ""              # 留空则从 .env 的 HF_TOKEN 读取
  min_speakers: 2           # 预估最少说话人数
  max_speakers: 5           # 预估最多说话人数
```

或在命令行添加 `--diarize` 参数临时启用。

### 输出示例

```markdown
# 视频标题

## SPEAKER_00 (00:00:00.000 - 00:01:23.456)
主持人开场介绍今天的主题。

## SPEAKER_01 (00:01:23.456 - 00:03:45.678)
我来分享一下技术架构的设计思路。
```

### 故障排查

**Q: 提示"模型尚未授权"？**
A: 检查是否遗漏了某个模型的协议（共 3 个，见上表）。日志中会给出具体是哪个模型和授权链接。

**Q: 前置检查失败但转写仍然继续？**
A: 这是预期行为。说话人分离是可选的增强功能，配置不完整时会自动回退到基础转写，不会影响转写本身。

**Q: 说话人标签为什么是 SPEAKER_00 而非真实人名？**
A: pyannote 只能区分"不同的人"，无法识别具体身份。要标注真实人名需要提前注册声纹样本（说话人识别），这是另一个领域的功能。

**Q: 两人同时说话能分开吗？**
A: 不能。重叠语音分离（speech separation）是目前学术界的开放难题，pyannote 不支持。

**Q: 短句或背景噪音大时说话人标错？**
A: 可尝试调整 `min_speakers` / `max_speakers` 参数。单人视频设 `max_speakers: 1` 可避免过度切分。

## 常见问题

**Q: 启动 GUI 无反应？**
A: 确保已运行 `python setup.py` 完成初始化。用命令行 `venv\Scripts\python gui.py` 启动可看到错误信息。

**Q: GPU 不可用？**
A: 自动回退到 CPU 并在日志中显示具体原因（如缺少 cuBLAS DLL、驱动版本不匹配等）。常见解决：确保 NVIDIA 驱动已安装，运行 `python main.py --check` 诊断。

**Q: 下载失败？**
A: 更新 yt-dlp：`venv/Scripts/pip install -U yt-dlp`

**Q: B站视频下载失败？**
A: 在 `config.yaml` 中设置 `downloader.cookies_file` 指向浏览器导出的 cookies.txt

**Q: 播放列表只下载了第一个？**
A: 确保加了 `--playlist` 参数，或在 GUI 中勾选"播放列表/合集"。

**Q: 转写文本标点不准？**
A: 标点由 LLM 自动添加，默认使用与总结相同的模型。可在 `config.yaml` 中设置 `summarizer.polish_model` 切换专用模型（推荐 flash 等便宜模型降成本），或更换 `summarizer.model` 提高质量。纯本地转写不含标点。

## 技术栈

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — 视频下载
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — 语音转文字（CTranslate2 推理引擎）
- [pyannote.audio](https://github.com/pyannote/pyannote-audio) — 说话人分离（可选）
- [ModelScope](https://modelscope.cn) — 模型下载
- OpenAI 兼容 API — LLM 标点分段 + AI 总结（默认 DeepSeek）
- [md2html](https://github.com/haidang1810/md2html) — HTML 输出模板（暗色模式、TOC 侧栏、代码复制）
- tkinter — 图形界面（Python 内置）

## 许可

MIT
