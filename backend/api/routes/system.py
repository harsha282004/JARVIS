"""Local dashboard API: real health, privacy, notification, intelligence and performance data from the running JARVIS.

Security: the API listens on the loopback address only. Every request's Host header must be a loopback name (this blocks DNS-rebinding
pages), and every route except /health and the dashboard page needs the per-run token (the dashboard embeds it, so only a page served by
this server can call the API). Changing anything (privacy mode, acknowledging notifications) needs the token as well.
Without a running launcher there is no context, and the routes answer 503 rather than invent data.
"""

import hmac
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from backend.core.context import AppContext, get_context
from backend.core.metrics import metrics
from backend.core.privacy import INDICATOR_TEXT, PrivacyMode, voice_indicator
from backend.core.sysmetrics import working_set_mb

router = APIRouter()
_DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard.html"
_LOOPBACK = {"127.0.0.1", "localhost", "[::1]", "::1", "testserver"}


def _host_only(host_header: str) -> str:
    host = host_header.strip().lower()
    return host.rsplit(":", 1)[0] if host.count(":") == 1 or (host.startswith("[") and "]:" in host) else host


def check_host(request: Request) -> None:
    if _host_only(request.headers.get("host", "")) not in _LOOPBACK:
        raise HTTPException(status_code=403, detail="forbidden host")


def context() -> AppContext:
    ctx = get_context()
    if ctx is None:
        raise HTTPException(status_code=503, detail="JARVIS is not running in this process (start it with python -m desktop.launcher)")
    return ctx


def authorized(x_jarvis_token: str = Header(default=""), ctx: AppContext = Depends(context)) -> AppContext:
    if not hmac.compare_digest(x_jarvis_token.encode(), ctx.api_token.encode()):
        raise HTTPException(status_code=401, detail="missing or wrong token")
    return ctx


@router.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(check_host)])
def dashboard(ctx: AppContext = Depends(context)) -> HTMLResponse:
    html = _DASHBOARD.read_text(encoding="utf-8").replace("__JARVIS_TOKEN__", ctx.api_token)
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"})


@router.get("/status", dependencies=[Depends(check_host)])
def status(ctx: AppContext = Depends(authorized)) -> dict:
    """Runtime state, the truthful voice/microphone indicator, privacy mode and the overall health, all from the running system."""
    manager = ctx.manager
    rs = manager.status() if manager is not None else None
    mode = ctx.privacy.mode if ctx.privacy is not None else PrivacyMode.ACTIVE
    indicator = voice_indicator(rs.state.value, rs.voice_state, rs.microphone_active, mode) if rs is not None else None
    return {
        "runtime": rs.state.value if rs else None,
        "last_error": rs.last_error if rs else None,
        "voice_state": rs.voice_state if rs else None,
        "microphone_active": rs.microphone_active if rs else False,
        "voice_indicator": indicator.value if indicator else None,
        "voice_indicator_text": INDICATOR_TEXT[indicator] if indicator else None,
        "privacy_mode": mode.value,
        "overall": ctx.health.overall().value if ctx.health is not None else None,
        "offline_mode": ctx.settings.JARVIS_OFFLINE_MODE,
    }


@router.get("/health/services", dependencies=[Depends(check_host)])
def health_services(ctx: AppContext = Depends(authorized)) -> dict:
    """Runs the health checks now (the slow ones are cached inside the monitor's checks, so this stays fast) and returns the results."""
    if ctx.health is None:
        raise HTTPException(status_code=503, detail="health monitor is not running")
    ctx.health.check_all()
    return ctx.health.as_dict()


class PrivacyChange(BaseModel):
    mode: PrivacyMode


@router.get("/privacy", dependencies=[Depends(check_host)])
def get_privacy(ctx: AppContext = Depends(authorized)) -> dict:
    caps = ctx.privacy.capabilities
    return {"mode": ctx.privacy.mode.value, "microphone": caps.microphone, "wake_word": caps.wake_word, "external_monitoring": caps.external_monitoring,
            "notifications": caps.notifications}


@router.post("/privacy", dependencies=[Depends(check_host)])
def set_privacy(change: PrivacyChange, ctx: AppContext = Depends(authorized)) -> dict:
    ctx.privacy.set_mode(change.mode)
    return get_privacy(ctx)


@router.get("/notifications", dependencies=[Depends(check_host)])
def notifications(ctx: AppContext = Depends(authorized)) -> dict:
    if ctx.center is None:
        raise HTTPException(status_code=503, detail="notifications are not running")
    return {"history": [n.to_dict() for n in reversed(ctx.center.history(50))]}


