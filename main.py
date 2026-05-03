"""
Video-to-Doc — 视频下载、转写、总结一体化工具

用法:
  python main.py <视频URL>                        单视频
  python main.py <视频URL> --playlist              播放列表/合集
  python main.py <视频URL> --skip-download         跳过下载，直接转写+总结
  python main.py <视频URL> --summary-style steps   指定总结风格

总结风格选项:
  auto             全面总结（默认）
  knowledge_points 提取知识点
  steps            提取操作步骤
  core_ideas       提炼核心观点
"""
import argparse
import sys
import yaml
from pathlib import Path
from pipeline import Pipeline


def load_config():
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
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
    parser.add_argument("url", help="视频 URL 或本地文件路径")
    parser.add_argument("--playlist", action="store_true", help="以播放列表模式下载")
    parser.add_argument("--skip-download", action="store_true", help="跳过下载（已有视频文件）")
    parser.add_argument("--summary-style", default="auto",
                        choices=["auto", "knowledge_points", "steps", "core_ideas"],
                        help="总结风格 (默认: auto)")
    parser.add_argument("--output-formats", default="md",
                        help="输出格式，逗号分隔: md,txt,html (默认: md)")

    args = parser.parse_args()

    config = load_config()

    if "output_dir" not in config:
        print("[ERROR] config.yaml 缺少必填项 output_dir，请检查配置文件")
        sys.exit(1)

    config.setdefault("summarizer", {})["summary_style"] = args.summary_style
    config.setdefault("summarizer", {})["output_formats"] = [
        f.strip() for f in args.output_formats.split(",") if f.strip()
    ]

    pipeline = Pipeline(config)

    try:
        pipeline.process(args.url, is_playlist=args.playlist)
    except KeyboardInterrupt:
        print("\n\n用户中断。断点续跑机制已保留进度，再次运行可继续。")
        sys.exit(0)
    except Exception as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
