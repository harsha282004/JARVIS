"""Browser engine over the deterministic fake web: lifecycle, navigation, tabs, elements, verification, downloads/uploads, recovery, URL safety."""

import json
import threading
from pathlib import Path

import pytest

from browser.downloads import DANGEROUS_EXTENSIONS, safe_filename
from browser.models import BrowserState, BrowserUnavailable, Target, public_url
from browser.urlsafe import KNOWN_SITES, registrable, same_site, validate_url
from tests.browser_helpers import El, FakeWeb, Spec, make_engine, standard_web


@pytest.fixture
def web():
    return standard_web()


@pytest.fixture
def engine(web, tmp_path):
    e = make_engine(web, tmp_path)
    yield e
    e.shutdown()


# ---- URL safety --------------------------------------------------------------------------------------------------------------------

PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731


@pytest.mark.parametrize("url", ["file:///C:/Windows/win.ini", "javascript:alert(1)", "data:text/html,<script>1</script>", "vbscript:msgbox(1)", "blob:https://x/1",
                                 "ftp://example.com/x", "chrome://settings", "edge://flags", "about:blank", "view-source:https://example.com", "JaVaScRiPt:alert(1)",
                                 "  javascript:alert(1)", "https://user:pass@example.com/", "https://example.com@evil.test/", "https://exa mple.com/", "http://\u202eexample.com"])
def test_dangerous_schemes_and_tricks_are_refused(url):
    assert validate_url(url, resolver=PUBLIC).ok is False


@pytest.mark.parametrize("url", ["http://localhost:8000/dashboard", "http://127.0.0.1/", "http://[::1]/", "http://192.168.1.1/", "http://10.0.0.5/", "http://172.16.3.4/",
                                 "http://169.254.169.254/latest/meta-data/", "http://2130706433/", "http://0x7f000001/", "http://0177.0.0.1/", "http://127.1/",
                                 "http://[::ffff:127.0.0.1]/", "http://foo.localhost/", "http://printer.local/", "http://intranet/", "http://0.0.0.0/", "http://example.com:22/",
                                 "http://example.com:3306/"])
def test_local_and_private_targets_are_refused(url):
    assert validate_url(url, resolver=PUBLIC).ok is False


def test_dns_rebinding_a_public_name_pointing_inside_is_refused():
    assert validate_url("https://innocent.example/", resolver=lambda h: ["10.1.2.3"]).ok is False
    assert validate_url("https://innocent.example/", resolver=lambda h: ["93.184.216.34", "127.0.0.1"]).ok is False
    assert validate_url("https://innocent.example/", resolver=PUBLIC).ok is True


def test_private_hosts_can_be_allowed_only_by_configuration():
    assert validate_url("http://127.0.0.1:8000/", allow_private=True, resolver=None).ok is True


def test_normal_urls_are_accepted_and_normalized():
    d = validate_url("github.com/harsh/jarvis#readme", resolver=PUBLIC)
    assert d.ok and d.url == "https://github.com/harsh/jarvis" and d.host == "github.com"
    assert validate_url("HTTP://Example.COM/Path?q=1", resolver=PUBLIC).url == "http://example.com/Path?q=1"


def test_suspicious_but_allowed_links_are_flagged():
    assert "shortener" in validate_url("https://bit.ly/abc", resolver=PUBLIC).suspicious
    assert "look-alike" in validate_url("https://xn--pple-43d.com/", resolver=PUBLIC).suspicious
    assert "IP address" in validate_url("http://93.184.216.34/", resolver=PUBLIC).suspicious


@pytest.mark.parametrize("bad", ["", "   ", None, "not a url", "x" * 3000, "http://", "https:///path"])
def test_garbage_is_refused_without_raising(bad):
    assert validate_url(bad, resolver=PUBLIC).ok is False


def test_site_helpers_and_known_sites_are_all_safe():
    assert registrable("m.youtube.com") == "youtube.com" and registrable("www.bbc.co.uk") == "bbc.co.uk"
    assert same_site("https://m.youtube.com/watch", "https://www.youtube.com/") and not same_site("https://youtube.com.evil.test/", "https://www.youtube.com/")
    for name, url in KNOWN_SITES.items():
        assert validate_url(url, resolver=PUBLIC).ok, name


