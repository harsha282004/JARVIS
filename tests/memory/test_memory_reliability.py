"""Memory reliability: persistence across restart, duplicate control, updates keep history, provenance/timestamps/confidence preserved."""

from datetime import datetime, timezone

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agent.memory.models import Confidence, MemoryBasis, MemoryCandidate, MemoryRejected, MemorySource, MemoryStatus, MemoryType, StoreOutcome
from agent.memory.repository import MemoryRepository
from agent.memory.service import MemoryService
from backend.models.base import Base
import backend.models.memory  # noqa: F401


def service(factory, now=None):
    clock = (lambda: now) if now else (lambda: datetime.now(timezone.utc))
    return MemoryService(MemoryRepository(factory), clock=clock)


def cand(content, slot=None, conf=Confidence.HIGH):
    return MemoryCandidate(type=MemoryType.FACT, content=content, source=MemorySource.EXPLICIT_USER_STATEMENT, basis=MemoryBasis.EXPLICIT, confidence=conf, slot=slot)


def make_factory():
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, expire_on_commit=False)


def test_long_term_memory_survives_a_restart_with_source_time_and_confidence():
    factory = make_factory()
    t = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    result = service(factory, t).store(cand("User studies Computer Science."))
    assert result.outcome is StoreOutcome.STORED
    restarted = service(factory)  # a new service instance over the same database
    m = restarted.search(None)[0]
    assert m.content == "User studies Computer Science." and m.created_at == t
    assert m.source is MemorySource.EXPLICIT_USER_STATEMENT and m.basis is MemoryBasis.EXPLICIT and m.confidence is Confidence.HIGH
    assert [x.memory_id for x in restarted.retrieve("computer science")] == [m.memory_id]  # retrieval works after restart


def test_duplicates_are_controlled():
    factory = make_factory()
    s = service(factory)
    s.store(cand("User prefers Python."))
    second = s.store(cand("User prefers Python."))
    assert second.outcome is not StoreOutcome.STORED and len(s.search(None)) == 1


def test_stale_memory_is_updated_and_old_value_kept_for_provenance():
    factory = make_factory()
    s = service(factory)
    old = s.store(cand("User's favorite language is Java.", slot="favorite language")).memory
    new = s.store(cand("User's favorite language is Python.", slot="favorite language")).memory
    assert [m.content for m in s.search(None)] == ["User's favorite language is Python."]
    superseded = s.search(None, statuses=[MemoryStatus.SUPERSEDED])
    assert [m.memory_id for m in superseded] == [old.memory_id] and superseded[0].superseded_by == new.memory_id


def test_secrets_are_never_stored():
    factory = make_factory()
    s = service(factory)
    with pytest.raises(MemoryRejected):
        s.store(cand("My key is sk-abcdefghijklmnopqrstuvwxyz123456"))
    assert s.search(None) == []
