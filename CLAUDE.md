# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Video-to-Doc: downloads online/local videos, transcribes via faster-whisper (GPU-accelerated via CTranslate2), adds punctuation/segmentation via LLM, optionally identifies speakers, translates, and generates AI summaries. Outputs `.md`/`.txt`/`.html`. GUI via tkinter, CLI via argparse.

## Common commands

```bash
# Environment setup (first time only, creates venv + downloads model ~1.6GB)
python setup.py

# Environment diagnostic (ffmpeg, model, API, GPU)
python main.py --check

# CLI: single video
python main.py "https://www.bilibili.com/video/BV1xx411x7xx"

# CLI: with all options
python main.py "URL" --translate --srt --multi-speaker --summary-style expert -o ./my-output

# GUI
python gui.py

# Update yt-dlp (frequently needed)
venv/Scripts/pip install -U yt-dlp
```

Both `main.py` and `gui.py` auto-redirect to the project venv — no manual activation needed.

## Running tests

No test runner configured. Tests are plain scripts in `tests/`:

```bash
venv/Scripts/python -m pytest tests/ -v
# Or run individually:
venv/Scripts/python tests/test_utils.py
venv/Scripts/python tests/test_format_converter.py
venv/Scripts/python tests/test_subtitle.py
```

There is no automated test suite — all changes should be manually verified with `python main.py --check` and a test run.

## Architecture (big picture)

```
main.py / gui.py
    └── Pipeline (pipeline.py) — the sole orchestrator
         ├── Downloader (downloader.py) — yt-dlp wrapper
         ├── Transcriber (transcriber.py) — faster-whisper (GPU-accelerated via CTranslate2)
         ├── Summarizer (summarizer.py) — OpenAI-compatible API client
         ├── Translator (translator.py) — reuses Summarizer's client
         ├── subtitle_extractor.py — parse/assess SRT/VTT, stdlib only
         ├── subtitle.py — SRT generation from segments
         └── format_converter.py — Markdown → txt/html via mistune
```

All modules share `utils.py` for logging, GPU init, state management, filename sanitization, venv/ffmpeg path resolution.

### Processing pipeline (order matters)

1. **Download** (`Downloader.download`) → video file + metadata
2. **Transcript** (`Pipeline._get_transcript`) → subtitle-first with Whisper fallback
3. **Translate** (optional, `Translator.translate`) → line-by-line LLM translation
4. **Polish** (`Summarizer.polish` or `polish_multispeaker`) → LLM adds punctuation + paragraph breaks, optionally identifies speakers (parallel chunks with overlap)
5. **Summarize** (`Summarizer.summarize`) → 6 styles (auto/knowledge_points/steps/core_ideas/expert/custom), auto-chunking for long text
6. **SRT** (optional) → generate subtitle file
7. **Cleanup** → remove intermediate files

Translation happens **before** polish so the polished/summarized text is in the target language. This is a design decision — don't reorder without updating `_process_one()`.

### Subtitle-first strategy

Before running Whisper (expensive), `Downloader._try_download_subtitles()` checks for platform subtitles (YouTube/B站). `subtitle_extractor.assess_quality()` evaluates coverage, noise, density, and language match. Manual subtitles only need ≥50% coverage; auto-generated subtitles must pass all 4 checks. If subtitles fail quality → fall back to Whisper.

### Checkpoint/resume

`.pipeline_state` files in each output folder track progress: `""` → `"downloaded"` → `"transcribed"` → `"done"`. Each stage checks this before redoing work. If you add/modify pipeline steps, update the state logic in all three files that touch it (`downloader.py`, `transcriber.py`, `pipeline.py`).

### Multi-speaker identification

When enabled (`--multi-speaker` or `config.yaml` `summarizer.multi_speaker: true`):
- Polish step uses `summarizer.polish_multispeaker()` with an augmented prompt that asks the LLM to identify speakers from conversational context and timestamps
- Output uses inline `说话人A：` / `说话人B：` labels
- No special pipeline paths needed — standard translate/summarize work naturally with the labeled text

## Key conventions

- **All paths**: `pathlib.Path`, never `os.path`
- **venv redirect**: Must be the very first code in `main.py`/`gui.py` (before any imports beyond `sys`/`pathlib.Path`). Uses `Path.resolve()` to avoid infinite loops from case/path differences.
- **API keys**: Loaded from `.env` via `utils.load_env()` → `os.environ["API_KEY"]`, never hardcoded
- **Logging**: All modules use `logger = logging.getLogger(__name__)`. Format: `"%(asctime)s [%(levelname)s] %(message)s"` with `datefmt="%H:%M:%S"`. GUI bridges logger → tkinter Text widget via `_GuiLogHandler`.
- **GPU fallback**: CUDA errors → auto CPU fallback. `_is_gpu_error()` checks for keyword matches (cublas, cuda, out of memory, etc.)
- **API retries**: Exponential backoff `2^attempt` seconds, up to `max_retries` (default 3)
- **`subtitle_extractor.py`**: Must only use stdlib — no third-party imports allowed
- **Lazy initialization**: `Downloader._ytdlp`, `Transcriber._model`, `Pipeline._translator` all lazy-load on first use
- **Intermediate files**: Files prefixed with `_` (`_segments.json`, `_subtitle.srt`, `_subtitle_info.json`, `_zh.txt`) are cleaned up after successful completion. Add cleanup for any new intermediate files in `_process_one()`.

## Config

- `config.yaml` — all runtime settings (see inline comments). Validated by `utils.validate_config()`.
- `.env` — secrets: `API_KEY` (required for summarization/polish)
- `summarizer.api_provider` — selects from preset providers (deepseek/mimo/zhipu/tongyi/moonshot/ollama); auto-fills `base_url` and `model`. Defined in `summarizer.API_PROVIDERS`.
- `summarizer.polish_model` defaults to provider's polish model; polish uses a fixed 5000-char chunk size (hardcoded); summarization uses `max_chunk_chars` (80000).
- `downloader.quality` — user-friendly presets (best/2160p/1080p/720p/480p/360p/audio) mapped via `Downloader._quality_to_format()`; advanced users can set `format` directly.

## Template

`template.html` is a full standalone HTML page with dark mode, TOC sidebar, and code-copy. `format_converter.md_to_html()` fills `{{PLACEHOLDER}}`-style slots. If modifying the template, keep the placeholder format consistent.
