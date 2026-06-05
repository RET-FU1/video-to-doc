"""
一键初始化脚本
用法: python setup.py

自动完成:
1. 创建项目虚拟环境 (venv/)
2. 在虚拟环境中安装依赖（含 GPU 加速库）
3. 设置模型缓存到项目目录
4. 检查 ffmpeg 可用性
5. 检查 GPU / CUDA 加速
"""
import sys
import subprocess
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
VENV_DIR = PROJECT_ROOT / "venv"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
MODEL_CACHE = PROJECT_ROOT / "models"
PIP_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"


def run(cmd, **kwargs):
    """运行命令并回显"""
    print(f"  → {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    return subprocess.run(cmd, check=True, **kwargs)


def get_python():
    """获取虚拟环境中的 python 路径"""
    from utils import find_venv_executable
    return find_venv_executable("python")


def get_pip():
    """获取虚拟环境中的 pip 路径"""
    from utils import find_venv_executable
    return find_venv_executable("pip")


def step1_create_venv():
    """创建虚拟环境"""
    if VENV_DIR.exists():
        print("[1/6] 虚拟环境已存在，跳过创建")
        return

    print("[1/6] 创建虚拟环境...")
    run([sys.executable, "-m", "venv", str(VENV_DIR)])
    print("  虚拟环境创建完成")


def step2_install_deps():
    """安装依赖"""
    print("[2/6] 安装 Python 依赖...")
    pip = get_pip()
    run([pip, "install", "-r", str(REQUIREMENTS), "-i", PIP_INDEX,
         "--trusted-host", "pypi.tuna.tsinghua.edu.cn"])
    print("  依赖安装完成")


def step3_download_model():
    """下载 Whisper 模型（约 1.6GB）"""
    model_dir = MODEL_CACHE / "pengzhendong" / "faster-whisper-large-v3-turbo"
    if model_dir.exists() and any(
        f.suffix == ".bin" for f in model_dir.iterdir()
    ):
        print("[3/6] 模型已存在，跳过下载")
        return

    python = get_python()
    print("[3/6] 下载 Whisper 模型（约 1.6GB，首次运行）...")
    print("  请耐心等待，下载进度实时显示...")
    print()

    try:
        subprocess.run(
            [python, str(PROJECT_ROOT / "download_model.py")],
            check=True,
        )
    except subprocess.CalledProcessError:
        print("\n[WARN] 模型下载失败，可稍后手动运行:")
        print(f"  {python} download_model.py                       # ModelScope（国内快）")
        print(f"  {python} download_model.py --source huggingface  # HuggingFace")


def step4_check_ffmpeg():
    """检查 ffmpeg"""
    print("[4/6] 检查 ffmpeg...")
    if shutil.which("ffmpeg"):
        print("  ffmpeg 可用")
        return

    print("  [WARN] 未找到 ffmpeg！")
    print("  请手动安装 ffmpeg:")
    print("    Windows: winget install Gyan.FFmpeg")
    print("    Mac:     brew install ffmpeg")
    print("    Linux:   sudo apt install ffmpeg")


def step5_check_gpu():
    """检查 GPU / CUDA"""
    print("[5/6] 检查 GPU 加速...")
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            gpu_name = result.stdout.strip()
            print(f"  GPU 可用: {gpu_name}")
            try:
                from utils import init_cuda, cublas_available
                init_cuda()
                if cublas_available():
                    print("  cuBLAS 已就绪，GPU 加速可用")
                else:
                    raise OSError("cuBLAS not found")
            except OSError:
                print("  [WARN] cuBLAS 未找到，将使用 CPU 转写")
                print("  如需 GPU 加速，请确保已安装 CUDA Toolkit 或 pip install nvidia-cublas-cu12")
        else:
            print("  未检测到 NVIDIA GPU，将使用 CPU 转写")
    except FileNotFoundError:
        print("  未找到 nvidia-smi，将使用 CPU 转写")


def main():
    print("=" * 60)
    print("  Video-to-Doc 一键初始化")
    print("=" * 60)
    print()

    try:
        step1_create_venv()
        step2_install_deps()
        step3_download_model()
        step4_check_ffmpeg()
        step5_check_gpu()
    except subprocess.CalledProcessError as e:
        print(f"\n[ERROR] 步骤失败: {e}")
        sys.exit(1)

    # 自动复制 .env（若不存在）
    env_file = PROJECT_ROOT / ".env"
    env_example = PROJECT_ROOT / ".env.example"
    if not env_file.exists() and env_example.exists():
        shutil.copy(env_example, env_file)
        print()
        print("[!] 已从 .env.example 复制 .env，请编辑填入 API_KEY:")
        print(f"    {env_file}")
        print()

    print()
    print("=" * 60)
    print("  初始化完成！")
    print()
    print("  使用方法:")
    print(f"    {get_python()} main.py <视频URL>")
    print(f"    {get_python()} main.py <播放列表URL> --playlist")
    print()
    print("  使用前请设置 .env:")
    print("    cp .env.example .env")
    print("    编辑 .env 填入 API_KEY（AI 总结）")
    print("=" * 60)


if __name__ == "__main__":
    main()
