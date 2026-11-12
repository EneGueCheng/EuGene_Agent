"""兼容层：开发模式 / PyInstaller 打包模式统一路径解析。

PyInstaller 打包后 sys.frozen=True，资源文件解压到临时目录 sys._MEIPASS。
路径分两类：
    - get_project_root() → 只读资源（templates/themes/system_prompt fallback）
    - get_runtime_dir()   → 可写数据（.env/.claw/data/ 等，打包后为 cwd）
"""

import sys
from pathlib import Path


def get_project_root() -> Path:
    """返回项目根目录（兼容开发与 PyInstaller 打包模式）。

    - 开发模式：通过 __file__ 推断（src/_compat.py → src/ → 根目录）
    - 打包模式：返回 sys._MEIPASS（PyInstaller 资源解压目录）
    用于读取只读资源：templates、themes、.claw/system_prompt.txt 等。
    """
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def get_runtime_dir() -> Path:
    """返回运行时数据目录。

    - 开发模式：与 get_project_root() 相同（在项目目录下读写）
    - 打包模式：返回当前工作目录（.env/.claw/data/ 等在该目录下读写）
    用于读写可写数据：.env、.claw/sessions/、.claw/models.json、data/ 等。
    """
    if getattr(sys, "frozen", False):
        return Path.cwd()
    return Path(__file__).resolve().parent.parent


def load_env_file() -> None:
    """加载运行时目录下的 .env 文件。

    在 PyInstaller 打包模式下，python-dotenv 导入可能失败（模块自动发现不完整），
    因此这里使用手动解析作为可靠后备方案。
    """
    env_path = get_runtime_dir() / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # 跳过空行和注释
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in sys.modules.get("os", __import__("os")).environ:
                __import__("os").environ[key] = value