def test_public_url_strips_secrets():
    assert public_url("https://user:pw@example.com/a/b?token=abc#frag") == "https://example.com/a/b"
    assert public_url("javascript:alert(1)") == ""


# ---- lifecycle -----------------------------------------------------------------------------------------------------------------------

def test_browser_opens_on_demand_and_closes(engine, web):
    assert engine.state is BrowserState.CLOSED and web.launches == 0
    r = engine.open_url("https://example.com/")
    assert r.success and r.verified and engine.state is BrowserState.READY and web.launches == 1
    assert engine.close_browser().success and engine.state is BrowserState.CLOSED
    assert engine.close_browser().message == "The browser wasn't open."


def test_restart_gives_a_fresh_session(engine):
    engine.open_url("https://example.com/")
    first = engine.session.session_id
    engine.close_browser()
    engine.open_url("https://example.com/")
    assert engine.session.session_id != first and len(engine.session.tabs) == 1


def test_launch_failure_is_reported_truthfully(web, tmp_path):
    web.launch_error = BrowserUnavailable("I couldn't start a browser.")
    engine = make_engine(web, tmp_path)
    r = engine.open_url("https://example.com/")
    assert not r.success and "couldn't start a browser" in r.error and engine.state is BrowserState.ERROR
    web.launch_error = None
    assert engine.open_url("https://example.com/").success  # it can recover on the next request
    engine.shutdown()


def test_shutdown_closes_everything_and_refuses_new_work(engine, web):
    engine.open_url("https://example.com/")
    engine.shutdown()
    assert engine.state is BrowserState.CLOSED and engine.open_url("https://example.com/").success is False


# ---- navigation and verification ---------------------------------------------------------------------------------------------------------

def test_open_url_verifies_domain_and_title(engine):
    r = engine.open_url("https://github.com/")
    assert r.success and r.verified and r.data["title"] == "GitHub" and r.url == "https://github.com/"


def test_wrong_page_is_a_failure_not_a_success(engine, web):
    web.add("https://github.com/", Spec("Not GitHub", "x", redirect="https://example.com/"))
    r = engine.open_url("https://github.com/")
    assert not r.success and r.verified is False and "asked for github.com" in r.error


def test_http_errors_and_404_are_reported(engine, web):
    web.add("https://example.com/down", Spec("Error", "oops", status=503))
    assert "503" in engine.open_url("https://example.com/down").error
    assert "404" in engine.open_url("https://example.com/nowhere").error


def test_redirect_to_a_private_address_is_stopped(engine, web):
    web.add("https://example.com/r", Spec("r", "x", redirect="http://127.0.0.1:8000/dashboard"))
    r = engine.open_url("https://example.com/r")
    assert not r.success and "redirected somewhere I won't open" in r.error


def test_refused_urls_never_reach_the_browser(engine, web):
    for url in ("file:///C:/secrets.txt", "javascript:alert(1)", "http://localhost:8000/"):
        r = engine.open_url(url)
        assert not r.success and r.needs_confirmation is False
    assert web.launches == 0 and web.visits == []  # the browser was not even started


def test_back_forward_refresh(engine):
    engine.open_url("https://example.com/")
    engine.open_url("https://example.com/more")
    assert engine.go_back().url == "https://example.com/"
    assert engine.go_forward().url == "https://example.com/more"
    assert engine.refresh_page().success
    assert not engine.go_forward().success  # nothing further forward


def test_back_with_no_history_is_honest(engine):
    engine.open_url("https://example.com/")
    r = engine.go_back()
    assert not r.success and "nothing to go back" in r.error


def test_offline_navigation_times_out_after_bounded_retries(engine, web):
    web.offline = True
    r = engine.open_url("https://example.com/")
    assert not r.success and "too long" in r.error and r.retried == 2


