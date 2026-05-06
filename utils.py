"""
共享工具 — 文件名清理、ffmpeg 查找、状态管理、venv 路径、.env 加载
"""
import os
import re
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent


def sanitize_filename(name):
    return re.sub(r'[<>:"/\\|?*]', "-", name)


def find_ffmpeg():
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    raise FileNotFoundError("未找到 ffmpeg，请先安装 ffmpeg")


def find_venv_executable(name):
    """在项目 venv 中查找可执行文件，未找到则回退到 PATH"""
    if sys.platform == "win32":
        exe = PROJECT_ROOT / "venv" / "Scripts" / f"{name}.exe"
    else:
        exe = PROJECT_ROOT / "venv" / "bin" / name
    if exe.exists():
        return str(exe)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"未找到 {name}，请先运行 python setup.py")


def get_state(folder):
    """读取 .pipeline_state，返回状态字符串（空串表示未开始）"""
    state_file = Path(folder) / ".pipeline_state"
    if not state_file.exists():
        return ""
    return state_file.read_text().strip()


def set_state(folder, state):
    (Path(folder) / ".pipeline_state").write_text(state)


def is_done(folder):
    """下载器用于判断是否已处理过，避免重复下载"""
    return get_state(folder) in ("downloaded", "transcribed", "done")


def split_text(text, max_chars, sep="\n\n"):
    """按分隔符粗略分段，每段不超过 max_chars"""
    paragraphs = text.split(sep)
    chunks = []
    current = ""

    for p in paragraphs:
        if len(current) + len(p) < max_chars:
            current += p + sep
        else:
            if current:
                chunks.append(current.strip())
            current = p + sep

    if current.strip():
        chunks.append(current.strip())

    return chunks or [text]


def load_env():
    """加载项目根目录 .env 到 os.environ"""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                os.environ[key.strip()] = val
