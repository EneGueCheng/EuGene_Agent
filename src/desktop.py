"""
Desktop App 模式 — pywebview 原生窗口包装。

用法：
    python -m src.main --app

将 Gateway 的 Flask 服务嵌入到一个原生窗口中（而非用户手动打开浏览器）。
若 pywebview 不可用（未安装 / 无 WebView2 / 无头环境），自动回退到浏览器打开。
"""

import os
import sys
import threading
import time
import urllib.request
import webbrowser


def _wait_for_server(url: str, timeout: float = 10.0, interval: float = 0.2) -> bool:
    """等待 HTTP 服务就绪。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except (OSError, urllib.error.URLError):
            time.sleep(interval)
    return False


def _open_browser(url: str) -> None:
    """打开系统默认浏览器。"""
    print(f"  在浏览器中打开: {url}")
    webbrowser.open(url)


def run_app(config: dict, port: int) -> None:
    """启动 Desktop App 模式。

    - Flask 在后台 daemon 线程运行
    - 尝试用 pywebview 创建原生窗口
    - 若 pywebview 不可用，回退到浏览器
    - 窗口/浏览器关闭后自动退出
    """
    from src.gateway.server import run_gateway

    host = "127.0.0.1"
    url = f"http://{host}:{port}"

    # ── 1. 后台启动 Flask（daemon 线程，进程退出时自动终止） ──
    t = threading.Thread(
        target=run_gateway,
        args=(host, port),
        kwargs={"debug": False},
        daemon=True,
    )
    t.start()

    # ── 2. 等待服务就绪 ──
    if not _wait_for_server(url):
        print(f"[app] 警告: Flask 服务未在 {url} 就绪，仍尝试创建窗口...")

    # ── 3. 尝试 pywebview 原生窗口 ──
    _try_pywebview(url)

    # ── 4. 退出 ──
    os._exit(0)


def _try_pywebview(url: str) -> None:
    """尝试用 pywebview 创建原生窗口；失败则回退到浏览器。"""
    try:
        import webview  # type: ignore[import-untyped]

        webview.create_window(
            "SJTUClaw",
            url,
            width=1200,
            height=800,
            resizable=True,
        )
        webview.start()
        return  # 窗口正常关闭后返回
    except ImportError:
        print("[app] pywebview 未安装，回退到浏览器模式")
        print("[app] 安装命令: pip install pywebview")
    except Exception as e:
        print(f"[app] 原生窗口创建失败 ({type(e).__name__}: {e})，回退到浏览器模式")
        print("[app] 请确保安装了 WebView2 Runtime（Win 10/11 已内置）")

    # ── 回退：打开浏览器 ──
    print(f"[app] Gateway 运行于: {url}")
    _open_browser(url)
    print("[app] 按 Ctrl+C 停止服务")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print()