@router.post("/notifications/{notification_id}/acknowledge", dependencies=[Depends(check_host)])
def acknowledge(notification_id: str, ctx: AppContext = Depends(authorized)) -> dict:
    if ctx.center is None or not ctx.center.acknowledge(notification_id):
        raise HTTPException(status_code=404, detail="no such unacknowledged notification")
    return {"acknowledged": notification_id}


@router.get("/intelligence/summary", dependencies=[Depends(check_host)])
def intelligence_summary(ctx: AppContext = Depends(authorized)) -> dict:
    if ctx.intelligence is None:
        raise HTTPException(status_code=503, detail="the personal intelligence layer is not enabled")
    return ctx.intelligence.dashboard()


@router.get("/intelligence/why", dependencies=[Depends(check_host)])
def intelligence_why(query: str = "", ctx: AppContext = Depends(authorized)) -> dict:
    """Evidence-based explanation of the most relevant recent recommendation. Never hidden reasoning: only the recorded facts and sources."""
    if ctx.intelligence is None:
        raise HTTPException(status_code=503, detail="the personal intelligence layer is not enabled")
    log = ctx.intelligence.explanations
    item = log.find(query or None)
    return {"why": log.why(item), "sources": log.sources(item)}


@router.get("/audit", dependencies=[Depends(check_host)])
def audit(limit: int = 50, ctx: AppContext = Depends(authorized)) -> dict:
    return {"entries": ctx.audit.entries(max(1, min(limit, 200))) if ctx.audit is not None else []}


@router.get("/metrics", dependencies=[Depends(check_host)])
def get_metrics(ctx: AppContext = Depends(authorized)) -> dict:
    data = metrics.snapshot()
    data["memory_mb"] = working_set_mb()
    return data


# ---- Integration Center (Phase 18) ------------------------------------------------------------------------------------------

def _hub(ctx: AppContext):
    if ctx.hub is None:
        raise HTTPException(status_code=503, detail="the integration hub is not enabled")
    return ctx.hub


def _known(ctx: AppContext, name: str):
    hub = _hub(ctx)
    if hub.registry.adapter(name) is None:
        raise HTTPException(status_code=404, detail="no such integration")
    return hub


class DisconnectRequest(BaseModel):
    purge: bool = False
    revoke: bool = True


class PermissionRequest(BaseModel):
    permission: str
    granted: bool


@router.get("/integrations", dependencies=[Depends(check_host)])
def integrations(ctx: AppContext = Depends(authorized)) -> dict:
    """The real state of every integration: status, enabled, last sync, granted permissions, last error. Never a token or credential."""
    return {"integrations": [i.to_dict() for i in _hub(ctx).registry.all_info()]}


@router.post("/integrations/{name}/enable", dependencies=[Depends(check_host)])
def enable_integration(name: str, ctx: AppContext = Depends(authorized)) -> dict:
    _known(ctx, name).registry.set_enabled(name, True)
    return _hub(ctx).registry.info(name).to_dict()


@router.post("/integrations/{name}/disable", dependencies=[Depends(check_host)])
def disable_integration(name: str, ctx: AppContext = Depends(authorized)) -> dict:
    _known(ctx, name).registry.set_enabled(name, False)
    return _hub(ctx).registry.info(name).to_dict()


@router.post("/integrations/{name}/sync", dependencies=[Depends(check_host)])
def sync_integration(name: str, ctx: AppContext = Depends(authorized)) -> dict:
    outcome = _known(ctx, name).engine.sync(name, force=True)
    return {"ok": outcome.ok, "created": outcome.created, "updated": outcome.updated, "unchanged": outcome.unchanged, "removed": outcome.removed,
            "skipped": outcome.skipped, "error_kind": outcome.error_kind, "error": outcome.error, "info": _hub(ctx).registry.info(name).to_dict()}


@router.post("/integrations/{name}/connect", status_code=202, dependencies=[Depends(check_host)])
def connect_integration(name: str, ctx: AppContext = Depends(authorized)) -> dict:
    """Starts the sign-in in the background (a browser window may open on this computer). Poll /integrations for the result."""
    hub = _known(ctx, name)
    if hub.registry.info(name).status.value == "authenticating":
        return hub.registry.info(name).to_dict()
    hub.registry.connect_async(name)
    return hub.registry.info(name).to_dict()


@router.post("/integrations/{name}/disconnect", dependencies=[Depends(check_host)])
def disconnect_integration(name: str, request: DisconnectRequest, ctx: AppContext = Depends(authorized)) -> dict:
    return _known(ctx, name).disconnect(name, revoke=request.revoke, purge=request.purge).to_dict()


