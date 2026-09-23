"""MemoryService + MemoryRepository against an isolated database.

Runs on in-memory SQLite by default (never the developer's PostgreSQL) and on
a disposable PostgreSQL too when JARVIS_TEST_DATABASE_URL is set.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agent.memory.base import MemoryInterface
from agent.memory.models import (
    Confidence,
    MemoryBasis,
    MemoryCandidate,
    MemoryNotFound,
    MemoryRejected,
    MemorySource,
    MemoryStatus,
    MemoryStorageError,
    MemoryType,
    StoreOutcome,
)
from agent.memory.policy import MemoryPolicy
from agent.memory.repository import MemoryRepository
from agent.memory.service import MemoryService
from backend.models.base import Base


class Clock:
    def __init__(self):
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(session_factory, clock):
    return MemoryService(MemoryRepository(session_factory), clock=clock)


def cand(content="User prefers Java.", type_=MemoryType.PREFERENCE, slot=None, retracts=None, **kw):
    base = dict(source=MemorySource.EXPLICIT_USER_STATEMENT, basis=MemoryBasis.EXPLICIT, confidence=Confidence.HIGH)
    return MemoryCandidate(type=type_, content=content, slot=slot, retracts=retracts, **{**base, **kw})


def inferred(content="User seems to like Java.", **kw):
    return cand(content, source=MemorySource.CONVERSATION, basis=MemoryBasis.INFERRED, confidence=Confidence.LOW, **kw)


# ---- interface, create, read ----

def test_service_implements_the_memory_interface(service):
    assert isinstance(service, MemoryInterface)


def test_store_and_retrieve_round_trip_with_provenance(service, clock):
    result = service.store(cand(metadata={"note": "x"}))
    assert result.outcome is StoreOutcome.STORED
    stored = result.memory
    assert stored.status is MemoryStatus.ACTIVE and stored.created_at == clock.now
    assert stored.last_accessed_at is None

    [found] = service.search("java")
    assert found.memory_id == stored.memory_id
    assert (found.source, found.basis, found.confidence, found.type) == (
        MemorySource.EXPLICIT_USER_STATEMENT, MemoryBasis.EXPLICIT, Confidence.HIGH, MemoryType.PREFERENCE)
    assert found.metadata == {"note": "x"} and found.created_at.tzinfo is not None


def test_memory_persists_across_service_instances_and_new_connections(tmp_path, clock):
    url = f"sqlite:///{tmp_path / 'memory.db'}"
    first = create_engine(url)
    Base.metadata.create_all(first)
    MemoryService(MemoryRepository(sessionmaker(bind=first)), clock=clock).store(cand("User prefers Java."))
    first.dispose()  # "application restart"

    second = create_engine(url)
    restarted = MemoryService(MemoryRepository(sessionmaker(bind=second)), clock=clock)
    assert [m.content for m in restarted.retrieve("What language do I prefer?")] == ["User prefers Java."]
    second.dispose()


# ---- search / filters ----

def test_search_filters_by_type_status_and_limit(service):
    service.store(cand("User prefers Java."))
    service.store(cand("User studies Java at university.", MemoryType.FACT))
    service.store(cand("User wants to learn Java deeply.", MemoryType.GOAL))
    assert len(service.search("java")) == 3
    assert [m.type for m in service.search("java", types=[MemoryType.GOAL])] == [MemoryType.GOAL]
    assert len(service.search("java", limit=2)) == 2
    assert len(service.search()) == 3  # no text: everything active
    assert service.search("java", statuses=[MemoryStatus.DELETED]) == []
    assert service.search("the of") == []  # only stopwords -> nothing


def test_active_and_inactive_filtering(service):
    m = service.store(cand()).memory
    service.delete(m.memory_id)
    assert service.search("java") == []
    assert [x.status for x in service.search("java", statuses=[MemoryStatus.DELETED])] == [MemoryStatus.DELETED]
    assert [x.memory_id for x in service.search(statuses=[MemoryStatus.ACTIVE, MemoryStatus.DELETED])] == [m.memory_id]


# ---- relevance + access tracking ----

def test_retrieval_returns_only_relevant_memories(service):
    service.store(cand("User prefers Java."))
    service.store(cand("User is a final-year student.", MemoryType.PROFILE))
    service.store(cand("User's cat is named Tom.", MemoryType.FACT))
    assert [m.content for m in service.retrieve("What programming language do I usually prefer?")] == ["User prefers Java."]
    assert service.retrieve("What is the weather like?") == []


def test_retrieval_ranks_by_keyword_hits_and_respects_limit(service):
    service.store(cand("User likes Java."))
    service.store(cand("User's favorite programming language is Java.", slot="programming language"))
    top = service.retrieve("favorite programming language java", limit=1)
    assert [m.content for m in top] == ["User's favorite programming language is Java."]


def test_last_accessed_only_updates_for_retrieved_memories(service, clock):
    java = service.store(cand("User prefers Java.")).memory
    cat = service.store(cand("User's cat is named Tom.", MemoryType.FACT)).memory
    clock.advance(60)

    service.search("java")  # an admin-style search must not count as use
    assert service.search("java")[0].last_accessed_at is None

    service.retrieve("what java do I prefer")
    fresh = {m.memory_id: m for m in service.search()}
    assert fresh[java.memory_id].last_accessed_at == clock.now
    assert fresh[cat.memory_id].last_accessed_at is None  # unrelated memory untouched


# ---- update / delete / purge / correct ----

def test_update_changes_content_and_timestamp(service, clock):
    m = service.store(cand()).memory
    clock.advance(10)
    updated = service.update(m.memory_id, "User prefers Kotlin.")
    assert updated.content == "User prefers Kotlin." and updated.updated_at == clock.now
    assert [x.content for x in service.search("kotlin")] == ["User prefers Kotlin."]
    assert service.search("java") == []


def test_update_rejects_secrets_and_missing_ids(service):
    m = service.store(cand()).memory
    with pytest.raises(MemoryRejected):
        service.update(m.memory_id, "My password is hunter2")
    assert service.search("java")[0].content == "User prefers Java."
    with pytest.raises(MemoryNotFound):
        service.update("0" * 32, "x")
    with pytest.raises(MemoryNotFound):
        service.update("not-an-id; DROP TABLE personal_memories", "x")


def test_soft_delete_keeps_the_row_but_hides_it(service):
    m = service.store(cand()).memory
    assert service.delete(m.memory_id) is True
    assert service.retrieve("java") == []
    assert service.delete(m.memory_id) is False  # already deleted
    assert service.delete("f" * 32) is False
    assert service._repo.get(m.memory_id).status is MemoryStatus.DELETED


def test_purge_physically_removes_the_row(service):
    m = service.store(cand()).memory
    assert service.purge(m.memory_id) is True
    assert service._repo.get(m.memory_id) is None
    assert service.purge(m.memory_id) is False


def test_explicit_correction_supersedes_and_links_old_memory(service):
    old = service.store(cand("User prefers Java.")).memory
    new = service.correct(old.memory_id, "User prefers Python.")
    assert new.source is MemorySource.USER_CORRECTION and new.basis is MemoryBasis.EXPLICIT
    kept = service._repo.get(old.memory_id)
    assert kept.status is MemoryStatus.SUPERSEDED and kept.superseded_by == new.memory_id
    assert [m.content for m in service.search("python")] == ["User prefers Python."]
    assert service.search("java") == []
    with pytest.raises(MemoryNotFound):
        service.correct(old.memory_id, "again")  # only active memories can be corrected


# ---- deduplication / conflicts ----

def test_duplicates_are_not_stored_twice(service):
    first = service.store(cand("User prefers Python."))
    again = service.store(cand("user prefers python"))
    assert again.outcome is StoreOutcome.DUPLICATE and again.memory.memory_id == first.memory.memory_id
    assert len(service.search()) == 1


def test_duplicate_explicit_statement_upgrades_an_inferred_memory(service):
    service.store(inferred("User prefers Python."))
    result = service.store(cand("User prefers Python."))
    assert result.reason == "upgraded_to_explicit"
    assert result.memory.basis is MemoryBasis.EXPLICIT and result.memory.confidence is Confidence.HIGH
    assert len(service.search()) == 1


def test_same_slot_newer_explicit_statement_supersedes_older(service):
    a = service.store(cand("User's favorite programming language is Java.", slot="programming language")).memory
    result = service.store(cand("User's favorite programming language is Python.", slot="programming language"))
    assert result.outcome is StoreOutcome.SUPERSEDED_OLD and result.superseded_ids == [a.memory_id]
    assert [m.content for m in service.search("programming language")] == ["User's favorite programming language is Python."]
    assert service._repo.get(a.memory_id).status is MemoryStatus.SUPERSEDED


def test_retraction_supersedes_the_named_old_value(service):
    service.store(cand("User's favorite programming language is Java.", slot="programming language"))
    service.store(cand("User loves JavaScript."))
    result = service.store(cand("User's favorite language is Python.", slot="language", retracts="Java",
                                source=MemorySource.USER_CORRECTION))
    assert result.outcome is StoreOutcome.SUPERSEDED_OLD and len(result.superseded_ids) == 1
    active = {m.content for m in service.search()}
    assert active == {"User loves JavaScript.", "User's favorite language is Python."}  # "java" != "javascript"


def test_inferred_memory_never_overrides_an_explicit_one(service):
    service.store(cand("User's favorite programming language is Java.", slot="programming language"))
    result = service.store(inferred("User's favorite programming language is Go.", slot="programming language"))
    assert result.outcome is StoreOutcome.KEPT_EXISTING and result.memory is None
    assert [m.content for m in service.search("programming")] == ["User's favorite programming language is Java."]


def test_different_slots_do_not_conflict(service):
    service.store(cand("User's favorite editor is Vim.", slot="editor"))
    result = service.store(cand("User's favorite language is Go.", slot="language"))
    assert result.outcome is StoreOutcome.STORED and len(service.search()) == 2


# ---- safety at the service boundary ----

def test_secrets_are_refused_and_never_logged(service, caplog):
    with caplog.at_level("DEBUG"):
        with pytest.raises(MemoryRejected):
            service.store(cand("User's password is hunter2."))
    assert service.search() == []
    assert "hunter2" not in caplog.text


def test_process_utterance_auto_saves_explicit_statements(service):
    [result] = service.process_utterance("My favorite programming language is Java.")
    assert result.outcome is StoreOutcome.STORED
    assert service.retrieve("favorite programming language")[0].content == "User's favorite programming language is Java."


def test_process_utterance_full_correction_flow(service):
    service.process_utterance("My favorite programming language is Java.")
    [result] = service.process_utterance("Actually, my favorite programming language is Python, not Java.")
    assert result.outcome is StoreOutcome.SUPERSEDED_OLD
    assert [m.content for m in service.search("programming")] == ["User's favorite programming language is Python."]
    assert service.process_utterance("My favorite programming language is Python.")[0].outcome is StoreOutcome.DUPLICATE


def test_process_utterance_rejects_secrets_and_holds_sensitive_for_confirmation(service, caplog):
    with caplog.at_level("DEBUG"):
        rejected = service.process_utterance("Remember that my password is hunter2.")
        held = service.process_utterance("Remember that I was diagnosed with diabetes.")
    assert rejected[0].outcome is StoreOutcome.REJECTED
    assert held[0].outcome is StoreOutcome.PENDING_CONFIRMATION
    assert service.search() == []  # nothing saved silently
    assert "hunter2" not in caplog.text and "diabetes" not in caplog.text

    [pending] = service.pending
    stored = service.confirm(pending.pending_id)
    assert stored.outcome is StoreOutcome.STORED and service.pending == []
    with pytest.raises(MemoryNotFound):
        service.confirm(pending.pending_id)


def test_pending_can_be_discarded(service):
    service.process_utterance("Remember that I was diagnosed with diabetes.")
    [pending] = service.pending
    assert service.discard(pending.pending_id) is True and service.discard(pending.pending_id) is False
    assert service.search() == []


def test_inferred_candidates_are_never_auto_saved(service):
    service._extractor.extract = lambda text: [inferred("User seems to like Java.")]
    [result] = service.process_utterance("I asked about Java a lot")
    assert result.outcome is StoreOutcome.PENDING_CONFIRMATION and service.search() == []


def test_auto_save_disabled_holds_everything(session_factory, clock):
    service = MemoryService(MemoryRepository(session_factory), policy=MemoryPolicy(auto_save=False), clock=clock)
    assert service.process_utterance("I love Java")[0].outcome is StoreOutcome.PENDING_CONFIRMATION
    assert service.search() == []


# ---- database failure ----

class BrokenSessions:
    def __call__(self):
        from sqlalchemy.exc import OperationalError

        raise OperationalError("SELECT secret-user-content", {}, Exception("db down"))


def test_database_failure_raises_a_content_free_storage_error(clock):
    service = MemoryService(MemoryRepository(BrokenSessions()), clock=clock)
    with pytest.raises(MemoryStorageError) as exc:
        service.store(cand("User prefers Java."))
    assert "Java" not in str(exc.value) and "secret-user-content" not in str(exc.value)
    with pytest.raises(MemoryStorageError):
        service.retrieve("java")


def test_database_failure_backs_off_then_recovers(session_factory, clock):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) <= 1:
            return BrokenSessions()()
        return session_factory()

    service = MemoryService(MemoryRepository(flaky), clock=clock, backoff_seconds=30)
    with pytest.raises(MemoryStorageError):
        service.search()
    made = len(calls)
    with pytest.raises(MemoryStorageError):
        service.search()  # within the back-off window: the database is not touched again
    assert len(calls) == made
    clock.advance(31)
    assert service.search() == []  # recovered
