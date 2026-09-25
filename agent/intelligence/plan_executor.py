"""PlanExecutor: turns a proposed plan (or one suggested event) into calendar events, only after confirmation, and only claims success it verified.

    PLAN (automatic, changes nothing)  ->  "add it to my calendar"  ->  confirmation naming every block  ->  create  ->  VERIFY  ->  report

* Confirmation: the prompt lists exactly what will be created; the ConfirmationEngine runs it only after a clear yes to that prompt.
* Idempotent: each event id is derived from the plan and the block, so saying yes twice (or a retry after a network error) cannot create
  duplicates; a block that already exists is verified and counted, not created again.
* Verification: after each create, the event is read back from the calendar and its title and time are compared. Only a read-back that
  matches counts as done. A failure or an unverifiable outcome is reported as exactly that, never as success.
* Nobody is invited, and nothing existing is modified or deleted.
"""

import base64
import hashlib
from datetime import datetime, timedelta

from agent.intelligence.confirmation import ActionReport, ConfirmationEngine
from agent.intelligence.findings import Offer
from agent.intelligence.phrasing import clock, day_word, join_and, quoted
from agent.intelligence.planner import PlanBlock, PlanProposal
from backend.core.logging import get_logger
from backend.core.security.approval import ApprovalClass
from integrations.calendar.models import CalendarError, CalendarEventDraft, CalendarNotFound, CalendarOutcomeUnknown

logger = get_logger(__name__)

TOOL = "calendar.create_event"
_TOLERANCE = timedelta(minutes=1)


def deterministic_event_id(*parts: str) -> str:
    """A Google-valid event id (lowercase base32hex, 5-1024 chars) that is the same every time for the same inputs."""
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).digest()
    return base64.b32hexencode(digest).decode("ascii").lower().rstrip("=")


