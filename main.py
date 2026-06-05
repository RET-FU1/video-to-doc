"""
Video-to-Doc — 视频下载、转写、总结一体化工具

用法:
  python main.py <视频URL>                        单视频/音频
  python main.py <本地文件路径>                    本地视频/音频文件
  python main.py <视频URL> --playlist              播放列表/合集
  python main.py <视频URL> --summary-style steps   指定总结风格
  python main.py <视频URL> --multi-speaker         多说话人识别
  python main.py <视频URL> --translate             翻译转写
  python main.py <视频URL> --srt                   生成 SRT 字幕文件
  python main.py --folder <文件夹路径>             批量处理文件夹内所有视频/音频

总结风格:
  auto             全面总结（默认）
  knowledge_points 提取知识点
  steps            提取操作步骤
  core_ideas       提炼核心观点
  expert           专家深度分析
  custom           自定义提示词
"""
import sys
from pathlib import Path

# 自动重定向到项目虚拟环境
_PROJECT_ROOT = Path(__file__).parent
_VENV_PYTHON = (_PROJECT_ROOT / "venv" / "Scripts" / "python.exe"
                if sys.platform == "win32"
                else _PROJECT_ROOT / "venv" / "bin" / "python")
if _VENV_PYTHON.exists() and _VENV_PYTHON.resolve() != Path(sys.executable).resolve():
    import subprocess
    sys.exit(subprocess.run([str(_VENV_PYTHON), __file__] + sys.argv[1:]).returncode)

import argparse
import logging
from typing import Dict, Any

import yaml
from pipeline import Pipeline
from utils import setup_logging, validate_config

logger = logging.getLogger(__name__)


