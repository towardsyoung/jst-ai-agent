"""专用浏览器认证：不解密日常浏览器，不导出明文登录文件。

启动时自动挑选本机已安装的浏览器（Chrome > Edge > 其他 Chromium 系），
无需下载 Playwright 内置 Chromium；全部候选失败时才回落到内置 chromium。
选中的浏览器记到 %LOCALAPPDATA%/jst-ai-agent/browser-choice.json，后续优先复用；
可用环境变量 JST_BROWSER_CHANNEL 强制指定。
"""
import json
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


# ---------- 本机浏览器探测 ----------

# (标签, 可执行文件名, Playwright 通道, 相对安装目录, 是否查注册表)
# 通道为 None 表示 Playwright 无对应 channel，需用 executable_path 启动。
_SPECS = (
    ("Chrome 稳定版", "chrome.exe", "chrome", "Google/Chrome/Application", True),
    ("Chrome Beta", "chrome.exe", "chrome-beta", "Google/Chrome Beta/Application", False),
    ("Chrome Dev", "chrome.exe", "chrome-dev", "Google/Chrome Dev/Application", False),
    ("Edge 稳定版", "msedge.exe", "msedge", "Microsoft/Edge/Application", True),
    ("Edge Beta", "msedge.exe", "msedge-beta", "Microsoft/Edge Beta/Application", False),
    ("Brave", "brave.exe", None, "BraveSoftware/Brave-Browser/Application", True),
    ("Chromium", "chromium.exe", None, "Chromium/Application", False),
    ("Vivaldi", "vivaldi.exe", None, "Vivaldi/Application", False),
)

# 什么都探测不到时，仍按此顺序交给 Playwright 自行解析；最后才是内置 chromium
_BLIND_FALLBACK = ("chrome", "msedge", "chromium")

try:
    import winreg
except ImportError:  # 非 Windows
    winreg = None


def _app_path(exe):
    """查注册表 App Paths，能覆盖自定义安装位置。"""
    if winreg is None:
        return None
    tail = r"Microsoft\Windows\CurrentVersion\App Paths\\" + exe
    for hive, prefix in ((winreg.HKEY_CURRENT_USER, ""),
                         (winreg.HKEY_LOCAL_MACHINE, r"WOW6432Node\\"),
                         (winreg.HKEY_LOCAL_MACHINE, "")):
        try:
            with winreg.OpenKey(hive, rf"SOFTWARE\{prefix}{tail}") as key:
                value, _ = winreg.QueryValueEx(key, None)
                if value and Path(value).is_file():
                    return Path(value)
        except OSError:
            continue
    return None


def _installed_dirs(relative):
    bases = (os.environ.get("LOCALAPPDATA"), os.environ.get("PROGRAMFILES"),
             os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMW6432"),
            str(Path.home()/".local/share")) if sys.platform == "win32" else \
           ("/opt/google/chrome", "/usr/bin", "/snap/bin")
    return [Path(base)/relative for base in bases if base]


def _find(spec):
    label, exe, channel, relative, use_registry = spec
    if use_registry:
        found = _app_path(exe)
        if found:
            return found
    for directory in _installed_dirs(relative):
        candidate = directory/exe
        if candidate.is_file():
            return candidate
    return None


def detect():
    """返回按优先级排列、本机确实存在的浏览器候选。"""
    found = []
    for spec in _SPECS:
        path = _find(spec)
        if not path:
            continue
        label, _exe, channel, _relative, _reg = spec
        if channel:
            found.append({"label": label, "kind": "channel", "value": channel, "path": str(path)})
        else:
            found.append({"label": label, "kind": "exe", "value": str(path), "path": str(path)})
    return found


def _state_file():
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()/"AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home()/"Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home()/".local/share"))
    return base/"jst-ai-agent/browser-choice.json"


