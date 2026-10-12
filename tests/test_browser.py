"""验证跨平台登录选择、域隔离、身份隔离及凭证错误处理；不访问真实ERP。"""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import sys
import time
import unittest
from unittest.mock import Mock, patch

import jst_browser
import jushuitan
from jushuitan import QueryError, browser_session, session_identity


def cookie(name, value, domain=".erp321.com", **fields):
    return {"name": name, "value": value, "domain": domain, "path": "/",
            "secure": True, "expires": -1, **fields}


class BrowserTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(prefix="jst isolated choice ")
        self.addCleanup(folder.cleanup)
        state = patch("jst_browser._state_file", return_value=Path(folder.name)/"choice.json")
        state.start()
        self.addCleanup(state.stop)
        env = patch.dict(os.environ, {"JST_BROWSER_CHANNEL": ""})
        env.start()
        self.addCleanup(env.stop)

    @unittest.skipUnless(os.environ.get("JST_TEST_BROWSER") == "1", "按需运行真实浏览器验证；仅使用合成Cookie")
    def test_native_profile_survives_restarts_and_company_switch(self):
        from playwright.sync_api import sync_playwright
        with TemporaryDirectory(prefix="jst native browser ") as folder:
            directory = Path(folder)/"profile"
            channel = os.environ.get("JST_TEST_BROWSER_CHANNEL", "chromium")
            def save(company):
                with sync_playwright() as playwright:
                    context = playwright.chromium.launch_persistent_context(
                        str(directory), channel=channel, headless=True, args=["--restore-last-session"])
                    try:
                        context.add_cookies([
                            cookie("u_co_id", company, expires=time.time()+3600),
                            cookie("u_id", "810001"),
                            cookie("foreign", "synthetic", domain=".example.test", expires=time.time()+3600)])
                    finally:
                        context.close()
            with patch("jst_browser.profile_directory", return_value=directory):
                for company in ("910001", "920002"):
                    save(company)
                    for _ in range(2):
                        with browser_session(channel) as session:
                            self.assertEqual(session_identity(session).company_id, company)
                            self.assertEqual(session_identity(session).user_id, "810001")
                            self.assertFalse(any(c.name == "foreign" for c in session.cookies))

    def test_platform_default_and_explicit_backend(self):
        for platform, expected in (("darwin", "chrome"), ("win32", "browser"), ("linux", "browser")):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform):
                self.assertEqual(jushuitan.authentication_backend(), expected)
                self.assertEqual(jushuitan.authentication_backend("browser"), "browser")
                self.assertEqual(jushuitan.authentication_backend("chrome"), "chrome")

    def test_profile_is_outside_repository_and_uses_local_platform_directory(self):
        with TemporaryDirectory() as folder:
            for platform, variable in (("win32", "LOCALAPPDATA"), ("linux", "XDG_DATA_HOME")):
                with patch.object(sys, "platform", platform), patch.dict("os.environ", {variable: folder}):
                    self.assertEqual(jst_browser.profile_directory(), Path(folder)/"jst-ai-agent/browser-profile")

    def test_cookie_scope_expiry_partition_and_session_cookie(self):
        cookies = [cookie("u_id", "810001"), cookie("token", "SECRET", "apiweb.erp321.com", expires=time.time()+600),
                   cookie("other", "SECRET", ".example.com"), cookie("lookalike", "SECRET", ".erp321.com.evil.test"),
                   cookie("expired", "SECRET", expires=time.time()-1), cookie("partition", "SECRET", partitionKey="https://example.com")]
        self.assertEqual([c["name"] for c in jst_browser.erp_cookies(cookies)], ["u_id", "token"])

    def test_switch_company_uses_fresh_cookie_identity(self):
        for company, user in (("910001", "810001"), ("920002", "820002")):
            with patch("jst_browser.browser_cookies", return_value=[cookie("u_co_id", company), cookie("u_id", user)]):
                with browser_session() as session:
                    identity = session_identity(session)
                    self.assertEqual((identity.company_id, identity.user_id), (company, user))

    def test_missing_conflicting_identity_never_returns_authenticated_session(self):
        for cookies in ([], [cookie("u_id", "810001")],
                        [cookie("u_id", "810001"), cookie("u_co_id", "910001"),
                         cookie("u_co_id", "920002", "apiweb.erp321.com")]):
            with patch("jst_browser.browser_cookies", return_value=cookies), self.assertRaises(QueryError) as error:
                browser_session()
            self.assertIn("browser-login", str(error.exception))
            self.assertNotIn("910001", str(error.exception))

    def test_browser_login_reports_only_identity_and_does_not_query_business_data(self):
        with patch.object(sys, "argv", ["query", "browser-login"]), \
             patch("jst_browser.browser_cookies", side_effect=[None, [cookie("u_id", "810001"), cookie("u_co_id", "910001")]]) as read, \
             patch("jushuitan.Client") as client, redirect_stdout(StringIO()) as output:
            self.assertEqual(jushuitan.main(), 0)
        client.assert_not_called()
        self.assertEqual(read.call_args_list[0].kwargs, {"login": True, "browser": None})
        self.assertIn('"company_id": "910001"', output.getvalue())
        self.assertNotIn("cookies", output.getvalue().lower())

    def test_browser_profile_option_cannot_silently_use_daily_chrome(self):
        with patch.object(sys, "argv", ["query", "session-check", "--auth", "browser", "--chrome-profile", "Profile 1"]), \
             patch("jushuitan.chrome_session") as chrome, redirect_stderr(StringIO()) as output:
            self.assertEqual(jushuitan.main(), 1)
        chrome.assert_not_called()
        self.assertIn("--chrome-profile", output.getvalue())

    def test_browser_launch_errors_are_sanitized(self):
        from playwright.sync_api import Error
        with TemporaryDirectory() as folder, patch("jst_browser.profile_directory", return_value=Path(folder)), \
             patch("playwright.sync_api.sync_playwright") as playwright:
            playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.side_effect = Error("SECRET_COOKIE_SECRET")
            with self.assertRaises(jst_browser.BrowserSessionError) as error:
                jst_browser.browser_cookies()
            self.assertNotIn("SECRET", str(error.exception))

    def test_login_waits_for_full_browser_close_and_then_closes_context(self):
        with TemporaryDirectory() as folder, patch("jst_browser.profile_directory", return_value=Path(folder)), \
             patch("playwright.sync_api.sync_playwright") as playwright, redirect_stderr(StringIO()):
            context = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
            context.pages = [Mock()]
            self.assertIsNone(jst_browser.browser_cookies(login=True))
            context.pages[0].goto.assert_called_once()
            context.wait_for_event.assert_called_once_with("close", timeout=600000)
            context.close.assert_called_once()
            context.cookies.assert_not_called()

    def test_detected_custom_windows_path_is_used_to_launch(self):
        with TemporaryDirectory() as folder:
            executable = Path(folder)/"custom install/chrome.exe"
            executable.parent.mkdir()
            executable.touch()
            with patch.object(sys, "platform", "win32"), patch.object(jst_browser, "_app_path", side_effect=lambda exe: executable if exe == "chrome.exe" else None):
                detected = jst_browser.detect()
            chosen = next(c for c in detected if c["channel"] == "chrome")
            with patch("jst_browser.detect", return_value=[chosen]), patch("jst_browser.profile_directory", return_value=Path(folder)), patch("playwright.sync_api.sync_playwright") as api:
                launch = api.return_value.__enter__.return_value.chromium.launch_persistent_context
                launch.return_value.cookies.return_value = []
                jst_browser.browser_cookies(browser="chrome")
                self.assertEqual(launch.call_args.kwargs["executable_path"], str(executable))
                self.assertNotIn("channel", launch.call_args.kwargs)

    def test_linux_path_browsers_are_detected_in_priority_order(self):
        executables = {"brave-browser": "/usr/bin/brave-browser", "chromium": "/snap/bin/chromium"}
        with patch.object(sys, "platform", "linux"), patch("jst_browser.shutil.which", side_effect=executables.get), patch.object(Path, "is_file", return_value=False):
            found = jst_browser.detect()
        self.assertEqual([c["value"] for c in found], [str(Path(p)) for p in executables.values()])
        self.assertTrue(all(c["kind"] == "exe" for c in found))

    def test_macos_apps_are_detected_with_actual_executable_paths(self):
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        with patch.object(sys, "platform", "darwin"), patch.object(Path, "is_file", lambda p: p == Path(chrome)), patch("jst_browser.os.access", return_value=True):
            found = jst_browser.detect()
        self.assertEqual([c["value"] for c in found], [str(Path(chrome))])

    def test_registry_custom_install_lookup_uses_valid_key(self):
        with TemporaryDirectory() as folder:
            executable = Path(folder)/"chrome.exe"
            executable.touch()
            registry = Mock()
            registry.HKEY_CURRENT_USER = 1
            def open_key(hive, key):
                if hive == 1 and key == r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe":
                    return Mock(__enter__=Mock(return_value="key"), __exit__=Mock(return_value=False))
                raise OSError("not found")
            registry.OpenKey.side_effect = open_key
            registry.QueryValueEx.return_value = (str(executable), 1)
            with patch.object(jst_browser, "winreg", registry):
                self.assertEqual(jst_browser._app_path("chrome.exe"), executable)

    def test_auto_fallback_runs_full_login_and_keeps_stdout_clean(self):
        from playwright.sync_api import Error
        with TemporaryDirectory() as folder, patch("jst_browser.profile_directory", return_value=Path(folder)), patch("jst_browser.detect", return_value=[]), patch("playwright.sync_api.sync_playwright") as api, redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()):
            launch = api.return_value.__enter__.return_value.chromium.launch_persistent_context
            context = Mock()
            context.pages = [Mock()]
            launch.side_effect = [Error("missing chrome"), Error("missing edge"), context]
            jst_browser.browser_cookies(login=True)
            self.assertEqual(launch.call_args.kwargs["channel"], "chromium")
            context.pages[0].goto.assert_called_once_with(jst_browser.LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            context.wait_for_event.assert_called_once_with("close", timeout=600000)
            context.close.assert_called_once()
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(jst_browser.load_choice()["value"], "chromium")

    def test_forced_browser_does_not_fall_back_after_launch_failure(self):
        from playwright.sync_api import Error
        with TemporaryDirectory() as folder, patch("jst_browser.profile_directory", return_value=Path(folder)), patch("jst_browser.detect", return_value=[]), patch("playwright.sync_api.sync_playwright") as api:
            launch = api.return_value.__enter__.return_value.chromium.launch_persistent_context
            launch.side_effect = Error("SECRET browser unavailable")
            with self.assertRaises(jst_browser.BrowserSessionError) as error:
                jst_browser.browser_cookies(login=True, browser="msedge")
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(launch.call_args.kwargs["channel"], "msedge")
            self.assertNotIn("SECRET", str(error.exception))

    def test_operation_failure_does_not_switch_to_another_browser(self):
        from playwright.sync_api import Error
        with TemporaryDirectory() as folder, patch("jst_browser.profile_directory", return_value=Path(folder)), patch("jst_browser.detect", return_value=[]), patch("playwright.sync_api.sync_playwright") as api:
            launch = api.return_value.__enter__.return_value.chromium.launch_persistent_context
            launch.return_value.cookies.side_effect = Error("synthetic read failure")
            with self.assertRaises(jst_browser.BrowserSessionError):
                jst_browser.browser_cookies()
            self.assertEqual(launch.call_count, 1)
            launch.return_value.close.assert_called_once()

    def test_browser_flag_reaches_login_and_followup_identity_read(self):
        with patch.object(sys, "argv", ["query", "browser-login", "--browser", "msedge"]), patch("jst_browser.browser_cookies", side_effect=[None, [cookie("u_id", "810001"), cookie("u_co_id", "910001")]]) as read, redirect_stdout(StringIO()):
            self.assertEqual(jushuitan.main(), 0)
        self.assertEqual(read.call_args_list[0].kwargs, {"login": True, "browser": "msedge"})
        self.assertEqual(read.call_args_list[1].kwargs, {"browser": "msedge"})

    def test_browser_flag_selects_browser_backend_for_queries_on_macos(self):
        with patch.object(sys, "platform", "darwin"), patch.object(sys, "argv", ["query", "session-check", "--browser", "msedge"]), patch("jushuitan.chrome_session") as chrome, patch("jushuitan.browser_session") as browser, patch("jushuitan.Client") as client, redirect_stdout(StringIO()):
            client.return_value.company_id = "910001"
            client.return_value.user_id = "810001"
            client.return_value.item_page.return_value = {"data": [], "page": {}}
            self.assertEqual(jushuitan.main(), 0)
        browser.assert_called_once_with("msedge")
        chrome.assert_not_called()



if __name__ == "__main__":
    unittest.main()