@router.post("/integrations/{name}/permissions", dependencies=[Depends(check_host)])
def set_integration_permission(name: str, request: PermissionRequest, ctx: AppContext = Depends(authorized)) -> dict:
    from integrations.hub.models import Permission

    hub = _known(ctx, name)
    try:
        permission = Permission(request.permission)
    except ValueError:
        raise HTTPException(status_code=422, detail="unknown permission") from None
    if permission not in hub.registry.adapter(name).permissions:
        raise HTTPException(status_code=422, detail="that integration has no such permission")
    (hub.registry.grant if request.granted else hub.registry.revoke_permission)(name, permission)
    return hub.registry.info(name).to_dict()


# ---- voice (Phase 19) ---------------------------------------------------------------------------------------------------------
# State names, short text the user said/heard, timings and error kinds. Never audio, never secrets.


def _voice(ctx: AppContext):
    voice = getattr(ctx, "voice", None)
    if voice is None:
        raise HTTPException(status_code=503, detail="voice control is not running")
    return voice


@router.get("/voice", dependencies=[Depends(check_host)])
def voice_status(ctx: AppContext = Depends(authorized)) -> dict:
    voice = _voice(ctx)
    snap = voice.snapshot()
    rs = ctx.manager.status() if ctx.manager is not None else None
    snap["runtime"] = rs.state.value if rs else None
    if rs is not None and snap["microphone"] in ("MICROPHONE_CONNECTED", "MICROPHONE_UNKNOWN") and (
            rs.state.value in ("paused", "stopped") or (rs.state.value == "running" and not rs.microphone_active and snap["microphone"] == "MICROPHONE_CONNECTED")):
        snap["microphone"] = "MICROPHONE_CLOSED"  # the engine is not running (paused / private / stopped): the microphone is released
    return snap


