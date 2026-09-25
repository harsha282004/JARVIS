"""Redaction, structured logging, event bus, recovery/backoff, health monitor, state stores, preferences, privacy, notifications, metrics."""

import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.core.events import EventBus, SystemEvent
from backend.core.health import Health, HealthMonitor, OverallStatus, ServiceState
from backend.core.logging import JsonFormatter, RedactingFilter, correlation, log_event
from backend.core.metrics import Metrics
from backend.core.notifications import Level, NotificationCenter
from backend.core.preferences import PreferenceStore, Preferences, in_window, parse_hhmm
from backend.core.privacy import PrivacyController, PrivacyMode, VoiceIndicator, voice_indicator
from backend.core.recovery import BackoffPolicy, SupervisedService, Supervisor, retry_call
from backend.core.redaction import REDACTED, redact, redact_mapping
from backend.core.state_store import JsonFile, JsonLines

IST = ZoneInfo("Asia/Kolkata")


# ---- redaction & logging -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,secret", [
    ("client_secret=GOCSPX-abcdefghijk123", "GOCSPX-abcdefghijk123"),
    ("Authorization: Bearer abcdefghijklmnop1234", "abcdefghijklmnop1234"),
    ('{"refresh_token": "1//0gAbCdEfGhIjKlMnOpQrStUv"}', "1//0gAbCdEfGhIjKlMnOpQrStUv"),
    ("postgresql://jarvis:hunter2pw@db.example.com/x", "hunter2pw"),
    ("token=ya29.a0AfH6SMBxxxxxxxxxxxxxxxxxxxx", "ya29.a0AfH6SMBxxxxxxxxxxxxxxxxxxxx"),
    ("password: 'correct horse battery'", "correct horse battery"),
])
def test_secrets_are_redacted(text, secret):
    out = redact(text)
    assert secret not in out and REDACTED in out and redact(out) == out  # idempotent


def test_redact_mapping_masks_secret_keys():
    assert redact_mapping({"access_token": "abc", "n": {"password": "x", "ok": "fine"}}) == {"access_token": REDACTED, "n": {"password": REDACTED, "ok": "fine"}}


def test_json_log_has_required_fields_and_redacts(capsys):
    logger = logging.getLogger("test.jsonlog")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    stream = logging.StreamHandler()
    stream.setFormatter(JsonFormatter())
    stream.addFilter(RedactingFilter())
    logger.addHandler(stream)
    with correlation("req-1"):
        log_event(logger, "gmail_failed", logging.ERROR, message="failed token=abcdefghijklmnop", service="gmail")
    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert {"timestamp", "severity", "component", "event", "correlation_id"} <= set(line)
    assert line["event"] == "gmail_failed" and line["severity"] == "ERROR" and line["correlation_id"] == "req-1" and line["service"] == "gmail"
    assert "abcdefghijklmnop" not in json.dumps(line)
    logger.removeHandler(stream)


# ---- event bus ---------------------------------------------------------------------------------------------------------------------

def test_event_bus_isolates_failing_handlers_and_unsubscribes():
    bus, seen = EventBus(), []
    bus.subscribe(SystemEvent.TASK_CREATED, lambda e: 1 / 0)
    unsub = bus.subscribe(SystemEvent.TASK_CREATED, lambda e: seen.append(e.payload["task_id"]))
    bus.subscribe(None, lambda e: seen.append("all"))
    bus.publish(SystemEvent.TASK_CREATED, task_id="t1")  # the first handler raises: publisher and others unaffected
    unsub()
    bus.publish(SystemEvent.TASK_CREATED, task_id="t2")
    assert seen == ["t1", "all", "all"] and len(bus.recent(SystemEvent.TASK_CREATED)) == 2


# ---- recovery ----------------------------------------------------------------------------------------------------------------------

def test_retry_call_backs_off_exponentially_and_gives_up():
    sleeps, calls = [], []

    def fail():
        calls.append(1)
        raise ConnectionError("x")

    with pytest.raises(ConnectionError):
        retry_call(fail, BackoffPolicy(1.0, 2.0, 60.0, 4), sleep=sleeps.append)
    assert len(calls) == 4 and sleeps == [1.0, 2.0, 4.0]


