r"""
模型下载脚本 — 支持 ModelScope 和 HuggingFace 两个下载源
请在终端中直接运行:
  venv\Scripts\python download_model.py                      # 默认 ModelScope（国内快）
  venv\Scripts\python download_model.py --source huggingface  # HuggingFace
"""
import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
MODEL_CACHE = PROJECT_ROOT / "models"

REPO_ID = "pengzhendong/faster-whisper-large-v3-turbo"
HF_REPO_ID = "Systran/faster-whisper-large-v3-turbo"


def check_exists(source: str) -> bool:
    """检查模型是否已存在"""
    if source == "huggingface":
        pattern = "models--Systran--faster-whisper-large-v3-turbo"
        for d in MODEL_CACHE.glob(pattern):
            snapshots = d / "snapshots"
            if snapshots.is_dir():
                for snap in snapshots.iterdir():
                    if snap.is_dir() and any(f.suffix == ".bin" for f in snap.iterdir()):
                        return True
    else:
        model_dir = MODEL_CACHE / "pengzhendong" / "faster-whisper-large-v3-turbo"
        if model_dir.exists() and any(f.suffix == ".bin" for f in model_dir.iterdir()):
            return True
    return False


def download_modelscope():
    from modelscope.hub.snapshot_download import snapshot_download
    return snapshot_download(REPO_ID, cache_dir=str(MODEL_CACHE))


def download_huggingface():
    from huggingface_hub import snapshot_download
    return snapshot_download(HF_REPO_ID, cache_dir=str(MODEL_CACHE))


def main():
    parser = argparse.ArgumentParser(description="下载 faster-whisper-large-v3-turbo 模型")
    parser.add_argument("--source", default="modelscope",
                        choices=["modelscope", "huggingface"],
                        help="下载源 (默认: modelscope，国内快；huggingface 需外网)")
    args = parser.parse_args()

    print("=" * 60)
    print(f"  下载 faster-whisper-large-v3-turbo 模型")
    print(f"  来源: {'ModelScope' if args.source == 'modelscope' else 'HuggingFace'}")
    print(f"  缓存目录: {MODEL_CACHE}")
    print("  大小约 1.6GB，请耐心等待...")
    print("=" * 60)
    print()

    if check_exists(args.source):
        print("模型已存在，跳过下载。如需重新下载请删除 models/ 目录。")
        sys.exit(0)

    try:
        if args.source == "huggingface":
            model_dir = download_huggingface()
        else:
            model_dir = download_modelscope()

        print(f"\n模型已下载到: {model_dir}")

        from faster_whisper import WhisperModel
        model = WhisperModel(model_dir, device="cpu", compute_type="int8")
        print("模型加载验证成功！")

    except ImportError as e:
        if "huggingface" in str(e).lower():
            print(f"\n缺少 huggingface_hub，请先安装: pip install huggingface_hub")
        else:
            print(f"\n导入失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n下载失败: {e}")
        if args.source == "huggingface":
            print("提示：HuggingFace 需要外网访问，国内用户可改用: python download_model.py")
        else:
            print("请检查网络后重试，或改用 HuggingFace: python download_model.py --source huggingface")
        sys.exit(1)


if __name__ == "__main__":
    main()