@router.post("/voice/settings", dependencies=[Depends(check_host)])
def voice_settings(changes: dict, ctx: AppContext = Depends(authorized)) -> dict:
    """Change persisted voice settings (wake sensitivity, TTS speed/volume, DND, ...). Invalid values change nothing (400)."""
    try:
        _voice(ctx).update(changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return voice_status(ctx)


@router.post("/voice/interrupt", dependencies=[Depends(check_host)])
def voice_interrupt(ctx: AppContext = Depends(authorized)) -> dict:
    _voice(ctx).interrupt()
    return {"interrupted": True}


@router.post("/voice/activate", dependencies=[Depends(check_host)])
def voice_activate(ctx: AppContext = Depends(authorized)) -> dict:
    """"Talk to JARVIS" without saying the wake word. Only while the runtime is running (it never opens a paused or private microphone)."""
    return {"activated": bool(ctx.manager is not None and ctx.manager.request_activation())}


@router.get("/voice/log", dependencies=[Depends(check_host)])
def voice_log(limit: int = 50, ctx: AppContext = Depends(authorized)) -> dict:
    return {"events": _voice(ctx).log.recent(max(1, min(limit, 200)))}


# ---- browser (Phase 20) ---------------------------------------------------------------------------------------------------------
# State only: tabs as scheme://host/path (no query strings), titles, the last action and whether it was verified. Never page content,
# cookies, credentials or screenshots.


def _browser(ctx: AppContext):
    browser = getattr(ctx, "browser", None)
    if browser is None:
        raise HTTPException(status_code=503, detail="the browser agent is not enabled")
    return browser


@router.get("/browser", dependencies=[Depends(check_host)])
def browser_status(ctx: AppContext = Depends(authorized)) -> dict:
    return _browser(ctx).snapshot()


@router.post("/browser/open", dependencies=[Depends(check_host)])
def browser_open(ctx: AppContext = Depends(authorized)) -> dict:
    return _browser(ctx).open_browser().to_dict()


@router.post("/browser/close", dependencies=[Depends(check_host)])
def browser_close(ctx: AppContext = Depends(authorized)) -> dict:
    return _browser(ctx).close_browser().to_dict()


@router.post("/browser/stop", dependencies=[Depends(check_host)])
def browser_stop(ctx: AppContext = Depends(authorized)) -> dict:
    _browser(ctx).stop_action()
    return {"stopping": True}


@router.get("/browser/log", dependencies=[Depends(check_host)])
def browser_log(limit: int = 50, ctx: AppContext = Depends(authorized)) -> dict:
    return {"events": _browser(ctx).log.recent(max(1, min(limit, 200)))}


# ---- autonomous tasks (Phase 21) -----------------------------------------------------------------------------------------------------
# Task summaries only: goal, status, progress, the current action's description, risk, question, result text. Never the blackboard, page text or chain of thought.


def _autonomy(ctx: AppContext):
    manager = getattr(ctx, "autonomy", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="the autonomous agent is not enabled")
    return manager


@router.get("/tasks", dependencies=[Depends(check_host)])
def tasks(ctx: AppContext = Depends(authorized)) -> dict:
    return _autonomy(ctx).snapshot()


@router.post("/tasks/cancel", dependencies=[Depends(check_host)])
def tasks_cancel(ctx: AppContext = Depends(authorized)) -> dict:
    return {"message": _autonomy(ctx).cancel()}


@router.post("/tasks/pause", dependencies=[Depends(check_host)])
def tasks_pause(ctx: AppContext = Depends(authorized)) -> dict:
    return {"message": _autonomy(ctx).pause()}


@router.post("/tasks/resume", dependencies=[Depends(check_host)])
def tasks_resume(ctx: AppContext = Depends(authorized)) -> dict:
    return {"message": _autonomy(ctx).resume()}


# ---- personal operator workflows (Phase 22) ------------------------------------------------------------------------------------------------
# Workflow summaries only: goal, status, progress n/m, sources, the current step's description, risk, question, result text, redacted audit lines. Never step outputs, email/page
# text, tokens or chain of thought. Every route needs the per-run token and a loopback Host; POST /workflows goes through the same planner, validator and permission checks as speech.


class WorkflowRequest(BaseModel):
    goal: str

    def clean(self) -> str:
        return self.goal.strip()[:300]


class WorkflowConfirm(BaseModel):
    approve: bool


def _operator(ctx: AppContext):
    op = getattr(ctx, "operator", None)
    if op is None:
        raise HTTPException(status_code=503, detail="the personal operator is not enabled")
    return op


def _workflow(ctx: AppContext, workflow_id: str):
    wf = _operator(ctx).get(workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="no such workflow")
    return wf


@router.post("/workflows", dependencies=[Depends(check_host)])
def workflows_start(body: WorkflowRequest, ctx: AppContext = Depends(authorized)) -> dict:
    goal = body.clean()
    if not goal:
        raise HTTPException(status_code=422, detail="say what the workflow should do")
    reply = _operator(ctx).start(goal, "api")
    if reply is None:
        return {"started": False, "message": "That isn't a workflow I can run.", "workflow": None}
    return {"started": reply.workflow is not None, "message": reply.text, "workflow": reply.workflow.summary() if reply.workflow is not None else None}


@router.get("/workflows", dependencies=[Depends(check_host)])
def workflows(ctx: AppContext = Depends(authorized)) -> dict:
    return _operator(ctx).snapshot()


@router.get("/workflows/{workflow_id}", dependencies=[Depends(check_host)])
def workflow_detail(workflow_id: str, ctx: AppContext = Depends(authorized)) -> dict:
    return _workflow(ctx, workflow_id).summary()


@router.get("/workflows/{workflow_id}/status", dependencies=[Depends(check_host)])
def workflow_status(workflow_id: str, ctx: AppContext = Depends(authorized)) -> dict:
    s = _workflow(ctx, workflow_id).summary()
    return {"workflow_id": s["workflow_id"], "status": s["status"], "progress": s["progress"], "current_step": s["current_step"], "question": s["question"]}


@router.post("/workflows/{workflow_id}/cancel", dependencies=[Depends(check_host)])
def workflow_cancel(workflow_id: str, ctx: AppContext = Depends(authorized)) -> dict:
    _workflow(ctx, workflow_id)
    return {"message": _operator(ctx).cancel(workflow_id)}


@router.post("/workflows/{workflow_id}/pause", dependencies=[Depends(check_host)])
def workflow_pause(workflow_id: str, ctx: AppContext = Depends(authorized)) -> dict:
    wf = _workflow(ctx, workflow_id)
    op = _operator(ctx)
    runner = op._runners.get(wf.workflow_id)  # noqa: SLF001
    return {"message": "Paused. Say resume to carry on." if runner is not None and runner.pause() else "That workflow can't be paused right now."}


@router.post("/workflows/{workflow_id}/resume", dependencies=[Depends(check_host)])
def workflow_resume(workflow_id: str, ctx: AppContext = Depends(authorized)) -> dict:
    _workflow(ctx, workflow_id)
    return {"message": _operator(ctx).resume(workflow_id)}


@router.post("/workflows/{workflow_id}/confirm", dependencies=[Depends(check_host)])
def workflow_confirm(workflow_id: str, body: WorkflowConfirm, ctx: AppContext = Depends(authorized)) -> dict:
    _workflow(ctx, workflow_id)
    return {"message": _operator(ctx).confirm(workflow_id, body.approve).text}
