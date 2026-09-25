"""Production builder: wires the Personal Operator from application settings and the running services. Building it starts nothing (no thread, no network)."""

from pathlib import Path
from typing import Any

from autonomy.toolrouter import ToolRouter
from backend.core.logging import get_logger
from workflows.operator import OperatorIntentRouter, PersonalOperator
from workflows.planner import Limits, WorkflowPlanner
from workflows.runner import OperatorConfig
from workflows.store import WorkflowStore
from workflows.tools import OperatorContext, OperatorRouter

logger = get_logger(__name__)


def config_from_settings(settings) -> OperatorConfig:
    return OperatorConfig(
        enabled=settings.WORKFLOWS_ENABLED, max_concurrent=settings.WORKFLOW_MAX_CONCURRENT, max_duration_s=settings.WORKFLOW_MAX_DURATION_SECONDS, max_steps=settings.WORKFLOW_MAX_STEPS,
        max_tool_calls=settings.WORKFLOW_MAX_TOOL_CALLS, max_retries=settings.WORKFLOW_MAX_RETRIES, max_systems=settings.WORKFLOW_MAX_SYSTEMS,
        confirmation_timeout_s=settings.WORKFLOW_CONFIRMATION_TIMEOUT_SECONDS, history_size=settings.WORKFLOW_HISTORY_SIZE, proactive=settings.WORKFLOW_PROACTIVE_ENABLED)


def build_operator(settings, state_dir: Path, *, hub, browser, tasks, reminders, memory, zone, clock, confirmations, center=None, announce: Any = None,
                   llm_available: Any = None, bus: Any = None) -> tuple[PersonalOperator, OperatorIntentRouter] | None:
    """The operator and its conversational router, or None when disabled or when there is nothing for it to coordinate."""
    if not settings.WORKFLOWS_ENABLED or (hub is None and tasks is None and reminders is None):
        return None
    cfg = config_from_settings(settings)
    store = WorkflowStore(state_dir / "workflows")
    (state_dir / "workflows").mkdir(parents=True, exist_ok=True)
    browser_router = ToolRouter(browser.tools if browser is not None else None, hub)
    ctx = OperatorContext(hub=hub, tasks=tasks, reminders=reminders, memory=memory if settings.JARVIS_MEMORY_ENABLED else None, tool_router=browser_router, store=store, zone=zone, clock=clock,
                          notify=announce, center=center)
    router = OperatorRouter(ctx)
    planner = WorkflowPlanner(router, Limits(cfg.max_steps, cfg.max_systems, cfg.max_tool_calls, cfg.max_duration_s))
    engine = browser.engine if browser is not None else None
    op = PersonalOperator(planner, router, store, cfg, confirmations=confirmations, announce=announce, browser_stop=engine.stop_current_action if engine is not None else None,
                          llm_available=llm_available, inline_wait_s=settings.WORKFLOW_INLINE_WAIT_SECONDS)
    if bus is not None and settings.WORKFLOW_PROACTIVE_ENABLED:
        from backend.core.events import SystemEvent

        bus.subscribe(SystemEvent.DEADLINE_DETECTED, op.on_deadline_event)
    try:
        op.recover()
    except Exception as exc:  # noqa: BLE001 - a damaged checkpoint file must not stop JARVIS from starting
        logger.error("Workflow recovery failed (%s)", type(exc).__name__)
    return op, OperatorIntentRouter(op)