def test_opening_the_same_site_twice_reuses_the_tab(engine):
    engine.open_url("https://www.youtube.com/")
    r = engine.open_url("https://www.youtube.com/")
    assert r.success and "already open" in r.message and len(engine.session.tabs) == 1


def test_tabs_open_switch_close_and_limits(engine, tmp_path, web):
    engine.open_url("https://example.com/")
    assert engine.open_url("https://github.com/", new_tab=True).success
    st = engine.status()
    assert st["tab_count"] == 2 and st["active_tab"] == "t2"
    assert engine.switch_tab("t1").success and engine.status()["url"] == "https://example.com/"
    assert not engine.switch_tab("t9").success
    assert engine.close_tab("t2").success and engine.status()["tab_count"] == 1
    small = make_engine(web, tmp_path, max_tabs=2)
    small.open_url("https://example.com/")
    small.open_new_tab()
    assert "2 tabs" in small.open_new_tab().error
    small.shutdown()


def test_a_page_that_opens_a_popup_makes_it_the_active_tab(engine):
    engine.open_url("https://example.com/")
    r = engine.click_element(Target(role="button", name="Open docs"))
    assert r.success and engine.status()["url"] == "https://example.com/docs" and engine.status()["tab_count"] == 2


def test_page_state_and_content_reading(engine):
    engine.open_url("https://github.com/login")
    st = engine.get_page_state()
    assert st.success and st.data["login_required"] is True and st.data["title"] == "Sign in to GitHub"
    page = engine.read_page()
    assert page.untrusted and page.data["forms"] == [{"label": "Username or email address", "type": "text"}, {"label": "Password", "type": "password"}]
    assert engine.get_current_url().message == "https://github.com/login" and engine.get_page_title().message == "Sign in to GitHub"


def test_opening_a_login_page_says_the_user_must_sign_in(engine):
    r = engine.open_url("https://github.com/login")
    assert r.success and r.data["login_required"] and "sign-in yourself" in r.message


def test_captcha_is_reported_never_solved(engine, web):
    web.add("https://example.com/c", Spec("Checking", "verify you are human", captcha=True))
    r = engine.open_url("https://example.com/c")
    assert r.data["captcha"] and "only you can complete" in r.message


# ---- elements --------------------------------------------------------------------------------------------------------------------------------

def test_find_text_and_find_element(engine):
    engine.open_url("https://example.com/")
    assert engine.find_text("illustrative").data["count"] == 1
    assert not engine.find_text("zebra").success
    r = engine.find_element("the download button")
    assert r.success and r.data["candidates"][0]["name"] == "Download report"
    assert not engine.find_element("teleport control").success


def test_click_verifies_that_something_changed(engine):
    engine.open_url("https://example.com/")
    ok = engine.click_element(Target(role="link", name="More information"))
    assert ok.success and ok.verified and engine.status()["url"] == "https://example.com/more"


def test_a_click_that_changes_nothing_is_a_failure(engine):
    engine.open_url("https://example.com/")
    r = engine.click_element(Target(role="button", name="Do nothing"))
    assert not r.success and r.verified is False and "nothing on the page changed" in r.error


def test_missing_element_is_reported(engine):
    engine.open_url("https://example.com/")
    r = engine.click_element(Target(role="button", name="Teleport"))
    assert not r.success and r.error == "Element not found."


def test_ambiguous_element_asks_instead_of_guessing(engine):
    engine.open_url("https://example.com/")
    r = engine.click_element(Target(name="Save"))
    assert not r.success and r.data["ambiguous"] is True  # two "Save" buttons that do different things
    assert engine.click_element(Target(role="button", name="Save", index=1)).success  # the user said "the second one"


def test_disabled_controls_are_not_clicked(engine, web):
    engine.open_url("https://example.com/")
    r = engine.click_element(Target(role="button", name="Disabled"))
    assert not r.success and "disabled" in r.error and "Disabled" not in web.clicks