def load_config() -> Dict[str, Any]:
    config_path: Path = Path(__file__).parent / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_check(config: Dict[str, Any]) -> None:
    """诊断环境：检查各项依赖是否就绪"""
    import os
    import shutil
    from pathlib import Path

    print("=" * 50)
    print("  Video-to-Doc 环境诊断")
    print("=" * 50)
    print()

    all_ok = True

    def ok(msg: str) -> None:
        print(f"  [OK]  {msg}")

    def warn(msg: str) -> None:
        nonlocal all_ok
        all_ok = False
        print(f"  [!!]  {msg}")

    def info(msg: str) -> None:
        print(f"  [--]  {msg}")

    # 1. ffmpeg
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        ok(f"ffmpeg: {ffmpeg}")
    else:
        warn("ffmpeg 未找到，请安装: winget install Gyan.FFmpeg")

    # 2. Python 版本
    import sys
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 10):
        ok(f"Python {py_ver}")
    else:
        warn(f"Python {py_ver} (< 3.10)，请升级")

    # 3. 模型文件
    project_root = Path(__file__).parent
    models_dir = project_root / "models"
    model_dirs = list(models_dir.glob("pengzhendong/faster-whisper-large-v3-turbo"))
    if not model_dirs:
        model_dirs = list(models_dir.glob("models--*"))
    if model_dirs:
        bin_files = list(model_dirs[0].glob("*.bin"))
        if bin_files:
            size_gb = sum(f.stat().st_size for f in bin_files) / (1024 ** 3)
            ok(f"Whisper 模型已下载 ({size_gb:.1f} GB)")
        else:
            warn("模型目录存在但缺少 .bin 文件，请运行: python download_model.py")
    else:
        warn("Whisper 模型未下载，请运行: python setup.py")

    # 4. API_KEY
    from utils import load_env
    load_env()
    api_key = os.environ.get("API_KEY", "")
    if api_key and api_key != "sk-your-api-key-here":
        masked = api_key[:8] + "***" + api_key[-4:] if len(api_key) > 12 else "***"
        ok(f"API_KEY 已设置: {masked}")

        # 4b. API 连通性验证
        from summarizer import API_PROVIDERS
        summarizer_cfg = config.get("summarizer", {})
        api_provider = summarizer_cfg.get("api_provider", "") or summarizer_cfg.get("provider", "")
        preset = API_PROVIDERS.get(api_provider)
        base_url = summarizer_cfg.get("base_url") or (preset["base_url"] if preset else "https://api.deepseek.com")
        model = summarizer_cfg.get("model") or (preset["model"] if preset else "deepseek-v4-pro")
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=15)
            resp = client.chat.completions.create(
                model=model,
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            if resp.choices:
                ok(f"API 连通: {base_url} (model={model})")
            else:
                warn(f"API 返回为空: {base_url}")
        except Exception as e:
            msg = str(e).lower()
            if "401" in msg or "unauthorized" in msg or "invalid" in msg:
                warn(f"API Key 无效，请检查 .env 中的 API_KEY")
            elif "404" in msg or "not found" in msg:
                warn(f"API 模型不存在: {model}，请检查 config.yaml → summarizer.model")
            elif "timeout" in msg or "connect" in msg:
                warn(f"API 无法连接: {base_url}，请检查 config.yaml → summarizer.base_url")
            else:
                warn(f"API 验证失败: {e}")
    else:
        warn("API_KEY 未设置，请在 .env 中配置（AI 总结和标点分段依赖此项）")

    # 5. GPU
    gpu_name = None
    try:
        result = __import__("subprocess").run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            gpu_name = result.stdout.strip()
    except Exception:
        pass

    if gpu_name:
        from utils import init_cuda, cublas_available
        init_cuda()
        if cublas_available():
            ok(f"GPU: {gpu_name} (cuBLAS 可用)")
        else:
            info(f"GPU: {gpu_name} (cuBLAS 未找到，将使用 CPU 转写)")
    else:
        info("未检测到 NVIDIA GPU，将使用 CPU 转写")

    # 6. 关键依赖
    for mod, name in [
        ("faster_whisper", "faster-whisper"),
        ("openai", "openai"),
        ("yaml", "pyyaml"),
        ("modelscope", "modelscope"),
        ("mistune", "mistune"),
    ]:
        try:
            __import__(mod)
            ok(f"依赖 {name}")
        except ImportError:
            warn(f"依赖 {name} 未安装，请运行: pip install {name}")

    print()
    if all_ok:
        print("  环境就绪，可以开始使用。")
    else:
        print("  存在以上问题，请修复后重试。")
    print()


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Video-to-Doc — 下载视频、转写为文档、AI 总结",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py https://www.bilibili.com/video/BV1xx411x7xx
  python main.py C:/videos/myvideo.mp4
  python main.py https://example.com/video --multi-speaker
  python main.py https://example.com/video --translate --srt
  python main.py https://example.com/video --summary-style custom
        """,
    )
    parser.add_argument("url", nargs="?", help="视频 URL 或本地文件路径（--folder 模式下可省略）")
    parser.add_argument("--playlist", action="store_true", help="以播放列表模式下载")
    parser.add_argument("--folder", default=None, help="批量处理文件夹内所有视频")
    parser.add_argument("--skip-download", action="store_true", help="跳过下载（已有视频文件）")
    parser.add_argument("--download-only", action="store_true", help="仅下载视频，不做转写和总结")
    parser.add_argument("--summary-style", default="auto",
                        choices=["auto", "knowledge_points", "steps", "core_ideas", "expert", "custom"],
                        help="总结风格 (默认: auto，custom 需在 config.yaml 中设置 custom_prompt)")
    parser.add_argument("--output-formats", default="md",
                        help="输出格式，逗号分隔: md,txt,html (默认: md)")
    parser.add_argument("--multi-speaker", action="store_true",
                        help="启用多说话人识别（抛光时由 LLM 自动识别说话人）")
    parser.add_argument("--translate", action="store_true",
                        help="翻译转写文本为目标语言（见 config.yaml → translation）")
    parser.add_argument("--srt", action="store_true",
                        help="生成 SRT 字幕文件（可配合 --translate 生成中文字幕）")
    parser.add_argument("--skip-summary", action="store_true",
                        help="跳过 AI 总结步骤")
    parser.add_argument("-o", "--output-dir", default=None,
                        help="输出目录（覆盖 config.yaml 中的 output_dir）")
    parser.add_argument("--check", action="store_true",
                        help="诊断环境：检查 ffmpeg、模型、API Key、GPU 等是否就绪")

    args = parser.parse_args()

    config: Dict[str, Any] = load_config()

    # 配置校验
    errors = validate_config(config)
    if errors:
        logger.error("config.yaml 配置校验失败:")
        for err in errors:
            logger.error("  - %s", err)
        sys.exit(1)

    summarizer_cfg = config.setdefault("summarizer", {})
    summarizer_cfg["summary_style"] = args.summary_style
    summarizer_cfg["output_formats"] = [
        f.strip() for f in args.output_formats.split(",") if f.strip()
    ]
    if args.multi_speaker:
        config.setdefault("summarizer", {})["multi_speaker"] = True
    if args.output_dir and args.output_dir.strip():
        config["output_dir"] = args.output_dir.strip()

    if args.check:
        run_check(config)
        return

    pipeline: Pipeline = Pipeline(config, translate=args.translate,
                                   srt=args.srt, skip_summary=args.skip_summary)

    try:
        if args.download_only:
            if args.folder:
                logger.error("[仅下载] 暂不支持文件夹模式，请逐个下载")
                sys.exit(1)
            pipeline.download_only(args.url, is_playlist=args.playlist)
        elif args.folder:
            pipeline.process_folder(args.folder)
        elif args.url:
            pipeline.process(args.url, is_playlist=args.playlist)
        else:
            parser.error("必须提供 URL 或 --folder 参数")
    except KeyboardInterrupt:
        logger.info("用户中断。断点续跑机制已保留进度，再次运行可继续。")
        sys.exit(0)
    except Exception as e:
        logger.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
