"""专用浏览器认证：不解密日常浏览器，不导出明文登录文件。"""
import os
from pathlib import Path
import sys
import time

LOGIN_URL = "https://www.erp321.com/epaas"


class BrowserSessionError(Exception):
    pass


def profile_directory():
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()/"AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home()/"Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home()/".local/share"))
    return base/"jst-ai-agent/browser-profile"


def erp_cookies(cookies):
    """仅把目标域、未过期、非分区 Cookie 交给只读查询会话。"""
    selected = []
    for cookie in cookies:
        domain = cookie["domain"].lstrip(".")
        expires = cookie.get("expires", -1)
        if (domain == "erp321.com" or domain.endswith(".erp321.com")) and \
                not cookie.get("partitionKey") and (expires <= 0 or expires > time.time()):
            selected.append(cookie)
    return selected


def browser_cookies(login=False):
    try:
        from playwright.sync_api import sync_playwright, Error, TimeoutError
    except ImportError as exc:
        raise BrowserSessionError("浏览器依赖尚未安装，请先运行 scripts/setup.py。") from exc
    directory = profile_directory()
    if not login and not directory.is_dir():
        raise BrowserSessionError("尚未连接聚水潭，请先运行 browser-login，在专用浏览器中正常登录。")
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if sys.platform != "win32":
            directory.chmod(0o700)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(directory), channel="chromium", headless=not login,
                args=["--restore-last-session"])
            try:
                if login:
                    page = context.pages[0] if context.pages else context.new_page()
                    print("请在专用浏览器正常登录聚水潭、完成验证并选择目标公司。"
                          "完成后关闭整个专用浏览器窗口（10分钟内）；不要向聊天发送密码或Cookie。",
                          file=sys.stderr)
                    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                    # 等待浏览器关闭，保证客户有机会完成验证与公司选择。
                    context.wait_for_event("close", timeout=600000)
                    return None
                return erp_cookies(context.cookies())
            finally:
                context.close()
    except TimeoutError as exc:
        raise BrowserSessionError("登录页面或操作等待超时，请重新运行 browser-login 完成登录。") from exc
    except (Error, OSError) as exc:
        # 浏览器异常可能包含路径、URL甚至凭证，绝不转发原始异常。
        raise BrowserSessionError("无法启动专用浏览器或读取会话。请先完成 setup.py 安装，"
                                  "关闭正在使用的专用浏览器再重试；Linux 登录需要桌面环境，"
                                  "系统库缺失时按安装说明补齐。") from exc