def test_type_text_reads_back_and_never_types_into_passwords(engine, web):
    engine.open_url("https://example.com/")
    assert engine.type_text(Target(role="textbox", name="Search"), "hello").success
    engine.open_url("https://github.com/login")
    r = engine.type_text(Target(role="textbox", name="Password"), "hunter2")
    assert not r.success and "never type into password" in r.error
    assert ("Password", "hunter2") not in web.typed
    assert not engine.type_text(Target(role="textbox", name="Username or email address"), "x" * 600).success  # length bound


def test_press_key_is_restricted_to_simple_keys(engine, web):
    engine.open_url("https://example.com/")
    assert engine.press_key("Escape").success
    for key in ("Control+L", "Alt+F4", "F12", "Meta", "Delete", "Control+Shift+I"):
        assert not engine.press_key(key).success
    assert web.keys == ["Escape"]


def test_scroll_verifies_movement_and_reports_the_edges(engine):
    engine.open_url("https://example.com/")
    assert engine.scroll("down", 1000).data["position"] == [1000, 3000]
    assert engine.scroll("bottom").success and "bottom" in engine.scroll("down").message
    assert engine.scroll("top").success and "top" in engine.scroll("up").message
    assert not engine.scroll("sideways").success


def test_wait_for_element(engine):
    engine.open_url("https://example.com/")
    assert engine.wait_for_element(Target(role="button", name="Download report"), 1).success
    assert not engine.wait_for_element(Target(role="button", name="Never appears"), 1).success


def test_screenshots_follow_the_mode_and_skip_sign_in_pages(web, tmp_path):
    engine = make_engine(web, tmp_path, screenshot_mode="memory")
    engine.open_url("https://example.com/")
    r = engine.take_screenshot()
    assert r.success and r.data["width"] == 1280 and r.data["stored"] is False and not list((tmp_path / "shots").glob("*"))
    engine.open_url("https://github.com/login")
    assert not engine.take_screenshot().success  # sign-in pages are never captured
    engine.shutdown()
    off = make_engine(web, tmp_path, screenshot_mode="off")
    assert "turned off" in off.take_screenshot().error
    disk = make_engine(web, tmp_path, screenshot_mode="disk")
    disk.open_url("https://example.com/")
    assert Path(disk.take_screenshot().data["path"]).is_file()
    off.shutdown()
    disk.shutdown()


# ---- downloads / uploads -------------------------------------------------------------------------------------------------------------------------

