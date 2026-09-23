"""MemoryService: the memory rules. The only writer of personal memory.

    MemoryInterface -> MemoryService (rules/policy) -> MemoryRepository -> PostgreSQL

Every change to stored memory goes through this class, from code, never from
model output: extraction reads the user's words with fixed rules, and there is
no tool, SQL or prompt path that reaches the repository. Failures raise
MemoryStorageError; nothing is ever reported as saved when it was not.

Conflict rules (deterministic, newer explicit statement wins):
- identical active memory (same type + normalized text): DUPLICATE, nothing added
- same type + same slot with different text, or a retracted old value ("..., not Java"):
  the newer EXPLICIT memory supersedes the old one (kept, status SUPERSEDED, linked)
- an INFERRED memory never overrides an EXPLICIT one (KEPT_EXISTING)
"""

import re
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any, TypeVar

from agent.memory.base import MemoryInterface
from agent.memory.extractor import RuleBasedExtractor
from agent.memory.models import (
    Confidence,
    Memory,
    MemoryBasis,
    MemoryCandidate,
    MemoryNotFound,
    MemoryRejected,
    MemorySource,
    MemoryStatus,
    MemoryStorageError,
    MemoryType,
    PendingMemory,
    StoreOutcome,
    StoreResult,
    utcnow,
)
from agent.memory.normalize import keywords, normalize_content, normalize_slot
from agent.memory.policy import MemoryDecision, MemoryPolicy
from agent.memory.repository import MemoryRepository
from agent.memory.safety import screen
from backend.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

MAX_PENDING = 20
CANDIDATE_FETCH_LIMIT = 50
DEFAULT_MAX_RETRIEVAL = 5
BACKOFF_SECONDS = 30.0
_ID_LENGTH = 32


