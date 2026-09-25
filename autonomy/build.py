"""Production builder: wires the autonomous agent from application settings and the running services. Building it starts nothing."""

from pathlib import Path
from typing import Any

from autonomy.manager import AutonomyManager, AutonomyRouter
from autonomy.observe import Observer, Verifier
from autonomy.planner import PlanContext, Planner
from autonomy.runner import AutonomyConfig
from autonomy.toolrouter import ToolRouter


def config_from_settings(settings) -> AutonomyConfig:
    return AutonomyConfig(
        enabled=settings.AUTONOMY_ENABLED, max_duration_s=settings.AUTONOMY_MAX_DURATION_SECONDS, max_steps=settings.AUTONOMY_MAX_STEPS,
        max_tool_calls=settings.AUTONOMY_MAX_TOOL_CALLS, max_retries=settings.AUTONOMY_MAX_RETRIES, max_replans=settings.AUTONOMY_MAX_REPLANS,
        loop_threshold=settings.AUTONOMY_LOOP_THRESHOLD, observation_timeout_s=settings.AUTONOMY_OBSERVATION_TIMEOUT_SECONDS,
        confirmation_timeout_s=settings.AUTONOMY_CONFIRMATION_TIMEOUT_SECONDS, browser_task_timeout_s=settings.AUTONOMY_BROWSER_TASK_TIMEOUT_SECONDS,
        max_consecutive_failures=settings.AUTONOMY_MAX_CONSECUTIVE_FAILURES, progress_notifications=settings.AUTONOMY_VOICE_PROGRESS,
        inline_wait_s=settings.AUTONOMY_INLINE_WAIT_SECONDS, history_size=settings.AUTONOMY_HISTORY_SIZE)


def build_autonomy(settings, state_dir: Path, *, browser, hub, confirmations, announce: Any = None) -> tuple[AutonomyManager, AutonomyRouter] | None:
    """The manager and its conversational router, or None when disabled or when neither the browser nor an integration is available to act through."""
    if not settings.AUTONOMY_ENABLED or (browser is None and hub is None):
        return None
    tools = browser.tools if browser is not None else None
    router = ToolRouter(tools, hub)
    cfg = config_from_settings(settings)
    engine = browser.engine if browser is not None else None

    def context() -> PlanContext:
        if engine is None:
            return PlanContext(browser_available=False)
        st = engine.status()
        url = st.get("url", "")
        yt = tools.youtube
        host = url.split("/")[2] if url.startswith("http") and len(url.split("/")) > 2 else ""
        return PlanContext(host=host, url=url, browser_open=st.get("state") == "ready" and st.get("tab_count", 0) > 0, yt_query=yt.last_query, yt_results=bool(yt.last_results))

    manager = AutonomyManager(Planner(router, max_steps=cfg.max_steps), router, Observer(engine), cfg, confirmations=confirmations, announce=announce, context_provider=context,
                              browser_stop=engine.stop_current_action if engine is not None else None, history_path=state_dir / "autonomy_history.json", verifier=Verifier())
    return manager, AutonomyRouter(manager)
