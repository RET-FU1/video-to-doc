"""
模型下载脚本 — 从 ModelScope 下载 faster-whisper-large-v3-turbo
请在终端中直接运行此脚本（不受 Claude Code 超时限制）:
  venv\Scripts\python download_model.py
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
MODEL_CACHE = PROJECT_ROOT / "models"

print("=" * 60)
print("  下载 faster-whisper-large-v3-turbo 模型")
print(f"  缓存目录: {MODEL_CACHE}")
print("  大小约 1.6GB，请耐心等待...")
print("=" * 60)
print()

# 检查是否已存在
model_dir = MODEL_CACHE / "pengzhendong" / "faster-whisper-large-v3-turbo"
if model_dir.exists() and any(f.suffix == ".bin" for f in model_dir.iterdir()):
    print("模型已存在，跳过下载。如需重新下载请删除 models/ 目录。")
    sys.exit(0)

try:
    from modelscope.hub.snapshot_download import snapshot_download
    model_dir = snapshot_download(
        "pengzhendong/faster-whisper-large-v3-turbo",
        cache_dir=str(MODEL_CACHE),
    )
    print(f"\n模型已下载到: {model_dir}")

    # 验证
    from faster_whisper import WhisperModel
    model = WhisperModel(model_dir, device="cpu", compute_type="int8")
    print("模型加载验证成功！")

except Exception as e:
    print(f"\n下载失败: {e}")
    print("请检查网络后重试。")
    sys.exit(1)