class MemoryService(MemoryInterface):
    def __init__(
        self,
        repository: MemoryRepository,
        policy: MemoryPolicy | None = None,
        extractor: RuleBasedExtractor | None = None,
        max_retrieval: int = DEFAULT_MAX_RETRIEVAL,
        clock: Callable[[], datetime] = utcnow,
        backoff_seconds: float = BACKOFF_SECONDS,
    ):
        self._repo = repository
        self._policy = policy or MemoryPolicy()
        self._extractor = extractor or RuleBasedExtractor()
        self._max_retrieval = max_retrieval
        self._clock = clock
        self._backoff = timedelta(seconds=backoff_seconds)
        self._retry_after: datetime | None = None
        self._pending: dict[str, PendingMemory] = {}

    # ---- storage guard -----------------------------------------------

    def _db(self, work: Callable[[], T]) -> T:
        """Run repository work. After a database failure, skip the database briefly
        so an unavailable database does not slow every conversation turn."""
        now = self._clock()
        if self._retry_after is not None and now < self._retry_after:
            raise MemoryStorageError("Memory database temporarily unavailable")
        try:
            result = work()
        except MemoryStorageError:
            self._retry_after = now + self._backoff
            raise
        self._retry_after = None
        return result

    # ---- MemoryInterface ------------------------------------------------

    def store(self, candidate: MemoryCandidate) -> StoreResult:
        """Save a candidate: refuse secrets, deduplicate, resolve conflicts."""
        if screen(f"{candidate.content} {candidate.slot or ''}").secret:
            logger.warning("Memory rejected by safety screening (type=%s)", candidate.type.value)
            raise MemoryRejected("Content looks like a secret and will not be stored")
        return self._db(lambda: self._store(candidate))

    def retrieve(self, query: str, limit: int | None = None) -> list[Memory]:
        """Active memories relevant to `query`, best first; marks them as accessed."""
        terms = keywords(query)
        if not terms:
            return []
        limit = limit or self._max_retrieval
        rows = self._db(
            lambda: self._repo.search(terms, statuses=[MemoryStatus.ACTIVE], limit=CANDIDATE_FETCH_LIMIT)
        )
        ranked = sorted(
            rows,
            key=lambda m: (-sum(t in normalize_content(m.content) for t in terms), -int(m.confidence), m.memory_id),
        )[:limit]
        if ranked:
            self._db(lambda: self._repo.touch([m.memory_id for m in ranked], self._clock()))
            logger.info("Memory retrieved (count=%d)", len(ranked))
        return ranked

    def update(self, memory_id: str, content: str) -> Memory:
        """Edit a memory's text in place (use `correct` to keep the old version)."""
        memory = self._require(memory_id)
        try:
            edited = memory.model_copy(update={"content": content, "updated_at": self._clock()})
            edited = Memory.model_validate(edited.model_dump())
        except ValueError as exc:
            raise MemoryRejected(f"Invalid memory content ({type(exc).__name__})") from None
        if screen(edited.content).secret:
            raise MemoryRejected("Content looks like a secret and will not be stored")
        self._db(lambda: self._save_or_raise(edited))
        logger.info("Memory updated (id=%s, type=%s)", memory_id, edited.type.value)
        return edited

    def delete(self, memory_id: str) -> bool:
        """Soft-delete: the row stays for provenance but is never retrieved."""
        memory = self._db(lambda: self._repo.get(self._valid_id(memory_id)))
        if memory is None or memory.status is MemoryStatus.DELETED:
            return False
        deleted = memory.model_copy(update={"status": MemoryStatus.DELETED, "updated_at": self._clock()})
        self._db(lambda: self._save_or_raise(deleted))
        logger.info("Memory deleted (id=%s, type=%s)", memory_id, memory.type.value)
        return True

    def search(
        self,
        text: str | None = None,
        types: Sequence[MemoryType] | None = None,
        statuses: Sequence[MemoryStatus] | None = None,
        limit: int = 20,
    ) -> list[Memory]:
        terms = keywords(text) if text else None
        if text and not terms:
            return []
        statuses = statuses if statuses is not None else [MemoryStatus.ACTIVE]
        return self._db(lambda: self._repo.search(terms, types, statuses, limit))

    # ---- corrections, purge ------------------------------------------------

    def correct(self, memory_id: str, new_content: str) -> Memory:
        """Replace a memory with the user's corrected version: the old one is kept as
        SUPERSEDED (linked to the new one) and the new one is an explicit correction."""
        old = self._require(memory_id)
        try:
            candidate = MemoryCandidate(
                type=old.type,
                content=new_content,
                source=MemorySource.USER_CORRECTION,
                basis=MemoryBasis.EXPLICIT,
                confidence=Confidence.HIGH,
                slot=old.slot,
            )
        except ValueError as exc:
            raise MemoryRejected(f"Invalid memory content ({type(exc).__name__})") from None
        if screen(candidate.content).secret:
            raise MemoryRejected("Content looks like a secret and will not be stored")

        def work() -> Memory:
            new = self._new_memory(candidate)
            self._repo.add(new)
            self._supersede(old, new.memory_id)
            return new

        new = self._db(work)
        logger.info("Memory corrected (old=%s, new=%s)", memory_id, new.memory_id)
        return new

    def purge(self, memory_id: str) -> bool:
        """Physically erase a memory row (privacy erase)."""
        removed = self._db(lambda: self._repo.purge(self._valid_id(memory_id)))
        if removed:
            logger.info("Memory purged (id=%s)", memory_id)
        return removed

    # ---- extraction from a conversation turn -----------------------------------

    def process_utterance(self, user_text: str) -> list[StoreResult]:
        """Extract candidates from what the user said and apply the confirmation policy.

        AUTO_SAVE candidates are stored; CONFIRM candidates wait in `pending`
        (in memory only); REJECT candidates are dropped without logging content.
        """
        results: list[StoreResult] = []
        for candidate in self._extractor.extract(user_text):
            verdict = self._policy.evaluate(candidate)
            logger.info(
                "Memory candidate detected (type=%s, decision=%s, reason=%s)",
                candidate.type.value, verdict.decision.value, verdict.reason,
            )
            if verdict.decision is MemoryDecision.REJECT:
                results.append(StoreResult(outcome=StoreOutcome.REJECTED, reason=verdict.reason))
            elif verdict.decision is MemoryDecision.CONFIRM:
                pending = self._hold(candidate, verdict.reason)
                results.append(StoreResult(outcome=StoreOutcome.PENDING_CONFIRMATION, reason=pending.pending_id))
            else:
                results.append(self.store(candidate))
        return results

    @property
    def pending(self) -> list[PendingMemory]:
        return list(self._pending.values())

    def confirm(self, pending_id: str) -> StoreResult:
        """The user confirmed a pending candidate: store it (secrets still refused)."""
        pending = self._pending.get(pending_id)
        if pending is None:
            raise MemoryNotFound("No such pending memory")
        result = self.store(pending.candidate)
        del self._pending[pending_id]
        return result

    def discard(self, pending_id: str) -> bool:
        return self._pending.pop(pending_id, None) is not None

    # ---- internals -----------------------------------------------------------

    def _hold(self, candidate: MemoryCandidate, reason: str) -> PendingMemory:
        pending = PendingMemory(candidate=candidate, reason=reason, created_at=self._clock())
        while len(self._pending) >= MAX_PENDING:
            del self._pending[next(iter(self._pending))]
        self._pending[pending.pending_id] = pending
        return pending

    def _new_memory(self, candidate: MemoryCandidate) -> Memory:
        now = self._clock()
        data: dict[str, Any] = candidate.model_dump()
        return Memory(**data, created_at=now, updated_at=now, retracts=candidate.retracts)

    def _valid_id(self, memory_id: str) -> str:
        if not isinstance(memory_id, str) or len(memory_id) != _ID_LENGTH or not memory_id.isalnum():
            raise MemoryNotFound("Invalid memory id")
        return memory_id

    def _require(self, memory_id: str) -> Memory:
        memory = self._db(lambda: self._repo.get(self._valid_id(memory_id)))
        if memory is None or memory.status is not MemoryStatus.ACTIVE:
            raise MemoryNotFound("No active memory with that id")
        return memory

    def _save_or_raise(self, memory: Memory) -> None:
        if not self._repo.save(memory):
            raise MemoryNotFound("Memory no longer exists")

    def _supersede(self, old: Memory, new_id: str) -> None:
        self._repo.save(
            old.model_copy(update={"status": MemoryStatus.SUPERSEDED, "superseded_by": new_id, "updated_at": self._clock()})
        )

    def _store(self, candidate: MemoryCandidate) -> StoreResult:
        norm = normalize_content(candidate.content)
        same = self._repo.active_with_norm(candidate.type, norm)
        if same:
            existing = same[0]
            if existing.basis is MemoryBasis.INFERRED and candidate.basis is MemoryBasis.EXPLICIT:
                upgraded = existing.model_copy(
                    update={
                        "basis": candidate.basis, "confidence": candidate.confidence,
                        "source": candidate.source, "updated_at": self._clock(),
                    }
                )
                self._repo.save(upgraded)
                return StoreResult(outcome=StoreOutcome.DUPLICATE, memory=upgraded, reason="upgraded_to_explicit")
            return StoreResult(outcome=StoreOutcome.DUPLICATE, memory=existing, reason="identical_active_memory")

        conflicts = self._conflicts(candidate, norm)
        if candidate.basis is MemoryBasis.INFERRED and any(c.basis is MemoryBasis.EXPLICIT for c in conflicts):
            return StoreResult(outcome=StoreOutcome.KEPT_EXISTING, reason="explicit_memory_outranks_inference")

        memory = self._new_memory(candidate)
        self._repo.add(memory)
        for old in conflicts:
            self._supersede(old, memory.memory_id)
        logger.info("Memory stored (id=%s, type=%s, superseded=%d)", memory.memory_id, memory.type.value, len(conflicts))
        if conflicts:
            return StoreResult(
                outcome=StoreOutcome.SUPERSEDED_OLD, memory=memory,
                superseded_ids=[c.memory_id for c in conflicts], reason="newer_explicit_statement",
            )
        return StoreResult(outcome=StoreOutcome.STORED, memory=memory)

    def _conflicts(self, candidate: MemoryCandidate, norm: str) -> list[Memory]:
        found: dict[str, Memory] = {}
        if candidate.slot:
            for m in self._repo.active_with_slot(candidate.type, normalize_slot(candidate.slot)):
                found[m.memory_id] = m
        if candidate.retracts:
            fragment = normalize_content(candidate.retracts)
            if fragment:
                whole_word = re.compile(r"\b" + re.escape(fragment) + r"\b")
                for m in self._repo.active_containing(candidate.type, fragment):
                    if whole_word.search(normalize_content(m.content)):  # "java" must not match "javascript"
                        found[m.memory_id] = m
        return list(found.values())
