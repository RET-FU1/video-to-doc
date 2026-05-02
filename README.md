# Video-to-Doc

一键将网络视频（B站、YouTube 等）下载、转写为文字文档，并用 AI 自动生成内容总结。

## 功能

- **图形界面** — 双击启动，粘贴链接即可，支持多链接队列处理
- **视频下载** — 基于 yt-dlp，支持 B站、YouTube 等 1000+ 平台
- **语音转文字** — 基于 faster-whisper，本地 GPU 加速，无需联网
- **AI 总结** — 兼容 OpenAI 接口（默认 DeepSeek），支持多种总结风格
- **多格式输出** — 转写和总结可同时输出 `.md` `.txt` `.html`
- **播放列表** — 支持合集/播放列表批量处理
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

### 3. 配置 API Key（AI 总结）

```bash
cp .env.example .env
```

编辑 `.env`，填入 API Key：

```
API_KEY=sk-your-api-key
```

默认使用 **DeepSeek** API（国内可直接访问）。在 `config.yaml` 的 `summarizer` 段可切换：

| 服务商 | base_url | model |
|--------|----------|-------|
| **DeepSeek**（默认） | `https://api.deepseek.com` | `deepseek-v4-pro` / `deepseek-v4-flash` |
| DeepSeek (Anthropic) | `https://api.deepseek.com/anthropic` | 同上 |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-turbo` |
| 月之暗面 | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |

> **注意：** `deepseek-chat` 和 `deepseek-reasoner` 将于 2026/07/24 弃用。

如果不需要 AI 总结，跳过此步骤。转写功能不依赖 API。

### 4. 使用

**图形界面（推荐）：**

Windows 用户双击 `启动.vbs` 即可。其他系统：

```bash
venv/bin/python gui.py    # Mac / Linux
venv\Scripts\python gui.py  # Windows（命令行）
```

GUI 功能：
- 多行输入框，每行一个视频链接，支持多链接队列处理
- 可勾选输出格式：`.md` `.txt` `.html`
- 一键打开输出目录

**命令行：**

```bash
# 激活虚拟环境
# Windows
venv\Scripts\activate
# Mac/Linux
source venv/bin/activate

# 下载并转写单个视频
python main.py "https://www.bilibili.com/video/BV1xx411x7xx"

# 指定输出格式（逗号分隔）
python main.py "https://example.com/video" --output-formats md,txt,html

# 处理播放列表/合集
python main.py "https://www.youtube.com/playlist?list=xxx" --playlist

# 指定总结风格
python main.py "https://example.com/video" --summary-style knowledge_points
```

## 输出结构

```
output/
└── {标题}/
    ├── video.mp4         # 原始视频
    ├── video.md          # 转写文档
    ├── video.txt         # （可选）
    ├── video.html        # （可选）
    ├── summary.md        # AI 总结
    ├── summary.txt       # （可选）
    ├── summary.html      # （可选）
    └── .pipeline_state   # 进度状态（自动管理）
```

## 总结风格

| 风格 | 说明 |
|------|------|
| `auto` | 全面总结：核心主题、主要观点、关键结论（默认） |
| `knowledge_points` | 提取知识点：结构化列出概念名称、解释 |
| `steps` | 提取步骤：操作方法按顺序列出，含注意事项 |
| `core_ideas` | 核心观点：不超过 10 条，每条一句话 |

## 输出格式

| 格式 | 说明 |
|------|------|
| `.md` | Markdown 原文（默认） |
| `.txt` | 纯文本，去 Markdown 标记 |
| `.html` | 网页格式，带样式，可直接浏览器打开 |

## 配置

编辑 `config.yaml`：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `whisper.model` | 模型大小 | `large-v3-turbo` |
| `whisper.device` | 推理设备 | `cuda` |
| `whisper.language` | 转写语言 | `zh` |
| `summarizer.base_url` | API 地址 | DeepSeek |
| `summarizer.model` | 模型名 | `deepseek-v4-pro` |
| `summarizer.output_formats` | 输出格式 | `[md]` |
| `downloader.format` | 视频质量 | `bestvideo[height<=1080]+bestaudio/best` |
| `downloader.cookies_file` | Cookie 文件 | 空 |

### 模型选择

| 模型 | 速度 | 准确率 | 适用场景 |
|------|------|--------|----------|
| `tiny` | 极快 | 一般 | 实时转写 |
| `small` | 快 | 还行 | 快速预览 |
| `large-v3-turbo` | 较快 | 很高 | **日常使用（推荐）** |
| `large-v3` | 慢 | 最高 | 追求极致 |

## 常见问题

**Q: 启动 GUI 无反应？**
A: 确保已运行 `python setup.py` 完成初始化。用命令行 `venv\Scripts\python gui.py` 启动可看到错误信息。

**Q: GPU 不可用？**
A: 自动回退到 CPU。确保已安装 NVIDIA 驱动，且 `nvidia-cublas-cu12` 已安装（setup.py 会自动安装）。

**Q: 下载失败？**
A: 更新 yt-dlp：`venv/Scripts/pip install -U yt-dlp`

**Q: B站视频下载失败？**
A: 在 `config.yaml` 中设置 `downloader.cookies_file` 指向浏览器导出的 cookies.txt

**Q: 下载播放列表只下了第一个视频？**
A: 确保加了 `--playlist` 参数，或在 GUI 中勾选"播放列表/合集"。

## 技术栈

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — 视频下载
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — 语音转文字（CTranslate2 推理引擎）
- [ModelScope](https://modelscope.cn) — 模型下载
- OpenAI 兼容 API — AI 总结（默认 DeepSeek）
- tkinter — 图形界面（Python 内置）

## 许可

MIT
