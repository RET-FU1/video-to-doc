r"""
模型下载脚本 — 从 ModelScope 下载 faster-whisper 模型
请在终端中直接运行:
  venv\Scripts\python download_model.py
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
MODEL_CACHE = PROJECT_ROOT / "models"

REPO_ID = "pengzhendong/faster-whisper-large-v3-turbo"


def check_exists() -> bool:
    """检查模型是否已存在"""
    model_dir = MODEL_CACHE / "pengzhendong" / "faster-whisper-large-v3-turbo"
    if model_dir.exists() and any(f.suffix == ".bin" for f in model_dir.iterdir()):
        return True
    return False


def download():
    from modelscope.hub.snapshot_download import snapshot_download
    return snapshot_download(REPO_ID, cache_dir=str(MODEL_CACHE))


def main():
    print("=" * 60)
    print("  下载 faster-whisper-large-v3-turbo 模型")
    print(f"  来源: ModelScope")
    print(f"  缓存目录: {MODEL_CACHE}")
    print("  大小约 1.6GB，请耐心等待...")
    print("=" * 60)
    print()

    if check_exists():
        print("模型已存在，跳过下载。如需重新下载请删除 models/ 目录。")
        sys.exit(0)

    try:
        model_dir = download()
        print(f"\n模型已下载到: {model_dir}")

        from faster_whisper import WhisperModel
        WhisperModel(model_dir, device="cpu", compute_type="int8")
        print("模型加载验证成功！")

    except Exception as e:
        print(f"\n下载失败: {e}")
        print("请检查网络后重试。")
        sys.exit(1)


if __name__ == "__main__":
    main()