def test_download_is_verified_recorded_and_never_executed(engine, tmp_path):
    engine.open_url("https://example.com/")
    r = engine.click_element(Target(role="button", name="Download report"))
    assert r.success and r.verified and "report.pdf" in r.message
    saved = tmp_path / "downloads" / "report.pdf"
    assert saved.read_bytes().startswith(b"%PDF")
    rec = engine.downloads.records[-1]
    assert rec.source_url == "https://example.com/files/report.pdf" and rec.sha256 and rec.size > 0 and "token" not in rec.source_url
    logged = [json.loads(line) for line in (tmp_path / "browser_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["action"] == "download" and e["verified"] for e in logged)


def test_downloads_never_overwrite(engine, tmp_path):
    engine.open_url("https://example.com/")
    engine.click_element(Target(role="button", name="Download report"))
    engine.click_element(Target(role="button", name="Download report"))
    assert sorted(p.name for p in (tmp_path / "downloads").iterdir()) == ["report (1).pdf", "report.pdf"]


def test_executable_downloads_are_refused_and_reported(engine, tmp_path):
    engine.open_url("https://example.com/")
    r = engine.click_element(Target(role="button", name="Run installer"))
    assert not r.success and "can run code" in r.error
    assert not (tmp_path / "downloads").exists() or list((tmp_path / "downloads").iterdir()) == []
    assert engine.downloads.records[-1].blocked


@pytest.mark.parametrize("name,expected", [("../../evil.txt", "evil.txt"), ("..\\..\\x.bat", "x.bat"), ("a<b>c:d.txt", "a_b_c_d.txt"), ("CON.txt", "_CON.txt"), ("", "download"),
                                           ("report\u202efdp.exe", "report_fdp.exe"), ("x" * 300 + ".txt", None)])
def test_download_names_are_sanitized(name, expected):
    clean = safe_filename(name)
    assert "/" not in clean and "\\" not in clean and len(clean) <= 120
    if expected:
        assert clean == expected


def test_dangerous_extension_list_covers_the_usual_suspects():
    assert {".exe", ".bat", ".ps1", ".msi", ".js", ".vbs", ".lnk", ".dll", ".jar", ".scr"} <= DANGEROUS_EXTENSIONS


def test_upload_only_from_the_approved_folder(engine, tmp_path, web):
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "cv.pdf").write_bytes(b"%PDF")
    (tmp_path / "uploads" / "tool.exe").write_bytes(b"MZ")
    engine.open_url("https://example.com/")
    ok = engine.upload_file(Target(label="file"), "cv.pdf")
    assert ok.success and "not submitted yet" in ok.message and web.uploads[0].endswith("cv.pdf")
    for bad in ("../secret.txt", "C:\\Users\\harsh\\.ssh\\id_rsa", "tool.exe", "missing.pdf", "/etc/passwd"):
        assert not engine.upload_file(Target(label="file"), bad).success
    assert len(web.uploads) == 1


# ---- failure and recovery -------------------------------------------------------------------------------------------------------------------------

def test_browser_crash_during_a_safe_read_recovers_and_retries(engine, web):
    engine.open_url("https://example.com/")
    web.crash_on_next = True
    r = engine.get_page_state()
    assert r.success and r.recovered and r.retried == 1 and engine.recoveries == 1 and engine.crashes == 1
    assert web.launches == 2 and engine.state is BrowserState.READY


def test_browser_crash_during_a_click_is_never_replayed(engine, web):
    engine.open_url("https://example.com/")
    web.crash_on_next = True
    r = engine.click_element(Target(role="button", name="Buy now"))
    assert not r.success and r.recovered and "Please ask again" in r.error
    assert web.clicks == []  # the risky action was not repeated blindly
    assert engine.get_page_state().success  # the browser is back at the page that was open


def test_recovery_failure_is_reported(engine, web):
    engine.open_url("https://example.com/")
    web.crash_on_next = True
    web.launch_error = BrowserUnavailable("no browser")
    r = engine.get_page_state()
    assert not r.success and "couldn't restart" in r.error and engine.state is BrowserState.ERROR


def test_one_tab_crashing_is_replaced_without_restarting_the_browser(engine, web):
    engine.open_url("https://example.com/")
    tab = engine.session.tabs[engine.session.active]
    tab.page.crashed = True
    assert engine.get_page_state().success and web.launches == 1 and engine.recoveries == 1


def test_a_stuck_action_can_be_stopped(engine):
    engine.open_url("https://example.com/")
    engine.stop_current_action()
    assert engine.get_page_state().success  # the flag is cleared at the start of the next action


def test_concurrent_callers_are_serialized_safely(engine):
    engine.open_url("https://example.com/")
    results = []

    def worker():
        results.append(engine.get_page_state().success)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == [True] * 8


def test_status_exposes_no_page_content_or_cookies(engine):
    engine.open_url("https://example.com/?token=abc#frag")
    dump = json.dumps(engine.status())
    assert "token" not in dump and "abc" not in dump and "cookie" not in dump.lower() and "illustrative" not in dump


def test_metrics_and_log_record_actions_without_secrets(engine, tmp_path):
    from backend.core.metrics import metrics

    metrics.reset()
    engine.open_url("https://example.com/?token=SECRET123")
    engine.click_element(Target(role="button", name="Do nothing"))
    snap = metrics.snapshot()
    assert "browser.startup_ms" in snap["timers"] and "browser.navigation_ms" in snap["timers"] and snap["counters"]["browser.failures"] == 1
    raw = (tmp_path / "browser_log.jsonl").read_text(encoding="utf-8")
    assert "SECRET123" not in raw
    rec = json.loads(raw.splitlines()[0])
    assert {"timestamp", "session_id", "action", "url", "duration_ms", "verified", "result"} <= set(rec)
