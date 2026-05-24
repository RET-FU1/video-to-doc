"""
Video-to-Doc — 视频下载、转写、总结一体化工具

用法:
  python main.py <视频URL>                        单视频/音频
  python main.py <本地文件路径>                    本地视频/音频文件
  python main.py <视频URL> --playlist              播放列表/合集
  python main.py <视频URL> --skip-download         跳过下载，直接转写+总结
  python main.py <视频URL> --summary-style steps   指定总结风格
  python main.py --folder <文件夹路径>             批量处理文件夹内所有视频/音频

总结风格选项:
  auto             全面总结（默认）
  knowledge_points 提取知识点
  steps            提取操作步骤
  core_ideas       提炼核心观点
  expert           专家深度分析
"""
import argparse
import logging
import sys
from pathlib import Path
from typing import Dict, Any

import yaml
from pipeline import Pipeline
from utils import setup_logging, validate_config

logger = logging.getLogger(__name__)


def load_config() -> Dict[str, Any]:
    config_path: Path = Path(__file__).parent / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Video-to-Doc — 下载视频、转写为文档、AI 总结",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py https://www.bilibili.com/video/BV1xx411x7xx
  python main.py C:/videos/myvideo.mp4
  python main.py https://www.youtube.com/playlist?list=xxx --playlist
  python main.py https://example.com/video --summary-style knowledge_points
        """,
    )
    parser.add_argument("url", nargs="?", help="视频 URL 或本地文件路径（--folder 模式下可省略）")
    parser.add_argument("--playlist", action="store_true", help="以播放列表模式下载")
    parser.add_argument("--folder", default=None, help="批量处理文件夹内所有视频")
    parser.add_argument("--skip-download", action="store_true", help="跳过下载（已有视频文件）")
    parser.add_argument("--download-only", action="store_true", help="仅下载视频，不做转写和总结")
    parser.add_argument("--summary-style", default="auto",
                        choices=["auto", "knowledge_points", "steps", "core_ideas", "expert"],
                        help="总结风格 (默认: auto)")
    parser.add_argument("--output-formats", default="md",
                        help="输出格式，逗号分隔: md,txt,html (默认: md)")
    parser.add_argument("--diarize", action="store_true",
                        help="启用说话人分离（需安装 pyannote.audio 并配置 HF_TOKEN）")

    args = parser.parse_args()

    config: Dict[str, Any] = load_config()

    # 配置校验
    errors = validate_config(config)
    if errors:
        logger.error("config.yaml 配置校验失败:")
        for err in errors:
            logger.error("  - %s", err)
        sys.exit(1)

    config.setdefault("summarizer", {})["summary_style"] = args.summary_style
    config.setdefault("summarizer", {})["output_formats"] = [
        f.strip() for f in args.output_formats.split(",") if f.strip()
    ]
    if args.diarize:
        config.setdefault("diarization", {})["enabled"] = True

    pipeline: Pipeline = Pipeline(config)

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