def test_retry_call_succeeds_after_transient_failure():
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise OSError("down")
        return "ok"

    assert retry_call(flaky, BackoffPolicy(0.1, 2.0, 1.0, 5), sleep=lambda s: None) == "ok"


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_supervisor_restarts_with_backoff_gives_up_then_retries_after_cooldown():
    clock, restarts, alive = Clock(), [], {"ok": False}
    sup = Supervisor(clock=clock)
    sup.add(SupervisedService("voice", lambda: alive["ok"], lambda: restarts.append(clock.t), BackoffPolicy(2.0, 2.0, 60.0, 3, 100.0)))
    for _ in range(3):  # ticks in the same instant: only one restart (no tight loop)
        sup.tick()
    assert restarts == [0.0]
    for t in (1, 2.1, 5, 6.1, 20, 40):
        clock.t = t
        sup.tick()
    assert restarts == [0.0, 2.1, 6.1] and sup.get("voice").failed  # exactly max_attempts, spaced 2s, 4s
    clock.t = 60
    sup.tick()
    assert len(restarts) == 3  # in cooldown: no more attempts
    clock.t = 150
    sup.tick()
    assert len(restarts) == 4  # cooldown over: a fresh series begins


def test_supervisor_recovery_resets_and_notifies_and_isolates_services():
    clock, events, alive = Clock(), [], {"ok": False}
    sup = Supervisor(clock=clock)
    sup.add(SupervisedService("a", lambda: alive["ok"], lambda: 1 / 0, on_failed=lambda n: events.append(("failed", n)), on_recovered=lambda n: events.append(("ok", n))))
    sup.add(SupervisedService("b", lambda: True, lambda: None))
    sup.tick()  # a's restart raises: must not escape or stop b
    alive["ok"] = True
    sup.tick()
    assert events == [("failed", "a"), ("ok", "a")] and sup.get("a").attempts == 0


# ---- health monitor ----------------------------------------------------------------------------------------------------------------

def test_health_states_overall_and_events():
    bus, states = EventBus(), {"db": ServiceState.HEALTHY}
    failed = []
    bus.subscribe(SystemEvent.INTEGRATION_FAILED, lambda e: failed.append(e.payload["service"]))
    mon = HealthMonitor(bus)
    mon.register("database", lambda: Health(states["db"]), critical=True)
    mon.register("gmail", lambda: Health(ServiceState.DISABLED, "not set up"))
    mon.register("calendar", lambda: 1 / 0)
    mon.check_all()
    assert mon.report("calendar").state is ServiceState.FAILED  # a check that raises is a failed service, not a crash
    assert mon.overall() is OverallStatus.DEGRADED  # a non-critical failure degrades, it does not make JARVIS offline
    states["db"] = ServiceState.DISCONNECTED
    mon.check_all()
    assert mon.overall() is OverallStatus.OFFLINE and failed.count("database") == 1
    states["db"] = ServiceState.HEALTHY
    mon.check_all()
    assert mon.report("gmail").state is ServiceState.DISABLED  # disabled never reads as healthy


def test_no_reports_means_starting_not_online():
    assert HealthMonitor().overall() is OverallStatus.STARTING


# ---- state stores ------------------------------------------------------------------------------------------------------------------

def test_json_file_atomic_and_corruption_recovery(tmp_path):
    f = JsonFile(tmp_path / "s.json", {"a": 1})
    assert f.read() == {"a": 1}
    f.write({"a": 2})
    assert JsonFile(tmp_path / "s.json", {}).read() == {"a": 2} and not list(tmp_path.glob("*.tmp"))
    (tmp_path / "s.json").write_text("{not json", encoding="utf-8")
    assert f.read() == {"a": 1} and (tmp_path / "s.json.corrupt").exists()  # startup does not crash