class PlanExecutor:
    def __init__(self, calendar, zone, confirmations: ConfirmationEngine, now, gate=None):
        self._gate = gate  # () -> (allowed, reason): the Integration Hub's CREATE_EVENT permission check; None = no hub
        self._calendar = calendar
        self._zone = zone
        self._confirm = confirmations
        self._now = now

    # ---- availability --------------------------------------------------------------------------------------------------

    def _writable_calendar(self):
        """(calendar, None) or (None, spoken reason)."""
        if self._calendar is None:
            return None, "Google Calendar isn't set up, so I can't add anything to it."
        if self._gate is not None:
            allowed, reason = self._gate()
            if not allowed:
                return None, reason + (" Say 'allow JARVIS to create calendar events' if you want me to." if "permission" in reason else "")
        try:
            if hasattr(self._calendar, "is_configured") and not self._calendar.is_configured():
                return None, "Google Calendar isn't set up, so I can't add anything to it."
            found = self._calendar.find_calendar(None, writable=True)
        except CalendarError as exc:
            return None, exc.user_message
        except Exception:  # noqa: BLE001
            return None, "I can't reach Google Calendar right now, so nothing was added."
        if not found:
            return None, "I couldn't find a calendar I'm allowed to add events to."
        return found[0], None

    # ---- proposing (asks for confirmation; changes nothing) -------------------------------------------------------

    def propose_plan(self, plan: PlanProposal, session_id: str) -> str:
        if not plan.blocks:
            return "That plan has no work blocks to add."
        cal, problem = self._writable_calendar()
        if cal is None:
            return problem or "I can't add to your calendar right now."
        blocks = [{"id": deterministic_event_id(plan.plan_id, str(i), b.title), "title": f"Work block: {b.title}", "start": b.start.isoformat(), "end": b.end.isoformat()}
                  for i, b in enumerate(plan.blocks)]
        listing = "; ".join(f"{clock(b.start, self._zone)} to {clock(b.end, self._zone)} {quoted(b.title)}" for b in plan.blocks)
        prompt = (f"I'll add {len(blocks)} work block{'s' if len(blocks) != 1 else ''} to your calendar '{cal.summary or 'primary'}' for "
                  f"{day_word(plan.day, self._now(), self._zone)}: {listing}. Nobody will be invited. Shall I go ahead?")
        return self._confirm.request(
            action_class=ApprovalClass.MODIFY_CALENDAR, tool=TOOL, summary=prompt, params={"calendar_id": cal.calendar_id, "blocks": blocks},
            run=lambda: self._create(cal.calendar_id, blocks, f"plan {plan.plan_id[:8]}"), session_id=session_id, source=f"plan:{plan.plan_id}",
        )

    def propose_offer(self, offer: Offer, session_id: str) -> str:
        """One suggested event (for example "an email mentions an interview that isn't on your calendar")."""
        if offer.start is None:
            return "I don't have a time for that, so I can't add it."
        cal, problem = self._writable_calendar()
        if cal is None:
            return problem or "I can't add to your calendar right now."
        end = offer.end or offer.start + timedelta(hours=1)
        block = {"id": deterministic_event_id("offer", offer.title, offer.start.isoformat()), "title": offer.title, "start": offer.start.isoformat(),
                 "end": end.isoformat(), "all_day": offer.all_day}
        return self._confirm.request(
            action_class=ApprovalClass.MODIFY_CALENDAR, tool=TOOL, summary=offer.prompt, params={"calendar_id": cal.calendar_id, "blocks": [block]},
            run=lambda: self._create(cal.calendar_id, [block], "suggested event", single=(offer.title, offer.start)), session_id=session_id, source="offer",
        )

    # ---- running (only ever reached through ConfirmationEngine.respond) --------------------------------------------

    def _create(self, calendar_id: str, blocks: list[dict], label: str, single: tuple[str, datetime] | None = None) -> ActionReport:
        verified, failed, unverified, existed = [], [], [], 0
        for b in blocks:
            start, end = datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])
            try:
                try:
                    current = self._calendar.get_event(calendar_id, b["id"])
                    existed += 1  # a retry of the same confirmed plan: nothing to create again
                except CalendarNotFound:
                    draft = CalendarEventDraft(event_id=b["id"], summary=b["title"], start=start, end=end, all_day=bool(b.get("all_day")), timezone=self._zone.key,
                                               description=f"Added by JARVIS after you confirmed it ({label}).")
                    self._calendar.create_event(calendar_id, draft)
                    current = self._calendar.get_event(calendar_id, b["id"])  # read it back: creating is not the same as it being there
                if current.summary == b["title"] and abs(current.start - start) <= _TOLERANCE and abs(current.end - end) <= _TOLERANCE:
                    verified.append(b["title"])
                else:
                    unverified.append(b["title"])
            except CalendarOutcomeUnknown:
                unverified.append(b["title"])  # the request may or may not have reached Google: never claim either way
            except CalendarError as exc:
                failed.append((b["title"], exc.user_message))
            except Exception as exc:  # noqa: BLE001
                logger.error("Calendar create failed (%s)", type(exc).__name__)
                failed.append((b["title"], "the calendar integration failed"))
        n = len(blocks)
        if len(verified) == n and single is not None:
            from agent.intelligence.phrasing import when_phrase

            return ActionReport(True, f"Done. I added {quoted(single[0])} {when_phrase(single[1], self._now(), self._zone)} to your calendar and confirmed it's there.", verified=True)
        if len(verified) == n:
            names = "block" if n == 1 else "blocks"
            extra = f" ({existed} already existed)" if existed else ""
            return ActionReport(True, f"Done. I added {n} {names} to your calendar and confirmed {'it' if n == 1 else 'each one'} there{extra}.", verified=True)
        parts = []
        if verified:
            parts.append(f"I added and confirmed {len(verified)} of {n}")
        if failed:
            reasons = join_and(sorted({r for _, r in failed}))
            parts.append(f"I couldn't create {join_and([quoted(t) for t, _ in failed])} because {reasons[:1].lower() + reasons[1:] if reasons else 'of an error'}")
        if unverified:
            parts.append(f"I couldn't confirm whether {join_and([quoted(t) for t in unverified])} was created, so please check your calendar before asking again")
        return ActionReport(False, ". ".join(p.rstrip('.') for p in parts) + ".", verified=False, partial=bool(verified))
