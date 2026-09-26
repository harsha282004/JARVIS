"""IntelligenceRouter: recognizes the questions and requests the intelligence layer answers, and answers them without a language model.

A request is matched by explicit patterns (planning, focus, importance on a day, conflicts, preparation, project status, timeline,
explanations, sources, dependencies, preferences, briefings, references such as "when is it due?", and the answers to a pending
confirmation). Anything that does not match returns None and continues through the normal path (agent brain and tools).

Why deterministic: these answers are built from the user's own data and must not vary or be embellished. It also means they keep
working when the language model or the internet is unavailable, and they cost no LLM call.

Nothing here changes a source system. "Add it to my calendar" only asks for confirmation naming the exact events; the change happens
only after the user's clear "yes" (ConfirmationEngine), and its result is verified before it is reported.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from agent.intelligence.explain import NOTHING_TO_EXPLAIN
from agent.intelligence.models import Answer, Entity, EntityKind, Provenance, SourceKind, fact
from agent.intelligence.phrasing import join_and, quoted, status_word, when_phrase
from agent.intelligence.prefs_intents import handle_preference
from agent.intelligence.service import IntelligenceService
from backend.core.logging import get_logger
from backend.core.security.trust import TrustLevel

logger = get_logger(__name__)

HISTORY_PLACEHOLDER = "(I answered from the user's own tasks, calendar, email and notes.)"
FAILURE_TEXT = "Sorry, I couldn't work that out right now, so I don't want to guess."

_DAYS = "today|tonight|tomorrow|day after tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_LEAD = re.compile(r"^(?:(?:hey|ok|okay|hi)[ ,]+)?(?:jarvis[ ,]+)?(?:please[ ,]+)?(?:can you|could you|would you|will you|i want you to|i'?d like you to|i need you to)?[ ,]*", re.I)
_GENERIC_PROJECT = re.compile(r"^(?:my|the|this|that|current|active)?\s*(?:project|one)?$", re.I)


@dataclass(frozen=True)
class IntelligenceReply:
    text: str
    history_text: str = HISTORY_PLACEHOLDER


def normalize(text: str) -> str:
    t = " ".join(text.replace("’", "'").replace("`", "'").split()).strip()
    t = re.sub(r"[.!?]+$", "", t).strip()
    prev = None
    while prev != t:
        prev = t
        t = _LEAD.sub("", t, count=1).strip(" ,")
    return t.lower()


def parse_day(word: str | None, now: datetime, zone, *, default: date | None = None) -> date | None:
    today = now.astimezone(zone).date()
    if not word:
        return default
    w = word.strip().lower()
    if w in ("today", "tonight"):
        return today
    if w == "tomorrow":
        return today + timedelta(days=1)
    if w == "day after tomorrow":
        return today + timedelta(days=2)
    if w == "yesterday":
        return today - timedelta(days=1)
    names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if w in names:
        ahead = (names.index(w) - today.weekday()) % 7
        return today + timedelta(days=ahead or 7)
    return default


_FOCUS = re.compile(r"^(?:what|which) (?:should|do|can|could|shall) (?:i|we) (?:focus on|work on|prioriti[sz]e|tackle|concentrate on|be (?:doing|working on|focusing on)|do(?: first)?)(?: (?:today|now|first|next|this morning|this afternoon|this evening))?$|^what(?:'s| is) my (?:top |main |biggest )?priority(?: (?:today|now))?$")
_IMPORTANT = re.compile(rf"^(?:what(?:'s| is| are)|anything|is there anything|show me|tell me|do i have anything) ?(?:the )?(?:most )?(?:important|urgent|critical|coming up|happening|on(?: my plate)?|due)(?: things?)?(?: for| on| in| by)? (?P<day>{_DAYS})$")
_PLAN_A = re.compile(rf"^(?:plan|organi[sz]e|schedule|map out|structure|lay out) (?:for |out )?(?:my |the |our )?(?:day|entire day|whole day|working day|workday|{_DAYS})(?: (?:for )?(?P<day>{_DAYS}))?$")
_PLAN_B = re.compile(rf"^(?:create|make|build|generate|give|draft|prepare|put together|come up with|set up|show)(?: me)? (?:a |an |the |my )?(?:proposed |daily |new )?(?:plan|schedule)(?: for (?:my |the )?(?:day )?(?P<day>{{days}}))?$".replace("{days}", _DAYS))
_ADD = re.compile(r"^(?:yes,? |yeah,? |ok,? |okay,? )?(?:please )?(?:add|put|save|schedule|block|book|insert|create|place) (?:it|this|that|the plan|these|those|them|the blocks?|my plan|the work blocks?|the proposed plan|all of it)(?: all)?(?: (?:in|into|to|on|onto) (?:my |the )?(?:google )?calendar)?$|^(?:add|put) (?:the |my )?plan (?:to|on|in) (?:my )?calendar$")
_WHY = re.compile(r"^why (?:did you|are you|do you|would you|have you|were you|was that|is that|is this|that|this)\b.*$|^why (?:did|are) you (?:tell|mention|schedule|suggest|say|bring)")
_SOURCE = re.compile(r"^where did you (?:get|find|read|see|learn|hear|pull) (?:that|this|it|those|the .+)(?: from)?$|^what(?:'s| is| was| were) (?:your|the) sources?(?: for (?:that|this|it))?$|^how do you know (?:that|this|it)$|^(?:show me )?(?:your |the )?sources?$|^which (?:email|calendar|source|document)\b.*(?:that|this|it)$")
_CONFLICTS = re.compile(r"^(?:do i have|are there|is there|any|show me|check for|check my calendar for|tell me about|find) ?(?:any )?(?:conflicts?|overlaps?|clashes|double[- ]?bookings?|conflicting (?:events|meetings|information|commitments|deadlines))(?: (?:today|tomorrow|this week|coming up))?$|^what(?:'s| is) conflicting$")
_PREPARE = re.compile(r"^(?:prepare|get) me (?:for|ready for) (?P<what>.+)$|^help me (?:prepare|prep|get ready) for (?P<what2>.+)$|^what do i need to (?:prepare|prep) for (?P<what3>.+)$")
_PROJECT = re.compile(r"^what(?:'s| is| are) (?:still )?(?:pending|left|remaining|outstanding|open|next)(?: (?:for|on|in|with|about))? (?P<n>.+?)$|^(?:show me|tell me about|give me (?:an? )?(?:update|status) (?:on|of)|what(?:'s| is) the status of|status of|how(?:'s| is)) (?P<n2>.+?)(?: going)?$")
_TIMELINE = re.compile(rf"^what (?:happened|went on|has happened)(?: (?:with|on|in|for|to|around|regarding))?(?: (?P<topic>.+?))?(?: (?P<day>yesterday|today|last night|on (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|earlier today))?$")
_MORNING = re.compile(r"^(?:good morning|morning|morning briefing|my morning briefing|brief me|(?:give me|read me|show me) (?:a |my |the )?(?:morning )?(?:briefing|brief)|start my day|what(?:'s| is) my morning briefing)$")
_EVENING = re.compile(r"^(?:evening review|(?:give me |do )?(?:an? |my )?evening (?:review|summary)|review my day|end of (?:the )?day (?:review|summary)|wrap up my day)$")
_MORE = re.compile(r"^(?:tell me more|more details?|give me (?:more )?details?|go on|elaborate|more|expand on that)$")
_BLOCKED = re.compile(r"^what(?:'s| is| are) (?:blocked|blocking (?P<t>.+)|waiting)$|^what tasks are blocked$|^is (?P<t2>.+) blocked$|^which tasks? (?:are|is) blocked$")
_DEPEND = re.compile(r"^(?:the )?(?:task )?(?P<a>.+?) (?:depends on|is blocked by|is waiting for|waits for|can'?t start until|cannot start until) (?:the )?(?:task )?(?P<b>.+?)(?: (?:is )?(?:done|finished|complete(?:d)?|first))?$")
_WHEN = re.compile(r"^(?:when|what time|what day) (?:is|was|are) (?P<p>it|that|this|these|those|the [a-z ]+|that [a-z ]+|this [a-z ]+|my [a-z ]+)(?: (?:due|scheduled|happening|on|at|again))?$|^when(?:'s| is) (?P<p2>it|that|this)(?: due)?$")
_ABOUT = re.compile(r"^(?:what about|tell me about|how about|what(?:'s| is) (?:the )?(?:status|state) of|status of|remind me about) (?P<p>it|that|this|(?:the|that|this|my) [a-z ]+)$")
_ACK = re.compile(r"^(?:acknowledge|dismiss|clear|mark as read) (?:all )?(?:the |my |those |these )?notifications?$")


_TIME_Q = re.compile(r"^(?:(?:hey )?jarvis )?(?:what(?:'s| is)? (?:the )?(?:current )?time(?: is it)?(?: (?:now|right now|please))?|what time is it(?: (?:now|right now))?|(?:tell me|give me) the time|do you (?:know|have) the time)$")
_DATE_Q = re.compile(r"^(?:(?:hey )?jarvis )?(?:what(?:'s| is)? (?:the )?(?:date|day)(?: (?:is it|it is))?(?: today)?|what(?:'s| is) today(?:'s date)?|what day is (?:it|today)|what(?:'s| is) today's date)$")


def _clock_answer(t: str, now, zone) -> str | None:
    """Deterministic answer for "what time is it?" / "what's the date?" from the same clock and zone the rest of the assistant uses."""
    if _TIME_Q.match(t):
        return f"It's {now.astimezone(zone).strftime('%I:%M %p').lstrip('0')}."
    if _DATE_Q.match(t):
        local = now.astimezone(zone)
        return f"Today is {local.strftime('%A, %B')} {local.day}, {local.year}."
    return None


class IntelligenceRouter:
    def __init__(self, service: IntelligenceService, hub_router=None, browser_router=None, autonomy_router=None, operator_router=None):
        self._svc = service
        self._operator_router = operator_router  # Phase 22: personal workflows across the user's systems (workflows.operator.OperatorIntentRouter)
        self._autonomy_router = autonomy_router  # Phase 21: multi-step goals and task controls (autonomy.manager.AutonomyRouter)
        self._browser_router = browser_router  # Phase 20: spoken browser control (browser.voice.BrowserRouter)
        self._hub_router = hub_router  # Phase 18: spoken requests about the integrations (agent.intelligence.hub_router.HubRouter)

    def cancel_task(self) -> bool:
        """Voice "Stop"/"Cancel": stop a workflow and/or an autonomous task, if one is running. False when there is none."""
        stopped = bool(self._operator_router is not None and self._operator_router.cancel_task())
        return bool(self._autonomy_router is not None and self._autonomy_router.cancel_task()) or stopped

    def task_active(self) -> bool:
        return bool(self._autonomy_router is not None and self._autonomy_router.task_active()) or bool(self._operator_router is not None and self._operator_router.task_active())

    @property
    def service(self) -> IntelligenceService:
        return self._svc

    def handle(self, text: str, session_id: str, *, level: TrustLevel = TrustLevel.USER) -> IntelligenceReply | None:
        """A reply if this is ours, otherwise None. Never raises: a failure gives an honest 'couldn't work that out'."""
        svc = self._svc
        try:
            if self._operator_router is not None and level is TrustLevel.USER:  # "Stop"/"Cancel the workflow" ends a workflow waiting for a yes; it is not just a "no"
                stopped = self._operator_router.intercept_cancel(text, session_id)
                if stopped is not None:
                    return IntelligenceReply(stopped, "(I stopped a workflow.)")
            answered = svc.confirmations.respond(text, session_id, level=level)
            if answered is not None:
                svc.after_change()  # a confirmed action ran: the cached view of the sources is stale
                return IntelligenceReply(answered)
            if level is not TrustLevel.USER:
                return None  # only what the user said can ask for anything
            t = normalize(text)
            if not t:
                return None
            reply = self._route(t, text, session_id)
            return reply
        except Exception as exc:  # noqa: BLE001 - the conversation must survive; we say we could not, we do not guess
            logger.error("Intelligence request failed (%s)", type(exc).__name__)
            return IntelligenceReply(FAILURE_TEXT)

    # ---- routing ---------------------------------------------------------------------------------------------------------

    def _route(self, t: str, original: str, session_id: str) -> IntelligenceReply | None:
        svc, now = self._svc, self._svc.now()
        zone = svc.zone

        clock = _clock_answer(t, now, zone)
        if clock is not None:  # the current time/date is read from the clock, never guessed by a language model
            return IntelligenceReply(clock)

        pref = handle_preference(original, svc.prefs)
        if pref is not None:
            return IntelligenceReply(pref.text, "(I updated or read the user's notification preferences.)")

        if self._operator_router is not None:  # Phase 22: a personal workflow (email -> task -> reminder, briefing, ...) or a control phrase for one
            handled = self._operator_router.handle(t, original, session_id)
            if handled is not None:
                return IntelligenceReply(handled, "(I carried out or discussed a personal workflow.)")
        if self._autonomy_router is not None:  # first: an answer to a waiting task, a control phrase, or a multi-step goal
            handled = self._autonomy_router.handle(t, original, session_id)
            if handled is not None:
                return IntelligenceReply(handled, "(I carried out or discussed an autonomous task.)")
        if self._hub_router is not None:
            handled = self._hub_router.handle(t, original, session_id)
            if handled is not None:
                return IntelligenceReply(handled)
        if self._browser_router is not None:  # after the hub: an API answer is preferred to a browser click
            handled = self._browser_router.handle(t, original, session_id)
            if handled is not None:
                return IntelligenceReply(handled, "(I controlled the user's browser.)")

        if _ADD.match(t):
            if svc.last_plan is None and not svc.last_offers:
                return None  # nothing to add: the normal calendar tools may know what "it" is
            return IntelligenceReply(svc.add_plan_to_calendar(session_id) if svc.last_plan is not None else svc.offer_action(svc.last_offers[0], session_id))

        if _MORE.match(t):
            for a in (svc.last_answer, svc.last_briefing):
                if a is not None and a.detail:
                    return IntelligenceReply(a.detail)
            return None

        if _MORNING.match(t):
            return self._answer(svc.morning(), session_id, offers=True)
        if _EVENING.match(t):
            return self._answer(svc.evening(), session_id)
        if _FOCUS.match(t):
            return self._answer(svc.focus(), session_id, offers=True)

        m = _IMPORTANT.match(t)
        if m:
            day = parse_day(m.group("day"), now, zone)
            return self._answer(svc.important(day), session_id, offers=True) if day else None

        day = self._plan_day(t, now, zone)
        if day is not None:
            answer = svc.plan(day)
            return IntelligenceReply(answer.text)

        if _WHY.match(t):
            kinds = ("plan_block",) if re.search(r"schedul|plan|put", t) else None
            return IntelligenceReply(svc.explanations.why(svc.explanations.find(t, kinds) or svc.explanations.find(t)))
        if _SOURCE.match(t):
            item = svc.explanations.find(t)
            return IntelligenceReply(svc.explanations.sources(item) if item else NOTHING_TO_EXPLAIN)

        if _CONFLICTS.match(t):
            return self._answer(svc.conflicts(), session_id)

        m = _PREPARE.match(t)
        if m:
            what = m.group("what") or m.group("what2") or m.group("what3")
            day, query = self._split_day(what, now, zone)
            return self._answer(svc.prepare(query, day), session_id)

        m = _BLOCKED.match(t)
        if m:
            return IntelligenceReply(svc.blocked_answer(m.group("t") or m.group("t2")).text)
        m = _DEPEND.match(t)
        if m:
            reply = svc.add_dependency(m.group("a"), m.group("b"))
            return IntelligenceReply(reply, "(I recorded a task dependency.)") if reply else None

        m = _TIMELINE.match(t)
        if m:
            day = parse_day((m.group("day") or "today").replace("on ", "").replace("last night", "yesterday").replace("earlier today", "today"), now, zone) or now.date()
            topic = (m.group("topic") or "").strip()
            topic = re.sub(r"^(?:with |on |in |for |to |regarding )?(?:my |the )?", "", topic).strip()
            if topic and _GENERIC_PROJECT.match(topic) or topic in ("project", "my project", "the project"):
                topic = svc.active_project() or ""
                if not topic:
                    return IntelligenceReply("Which project do you mean?")
            topic = re.sub(r"\s+project$", "", topic).strip()
            return IntelligenceReply(svc.activity(day, topic or None).text)

        m = _PROJECT.match(t)
        if m:
            name = (m.group("n") or m.group("n2") or "").strip()
            name = re.sub(r"^(?:my|the|our)\s+", "", name)
            has_word = bool(re.search(r"\bproject\b", name))
            name = re.sub(r"\s*project$", "", name).strip()
            if _GENERIC_PROJECT.match(name) or not name:
                if not has_word and not name:
                    return None
                name = svc.active_project() or ""
                if not name:
                    return IntelligenceReply("Which project do you mean?") if has_word else None
            bundle = svc.bundle()
            if svc.workflows.find_project(bundle.result, name) is None and not has_word:
                return None  # "what's pending for today" is a task question, not a project question
            return self._answer(svc.project(name), session_id)

        m = _WHEN.match(t) or _ABOUT.match(t)
        if m:
            return self._reference(t, m.group("p") if "p" in m.groupdict() and m.group("p") else m.groupdict().get("p2") or "it", when=bool(_WHEN.match(t)))

        if _ACK.match(t):
            center = svc.notifications
            if center is None:
                return None
            n = center.acknowledge_all()
            return IntelligenceReply(f"Okay, I marked {n} notification{'s' if n != 1 else ''} as read." if n else "You have no unread notifications.", "(I acknowledged notifications.)")
        return None

    # ---- helpers ----------------------------------------------------------------------------------------------------------

    def _plan_day(self, t: str, now: datetime, zone) -> date | None:
        m = _PLAN_A.match(t) or _PLAN_B.match(t)
        if not m:
            return None
        word = m.groupdict().get("day")
        if not word:
            for d in ("day after tomorrow", "tomorrow", "today"):
                if d in t:
                    word = d
                    break
            if not word:
                for name in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
                    if name in t:
                        word = name
                        break
        if word:
            return parse_day(word, now, zone)
        # No day named: plan today, or tomorrow once today's working day is over.
        end = datetime.combine(now.date(), self._svc.planner._we, tzinfo=zone)  # noqa: SLF001
        return now.date() + timedelta(days=1) if now >= end else now.date()

    @staticmethod
    def _split_day(text: str, now: datetime, zone) -> tuple[date | None, str]:
        m = re.search(rf"\b({_DAYS})(?:'s)?\b", text)
        day = parse_day(m.group(1), now, zone) if m else None
        query = re.sub(rf"\b(?:for |on |this )?(?:{_DAYS})(?:'s)?\b", " ", text)
        query = re.sub(r"\b(?:the|my|our|a|an)\b", " ", query)
        return day, " ".join(query.split())

    def _answer(self, answer: Answer, session_id: str, *, offers: bool = False) -> IntelligenceReply:
        text = answer.text
        if offers:
            text = self._attach_offer(answer, session_id)
        return IntelligenceReply(text)

    def _attach_offer(self, answer: Answer, session_id: str) -> str:
        """If the answer already asks "Would you like me to add X to your calendar?", register exactly that as the pending confirmation."""
        svc = self._svc
        b = svc.cached()
        if b is None:
            return answer.text
        for f in b.findings:
            if f.offer is not None and f.offer.prompt and f.offer.prompt in answer.text:
                svc.last_offers = [f.offer]
                prompt = svc.offer_action(f.offer, session_id)
                return answer.text if prompt == f.offer.prompt else f"{answer.text} {prompt}"
        return answer.text

    def _reference(self, t: str, phrase: str, *, when: bool) -> IntelligenceReply | None:
        svc = self._svc
        b = svc.bundle()
        graph = b.result.graph
        resolution = svc.context.resolve(phrase, graph)
        if resolution.entity is None:
            if resolution.candidates:
                return IntelligenceReply(resolution.question or "Which one do you mean?")
            return None  # nothing in the conversation to refer to: not something the context layer can answer
        e = resolution.entity
        svc.context.note([e.entity_id])
        now = b.snapshot.now
        if when:
            if e.when is None:
                text = f"I don't have a date for {quoted(e.name)}."
            else:
                label = "due" if e.kind in (EntityKind.TASK, EntityKind.DEADLINE, EntityKind.ASSIGNMENT) else "scheduled for"
                text = f"{quoted(e.name)} is {label} {when_phrase(e.when, now, svc.zone, e.all_day)}."
                conflict = next((f for f in b.findings if e.entity_id in f.entity_ids and f.kind.value in ("memory_conflict", "source_conflict")), None)
                if conflict is not None:
                    text += " " + conflict.spoken(with_suggestion=False)
            svc.explanations.record("answer", e.name, (fact(text, *e.provenance[:2]),))
            return IntelligenceReply(text)
        return IntelligenceReply(self._describe(e, b, now))

    def _describe(self, e: Entity, b, now: datetime) -> str:
        svc = self._svc
        parts = []
        head = f"{quoted(e.name)}"
        if e.when is not None:
            parts.append(f"{head} is {when_phrase(e.when, now, svc.zone, e.all_day)}.")
        else:
            parts.append(f"{head} has no date.")
        if e.status and e.kind in (EntityKind.TASK, EntityKind.ASSIGNMENT):
            parts.append(f"It's {status_word(e.status)}.")
        related = svc.workflows._related(b.result, e.entity_id)  # noqa: SLF001
        tasks = [o for _, o in related if o.kind is EntityKind.TASK and o.status not in ("completed", "cancelled", "candidate") and o.entity_id != e.entity_id]
        if tasks:
            parts.append("Related open tasks: " + join_and([quoted(t.name) for t in tasks[:4]]) + ".")
        sources = join_and([p.describe() for p in e.provenance[:3]])
        parts.append(f"That came from {sources}.")
        svc.explanations.record("answer", e.name, (fact(parts[0], *e.provenance[:3]),))
        return " ".join(parts)