def test_json_lines_rotation_and_bad_lines(tmp_path):
    log = JsonLines(tmp_path / "l.jsonl", max_bytes=200, backups=2)
    for i in range(40):
        log.append({"i": i, "pad": "x" * 20})
    assert (tmp_path / "l.jsonl.1").exists()
    with (tmp_path / "l.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("garbage\n")
    assert all("i" in r for r in log.read())


# ---- preferences -------------------------------------------------------------------------------------------------------------------

def test_quiet_window_crossing_midnight():
    assert in_window(parse_hhmm("23:30"), parse_hhmm("22:00"), parse_hhmm("07:00"))
    assert in_window(parse_hhmm("06:59"), parse_hhmm("22:00"), parse_hhmm("07:00"))
    assert not in_window(parse_hhmm("12:00"), parse_hhmm("22:00"), parse_hhmm("07:00"))


def test_preferences_persist_and_survive_corruption(tmp_path):
    p = PreferenceStore(tmp_path / "p.json")
    p.mute("Newsletters!")
    p.set_reminder_lead("project deadline", 1440)
    p.set_voice_cutoff("22:00")
    again = PreferenceStore(tmp_path / "p.json")
    assert again.is_muted("weekly newsletters digest") is False or again.get().muted_categories == ["newsletters"]
    assert again.reminder_lead_for("JARVIS project deadline") == 1440
    (tmp_path / "p.json").write_text(json.dumps({"quiet_start": "bogus", "voice_cutoff": "99:99"}), encoding="utf-8")
    fixed = PreferenceStore(tmp_path / "p.json").get()
    assert fixed.quiet_start == "22:00" and fixed.voice_cutoff is None  # invalid values fall back safely
    with pytest.raises(ValueError):
        p.set_workday("18:00", "09:00")


def test_voice_not_allowed_after_cutoff():
    p = PreferenceStore()
    p.set_voice_cutoff("21:00")
    p.set_quiet_hours("23:00", "07:00")
    at = lambda h: datetime(2026, 9, 24, h, 0, tzinfo=IST)  # noqa: E731
    assert p.voice_allowed(at(20), IST) and not p.voice_allowed(at(22), IST) and not p.voice_allowed(at(1), IST)


# ---- privacy & voice indicator -----------------------------------------------------------------------------------------------------

def test_private_mode_persists_across_restart(tmp_path):
    bus, seen = EventBus(), []
    bus.subscribe(SystemEvent.PRIVACY_CHANGED, lambda e: seen.append(e.payload["new"]))
    PrivacyController(tmp_path / "p.json", bus).set_mode(PrivacyMode.PRIVATE)
    restarted = PrivacyController(tmp_path / "p.json", default=PrivacyMode.ACTIVE)
    assert restarted.mode is PrivacyMode.PRIVATE and not restarted.capabilities.microphone and not restarted.capabilities.external_monitoring
    assert seen == ["private"]


@pytest.mark.parametrize("runtime,voice,mic,mode,expected", [
    ("running", "waiting", True, PrivacyMode.ACTIVE, VoiceIndicator.LISTENING),
    ("running", "waiting", False, PrivacyMode.ACTIVE, VoiceIndicator.UNAVAILABLE),  # never "listening" without an open stream
    ("running", "thinking", False, PrivacyMode.ACTIVE, VoiceIndicator.PROCESSING),
    ("running", "speaking", False, PrivacyMode.ACTIVE, VoiceIndicator.SPEAKING),
    ("paused", None, False, PrivacyMode.ACTIVE, VoiceIndicator.PAUSED),
    ("running", "waiting", True, PrivacyMode.PRIVATE, VoiceIndicator.MICROPHONE_DISABLED),
    ("error", None, False, PrivacyMode.ACTIVE, VoiceIndicator.UNAVAILABLE),
    ("starting", None, False, PrivacyMode.ACTIVE, VoiceIndicator.UNAVAILABLE),
])
def test_voice_indicator_never_lies(runtime, voice, mic, mode, expected):
    assert voice_indicator(runtime, voice, mic, mode) is expected


# ---- notification center -----------------------------------------------------------------------------------------------------------

class World:
    def __init__(self, tmp_path=None, hour=12):
        self.now = datetime(2026, 9, 24, hour, 0, tzinfo=IST)
        self.sent, self.fail = [], set()
        self.prefs = PreferenceStore()
        self.privacy = PrivacyController()
        self.file = tmp_path / "n.json" if tmp_path else None
        self.center = self.make()

    def make(self):
        def deliver(ch, n):
            if ch in self.fail:
                raise RuntimeError("down")
            self.sent.append((ch, n.title))

        return NotificationCenter(preferences=self.prefs, zone=IST, deliver=deliver, privacy=self.privacy, state_file=self.file, clock=lambda: self.now)


def test_levels_route_correctly():
    w = World()
    w.center.submit(dedupe_key="c", level=Level.CRITICAL, title="Critical", body="b")
    w.center.submit(dedupe_key="i", level=Level.IMPORTANT, title="Important", body="b")
    w.center.submit(dedupe_key="n", level=Level.NORMAL, title="Normal", body="b")
    w.center.submit(dedupe_key="l", level=Level.LOW, title="Low", body="b")
    assert {t for _, t in w.sent} == {"Critical", "Important"}
    assert [n.status for n in w.center.history()] == ["delivered", "delivered", "stored", "stored"]
    assert [t for t, in [(n.title,) for n in w.center.for_briefing()]] == ["Critical", "Important"]


def test_duplicates_suppressed_until_content_changes_or_escalates():
    w = World()
    assert w.center.submit(dedupe_key="k", level=Level.IMPORTANT, title="T", body="due 5pm")
    assert w.center.submit(dedupe_key="k", level=Level.IMPORTANT, title="T", body="due 5pm") is None
    assert w.center.submit(dedupe_key="k", level=Level.IMPORTANT, title="T", body="due 6pm")  # changed: news
    assert w.center.submit(dedupe_key="k", level=Level.CRITICAL, title="T", body="due 6pm")  # escalated


def test_quiet_hours_defer_important_but_not_critical_then_release():
    w = World(hour=23)
    w.center.submit(dedupe_key="i", level=Level.IMPORTANT, title="Imp", body="b")
    w.center.submit(dedupe_key="c", level=Level.CRITICAL, title="Crit", body="b")
    assert [t for _, t in w.sent] == ["Crit", "Crit"]  # critical breaks through on both channels
    w.now = w.now.replace(day=25, hour=8)
    assert w.center.release_deferred() == 1 and ("desktop", "Imp") in w.sent


def test_voice_dropped_after_cutoff_but_desktop_kept():
    w = World(hour=21)
    w.prefs.set_voice_cutoff("20:00")
    w.center.submit(dedupe_key="i", level=Level.IMPORTANT, title="Imp", body="b")
    assert w.sent == [("desktop", "Imp")]


def test_failed_delivery_is_kept_and_retried():
    w = World()
    w.fail = {"desktop", "voice"}
    n = w.center.submit(dedupe_key="i", level=Level.IMPORTANT, title="Imp", body="b")
    assert n.status == "deferred" and w.sent == []
    w.fail = set()
    assert w.center.release_deferred() == 1 and w.center.history()[0].status == "delivered"


def test_privacy_mode_limits_notifications():
    w = World()
    w.privacy.set_mode(PrivacyMode.PRIVATE)
    w.center.submit(dedupe_key="i", level=Level.IMPORTANT, title="Imp", body="b")
    w.center.submit(dedupe_key="c", level=Level.CRITICAL, title="Crit", body="b")
    assert {t for _, t in w.sent} == {"Crit"}


def test_muted_and_important_keyword_preferences():
    w = World()
    w.prefs.mute("newsletter")
    w.prefs.add_important("hackathon")
    assert w.center.submit(dedupe_key="a", level=Level.IMPORTANT, title="Weekly newsletter", body="b").status == "suppressed"
    w.center.submit(dedupe_key="b", level=Level.NORMAL, title="Hackathon opens", body="b")
    assert ("desktop", "Hackathon opens") in w.sent  # promoted to important by the user's preference


def test_history_and_acknowledgement_persist(tmp_path):
    w = World(tmp_path)
    n = w.center.submit(dedupe_key="i", level=Level.IMPORTANT, title="Imp", body="b")
    assert w.center.acknowledge(n.notification_id) and not w.center.acknowledge(n.notification_id)
    again = w.make()
    assert again.history()[0].status == "acknowledged"


# ---- metrics -----------------------------------------------------------------------------------------------------------------------

def test_metrics_counters_and_timers():
    m = Metrics()
    m.incr("llm_calls")
    m.incr("llm_calls")
    for v in (10, 20, 30):
        m.observe("stt_ms", v)
    snap = m.snapshot()
    assert snap["counters"]["llm_calls"] == 2 and snap["timers"]["stt_ms"]["mean_ms"] == 20 and snap["timers"]["stt_ms"]["max_ms"] == 30
