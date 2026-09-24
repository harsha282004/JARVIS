"""Briefing tools: `briefing_generate` (morning briefing, schedule, tasks, deadlines, priorities, focus, what's next, what was missed,
preparation) and `briefing_explain` ("where did you get that?", "why are you mentioning this?").

Read-only, LOW risk, no approval, ONE_TIME scope: they only read the existing services through BriefingService and change nothing
(no task, event, calendar entry, email or message is created, modified, sent or deleted). Same two-part shape as the other tools:
resolve() validates and returns Ready; run() executes only through Tool.execute after the PermissionManager authorized exactly
these parameters. Briefing text can quote email subjects and calendar titles (untrusted), so the conversation history keeps only
a placeholder and the model never reads it.
"""

from dataclasses import dataclass

from agent.briefing.intents import BriefingActionName, BriefingExplainArgs, BriefingGenerateArgs
from agent.briefing.models import BriefingError, BriefingWindow, Detail, View
from agent.briefing.service import BriefingService
from agent.tasks.tools import Clarify, Ready
from agent.tools.base import Tool
from backend.core.security import PermissionScope, RiskLevel

PLACEHOLDER = "[A briefing was read to the user. Its text is deliberately not kept in the conversation history.]"


@dataclass(frozen=True)
class BriefingToolContext:
    service: BriefingService


class BriefingGenerateTool(Tool):
    name = BriefingActionName.GENERATE.value
    description = (
        "Give the user a spoken briefing built from their existing tasks, reminders, deadlines, calendar, important email and messages "
        "(read-only; nothing is changed or invented). view: overview (morning briefing, 'what do I have today'), schedule, tasks, "
        "deadlines, priorities, focus ('what should I focus on'), next ('what's next'), missed ('what did I miss'), prepare."
    )
    input_schema = {
        "view": "overview | schedule | tasks | deadlines | priorities | focus | next | missed | prepare (default overview)",
        "window": "today | tomorrow | this_week | next_7_days | yesterday | last_24_hours (default today; yesterday for missed)",
        "detail": "quick | normal | detailed (default normal)",
        "day_part": "morning | afternoon | evening, optional (schedule only, e.g. 'my afternoon')",
    }
    requires_permission = False
    risk = RiskLevel.LOW
    allowed_scopes = (PermissionScope.ONE_TIME,)
    history_placeholder = PLACEHOLDER

    def __init__(self, context: BriefingToolContext):
        self._ctx = context

    def resolve(self, args: BriefingGenerateArgs) -> Ready | Clarify:
        window = args.window or (BriefingWindow.YESTERDAY if args.view is View.MISSED else BriefingWindow.TODAY)
        if window.is_past and args.view is not View.MISSED:
            return Clarify("I can only look ahead for that. Ask me what you missed yesterday if you want to look back.")
        if args.view is View.MISSED and window in (BriefingWindow.TOMORROW, BriefingWindow.NEXT_7_DAYS):
            return Clarify("That's in the future. I can tell you what you missed yesterday, in the last 24 hours, or so far today.")
        return Ready({"view": args.view.value, "window": window.value, "detail": args.detail.value, "day_part": args.day_part or ""})

    def run(self, *, view: str, window: str, detail: str, day_part: str, origin_session: str | None = None) -> str:
        try:
            briefing = self._ctx.service.brief(View(view), BriefingWindow(window), Detail(detail), day_part=day_part or None)
        except BriefingError:
            return "I can't build that kind of briefing."
        return briefing.spoken


class BriefingExplainTool(Tool):
    name = BriefingActionName.EXPLAIN.value
    description = (
        "Say where an item in the briefing JARVIS just gave came from (its source) and why it was mentioned (the factual reason). "
        "Read-only. Use for 'where did you get that?' and 'why are you mentioning this / why is this a priority?'."
    )
    input_schema = {"query": "string: words from the item the user means, optional", "aspect": "source | reason | both (default both)"}
    requires_permission = False
    risk = RiskLevel.LOW
    allowed_scopes = (PermissionScope.ONE_TIME,)
    history_placeholder = PLACEHOLDER

    def __init__(self, context: BriefingToolContext):
        self._ctx = context

    def resolve(self, args: BriefingExplainArgs) -> Ready:
        return Ready({"query": args.query or "", "aspect": args.aspect})

    def run(self, *, query: str, aspect: str, origin_session: str | None = None) -> str:
        return self._ctx.service.explain(query, aspect)


def build_briefing_tools(context: BriefingToolContext) -> list[Tool]:
    return [BriefingGenerateTool(context), BriefingExplainTool(context)]