def load_choice():
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("kind") in ("channel", "exe") and data.get("value"):
        if data["kind"] == "channel" or Path(str(data["value"])).is_file():
            return {"label": data.get("label", data["value"]), "kind": data["kind"],
                    "value": data["value"], "path": data.get("path", data["value"])}
    return None


def save_choice(candidate):
    try:
        path = _state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({k: candidate.get(k) for k in ("label", "kind", "value", "path")},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _as_candidate(value):
    if os.sep in str(value) or "/" in str(value):
        return {"label": "指定路径", "kind": "exe", "value": str(value), "path": str(value)}
    return {"label": value, "kind": "channel", "value": value, "path": ""}


def candidates(forced=None):
    """启动顺序：强制指定 > 上次成功 > 本机探测 > 盲试兜底。"""
    forced = forced or os.environ.get("JST_BROWSER_CHANNEL")
    if forced:
        return [_as_candidate(forced)]
    result, seen = [], set()

    def push(item):
        key = (item["kind"], item["value"])
        if key not in seen:
            seen.add(key)
            result.append(item)

    cached = load_choice()
    if cached:
        push(cached)
    for item in detect():
        push(item)
    for name in _BLIND_FALLBACK:
        push({"label": name, "kind": "channel", "value": name, "path": ""})
    return result


def describe():
    lines = ["已探测到的本机浏览器（按优先级）："]
    found = detect()
    if not found:
        lines.append("  （未探测到，将交由 Playwright 自行解析，最后回落内置 chromium）")
    for index, item in enumerate(found, 1):
        lines.append(f"  {index}. {item['label']}  [{item['kind']}={item['value']}]  {item['path']}")
    cached = load_choice()
    lines.append("上次成功使用：" + (f"{cached['label']} ({cached['value']})" if cached else "无"))
    lines.append("实际启动顺序：" + " -> ".join(item["label"] for item in candidates()))
    return "\n".join(lines)


# ---------- 会话获取 ----------

def browser_cookies(login=False):
    try:
        from playwright.sync_api import sync_playwright, Error, TimeoutError
    except ImportError as exc:
        raise BrowserSessionError("浏览器依赖尚未安装，请先运行 scripts/setup.py。") from exc
    directory = profile_directory()
    if not login and not directory.is_dir():
        raise BrowserSessionError("尚未连接聚水潭，请先运行 browser-login，在专用浏览器中正常登录。")
    tried = []
    for candidate in candidates():
        tried.append(candidate["label"])
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if sys.platform != "win32":
                directory.chmod(0o700)
            with sync_playwright() as playwright:
                options = {"headless": not login, "args": ["--restore-last-session"]}
                if candidate["kind"] == "channel":
                    context = playwright.chromium.launch_persistent_context(
                        str(directory), channel=candidate["value"], **options)
                else:
                    context = playwright.chromium.launch_persistent_context(
                        str(directory), executable_path=candidate["value"], **options)
                try:
                    save_choice(candidate)
                    if login:
                        page = context.pages[0] if context.pages else context.new_page()
                        print(f"已选用本机浏览器：{candidate['label']}", flush=True)
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
        except (Error, OSError):
            continue  # 该浏览器不可用，自动顺延到下一个候选
    # 所有本机候选都失败，才回落到 Playwright 内置 chromium（需要 setup.py 下载）
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(directory), headless=not login, args=["--restore-last-session"])
            try:
                return None if login else erp_cookies(context.cookies())
            finally:
                context.close()
    except (Error, OSError) as exc:
        # 浏览器异常可能包含路径、URL甚至凭证，绝不转发原始异常。
        raise BrowserSessionError(
            "无法启动任何浏览器（已尝试：" + "、".join(tried) +
            "）。请确认本机已安装 Chrome 或 Edge；或完成 setup.py 安装内置浏览器后重试，"
            "并关闭正在使用的专用浏览器。Linux 登录需要桌面环境，系统库缺失时按安装说明补齐。") from exc
