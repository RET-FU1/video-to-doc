"""
共享工具 — 文件名清理、ffmpeg 查找、状态管理、venv 路径、.env 加载、日志、重试
"""
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import List

PROJECT_ROOT: Path = Path(__file__).parent


# ---- 日志 ----

def setup_logging(level: int = logging.INFO) -> None:
    """配置全局日志格式，同时输出到控制台"""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# ---- 媒体文件扩展名 ----

VIDEO_EXTS: set = {".mp4", ".mkv", ".webm", ".flv", ".avi", ".mov"}
AUDIO_EXTS: set = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".opus", ".wma"}
MEDIA_EXTS: set = VIDEO_EXTS | AUDIO_EXTS


# ---- 文件名清理 ----

def sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符，并处理边界情况"""
    if not name or not name.strip():
        return "untitled"
    name = name.strip().strip(".")
    if not name:
        return "untitled"
    name = re.sub(r'[<>:"/\\|?*]', "-", name)
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }
    upper = name.upper()
    if upper in reserved or upper.startswith(tuple(r + "." for r in reserved)):
        name = "_" + name
    return name


# ---- ffmpeg / venv 可执行文件查找 ----

def find_ffmpeg() -> str:
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    raise FileNotFoundError("未找到 ffmpeg，请先安装 ffmpeg")


def find_venv_executable(name: str) -> str:
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


# ---- CUDA / GPU 检测 ----

_cuda_inited: bool = False


def init_cuda() -> None:
    """初始化 CUDA DLL 搜索路径，Windows 下避免手动管理 PATH"""
    global _cuda_inited
    if _cuda_inited:
        return
    _cuda_inited = True

    import ctypes

    # 扫描 site-packages 中的 nvidia/ 和 ctranslate2/ DLL 目录
    for _d in sys.path:
        for _pkg in ("nvidia", "ctranslate2"):
            _pkg_dir = os.path.join(_d, _pkg)
            if os.path.isdir(_pkg_dir):
                # 子目录的 bin/（nvidia/cublas/bin, nvidia/cuda_runtime/bin 等）
                for _sub in os.listdir(_pkg_dir):
                    _bin = os.path.join(_pkg_dir, _sub, "bin")
                    if os.path.isdir(_bin):
                        try:
                            os.add_dll_directory(_bin)
                        except Exception:
                            pass
                # 包目录本身可能直接包含 DLL（如 ctranslate2/cudnn64_9.dll）
                if any(f.endswith(".dll") for f in os.listdir(_pkg_dir)):
                    try:
                        os.add_dll_directory(_pkg_dir)
                    except Exception:
                        pass

    # 扫描 CUDA Toolkit 安装目录
    for cuda_root in [r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA",
                      os.environ.get("CUDA_PATH", "")]:
        if cuda_root and os.path.isdir(cuda_root):
            for ver_dir in sorted(os.listdir(cuda_root), reverse=True):
                bin_dir = os.path.join(cuda_root, ver_dir, "bin")
                if os.path.isdir(bin_dir):
                    try:
                        os.add_dll_directory(bin_dir)
                    except Exception:
                        pass
                    break

    # 预加载 cuBLAS / cuDART（版本号自适应）
    for major in (13, 12, 11):
        for dll in (f"cublas64_{major}.dll", f"cudart64_{major}.dll"):
            try:
                ctypes.cdll.LoadLibrary(dll)
            except OSError:
                pass


def cublas_available() -> bool:
    """检查 cuBLAS DLL 是否可加载"""
    init_cuda()
    import ctypes
    for major in (13, 12, 11):
        try:
            ctypes.cdll.LoadLibrary(f"cublas64_{major}.dll")
            return True
        except OSError:
            pass
    return False


# ---- 流水线状态管理 ----

def get_state(folder: Path) -> str:
    """读取 .pipeline_state，返回状态字符串（空串表示未开始）"""
    state_file = Path(folder) / ".pipeline_state"
    if not state_file.exists():
        return ""
    return state_file.read_text(encoding="utf-8").strip()


def set_state(folder: Path, state: str) -> None:
    (Path(folder) / ".pipeline_state").write_text(state, encoding="utf-8")


def is_done(folder: Path) -> bool:
    """下载器用于判断是否已处理过，避免重复下载"""
    return get_state(folder) in ("downloaded", "transcribed", "done")


# ---- 文本分段 ----

def split_text(text: str, max_chars: int, sep: str = "\n\n") -> List[str]:
    """按分隔符粗略分段，每段不超过 max_chars"""
    if not text.strip():
        return []
    paragraphs = [p for p in text.split(sep) if p.strip()]
    if not paragraphs:
        return [text]

    chunks: List[str] = []
    current = ""

    for p in paragraphs:
        if len(current) + len(p) < max_chars:
            current += p + sep
        else:
            if current.strip():
                chunks.append(current.strip())
            current = p + sep

    if current.strip():
        chunks.append(current.strip())

    return chunks or [text]


def split_text_with_overlap(text: str, max_chars: int = 5000, overlap: int = 300,
                            sep: str = "\n") -> list:
    """按分隔符切分文本，相邻块之间保留重叠区域作为上下文。

    重叠让 LLM 在处理边界句子时能看到上下文，避免标点错误。
    合并时调用方需丢弃每块（除第一块外）的第一个段落来去重。
    """
    parts = [p for p in text.split(sep) if p.strip()]
    if not parts:
        return [text]

    core: list = []
    current = ""
    for p in parts:
        piece = p + sep
        if len(current) + len(piece) < max_chars:
            current += piece
        else:
            if current.strip():
                core.append(current.strip())
            current = piece
    if current.strip():
        core.append(current.strip())

    if len(core) <= 1:
        return core or [text]

    result = [core[0]]
    for i in range(1, len(core)):
        prev = core[i - 1]
        if len(prev) > overlap:
            start = max(0, len(prev) - overlap)
            nl = prev.find("\n", start)
            ctx = prev[nl + 1:] if nl > 0 else prev[start:]
        else:
            ctx = prev
        result.append(ctx + "\n" + core[i])

    return result


# ---- .env 加载 ----

def load_env() -> None:
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


# ---- 配置校验 ----

def validate_config(config: dict) -> List[str]:
    """校验配置文件的必填项和类型，返回错误消息列表"""
    errors: List[str] = []

    if not isinstance(config, dict):
        return ["config.yaml 解析结果不是有效的字典"]

    if "output_dir" not in config:
        errors.append("config.yaml 缺少必填项 output_dir")

    whisper = config.get("whisper", {})
    if not isinstance(whisper, dict):
        errors.append("whisper 配置应为字典")
    else:
        device = whisper.get("device", "cpu")
        if device not in ("cpu", "cuda"):
            errors.append(f"whisper.device 无效值 '{device}'，可选: cpu, cuda")
        compute = whisper.get("compute_type", "int8")
        if compute not in ("float16", "int8", "float32"):
            errors.append(f"whisper.compute_type 无效值 '{compute}'")

    summarizer = config.get("summarizer", {})
    if not isinstance(summarizer, dict):
        errors.append("summarizer 配置应为字典")
    else:
        provider = summarizer.get("provider", "openai")
        if provider not in ("openai", "ollama"):
            errors.append(f"summarizer.provider 无效值 '{provider}'，可选: openai, ollama")
        if "model" not in summarizer:
            errors.append("summarizer 缺少必填项 model")
        max_chunk = summarizer.get("max_chunk_chars", 80000)
        if not isinstance(max_chunk, (int, float)) or max_chunk < 1000:
            errors.append(f"summarizer.max_chunk_chars 值无效，应为 >= 1000 的整数")
        max_tok = summarizer.get("max_tokens", 4096)
        if not isinstance(max_tok, (int, float)) or max_tok < 256:
            errors.append(f"summarizer.max_tokens 值无效，应为 >= 256 的整数")

    diar = config.get("diarization", {})
    if isinstance(diar, dict) and diar.get("enabled"):
        if diar.get("min_speakers", 0) > diar.get("max_speakers", 999):
            errors.append("diarization.min_speakers 不能大于 max_speakers")

    dl = config.get("downloader", {})
    if isinstance(dl, dict):
        timeout = dl.get("timeout", 7200)
        if not isinstance(timeout, (int, float)) or timeout < 10:
            errors.append(f"downloader.timeout 值无效，应为 >= 10 的整数")

    return errors