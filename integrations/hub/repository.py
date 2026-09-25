"""HubRepository: idempotent storage of normalized items (see backend/models/hub.py).

`upsert` is the whole synchronization contract: the same source item retrieved twice is stored once; if its content changed the row is updated, if
not it is left alone (only `retrieved_at` moves). Sessions are per call, so it is safe from the sync thread and the conversation thread.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from backend.models.hub import HubItemRow
from integrations.hub.models import ItemKind, NormalizedItem, utcnow

SessionFactory = Callable[[], Session]


@dataclass
class UpsertCounts:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    items: list = None  # (item, outcome) for every item that was created or updated

    def __post_init__(self) -> None:
        self.items = []

    def add(self, outcome: str, item: "NormalizedItem | None" = None) -> None:
        setattr(self, outcome, getattr(self, outcome) + 1)
        if item is not None and outcome != "unchanged":
            self.items.append((item, outcome))

    @property
    def changed(self) -> int:
        return self.created + self.updated


def _utc(value: datetime | None) -> datetime | None:
    """Every stored time is UTC. (SQLite drops the offset and keeps the wall clock, so an aware non-UTC value must be converted BEFORE it is stored.)"""
    return value.astimezone(timezone.utc) if value is not None and value.tzinfo is not None else value


def _to_item(row: HubItemRow) -> NormalizedItem:
    def aware(d):
        return d if d is None or d.tzinfo else d.replace(tzinfo=utcnow().tzinfo)

    return NormalizedItem(
        kind=ItemKind(row.kind), source=row.source, source_id=row.source_id, timestamp=aware(row.source_timestamp), title=row.title, summary=row.summary or "",
        metadata=dict(row.extra or {}), confidence=row.confidence, external_id=row.external_id, retrieved_at=aware(row.retrieved_at),
    )


class HubRepository:
    def __init__(self, session_factory: SessionFactory, clock: Callable[[], datetime] = utcnow):
        self._sf = session_factory
        self._clock = clock

    def upsert(self, item: NormalizedItem) -> str:
        """'created' | 'updated' | 'unchanged'."""
        now = self._clock()
        with self._sf() as db:
            row = db.execute(select(HubItemRow).where(HubItemRow.source == item.source, HubItemRow.kind == item.kind.value,
                                                     HubItemRow.source_id == item.source_id[:200])).scalar_one_or_none()
            if row is None:
                db.add(HubItemRow(
                    id=item.item_id, source=item.source, kind=item.kind.value, source_id=item.source_id[:200], external_id=item.external_id, title=item.title[:300],
                    summary=item.summary, extra=item.metadata, confidence=item.confidence, content_hash=item.content_hash, source_timestamp=_utc(item.timestamp),
                    first_seen_at=now, retrieved_at=now,
                ))
                db.commit()
                return "created"
            revived = row.deleted_at is not None
            if row.content_hash == item.content_hash and not revived:
                row.retrieved_at = now
                db.commit()
                return "unchanged"
            row.title, row.summary, row.extra, row.confidence = item.title[:300], item.summary, item.metadata, item.confidence
            row.content_hash, row.source_timestamp, row.external_id, row.retrieved_at, row.deleted_at = item.content_hash, _utc(item.timestamp), item.external_id, now, None
            db.commit()
            return "updated"

    def upsert_many(self, items: Iterable[NormalizedItem]) -> UpsertCounts:
        counts = UpsertCounts()
        for item in items:
            counts.add(self.upsert(item), item)
        return counts

    def get(self, source: str, kind: ItemKind, source_id: str) -> NormalizedItem | None:
        with self._sf() as db:
            row = db.execute(select(HubItemRow).where(HubItemRow.source == source, HubItemRow.kind == kind.value, HubItemRow.source_id == source_id[:200],
                                                     HubItemRow.deleted_at.is_(None))).scalar_one_or_none()
            return _to_item(row) if row else None

    def search(self, text: str = "", *, source: str | None = None, kind: ItemKind | None = None, since: datetime | None = None, limit: int = 20) -> list[NormalizedItem]:
        """Items whose title or summary contain every word of `text`, newest first. Parameterized; the text is never interpolated."""
        stmt = select(HubItemRow).where(HubItemRow.deleted_at.is_(None))
        if source:
            stmt = stmt.where(HubItemRow.source == source)
        if kind:
            stmt = stmt.where(HubItemRow.kind == kind.value)
        if since:
            stmt = stmt.where(HubItemRow.source_timestamp >= _utc(since))
        for word in re.findall(r"[a-z0-9]{2,}", text.lower())[:8]:
            like = f"%{word}%"
            stmt = stmt.where(or_(func.lower(HubItemRow.title).like(like), func.lower(HubItemRow.summary).like(like)))
        stmt = stmt.order_by(HubItemRow.source_timestamp.desc().nullslast(), HubItemRow.id).limit(max(1, min(limit, 100)))
        with self._sf() as db:
            return [_to_item(r) for r in db.execute(stmt).scalars()]

    def mark_deleted(self, source: str, kind: ItemKind, source_ids: Iterable[str]) -> int:
        ids = [s[:200] for s in source_ids]
        if not ids:
            return 0
        with self._sf() as db:
            result = db.execute(update(HubItemRow).where(HubItemRow.source == source, HubItemRow.kind == kind.value, HubItemRow.source_id.in_(ids),
                                                         HubItemRow.deleted_at.is_(None)).values(deleted_at=self._clock()))
            db.commit()
            return result.rowcount or 0

    def live_ids(self, source: str, kind: ItemKind, since: datetime | None = None, until: datetime | None = None) -> set[str]:
        stmt = select(HubItemRow.source_id).where(HubItemRow.source == source, HubItemRow.kind == kind.value, HubItemRow.deleted_at.is_(None))
        if since:
            stmt = stmt.where(HubItemRow.source_timestamp >= since)
        if until:
            stmt = stmt.where(HubItemRow.source_timestamp < _utc(until))
        with self._sf() as db:
            return set(db.execute(stmt).scalars())

    def purge(self, source: str) -> int:
        """Delete everything stored from `source` (used when the user disconnects with deletion)."""
        with self._sf() as db:
            result = db.execute(delete(HubItemRow).where(HubItemRow.source == source))
            db.commit()
            return result.rowcount or 0

    def prune(self, older_than_days: int) -> int:
        cutoff = self._clock() - timedelta(days=older_than_days)
        with self._sf() as db:
            result = db.execute(delete(HubItemRow).where(HubItemRow.retrieved_at < cutoff))
            db.commit()
            return result.rowcount or 0

    def count(self, source: str | None = None) -> int:
        stmt = select(func.count()).select_from(HubItemRow).where(HubItemRow.deleted_at.is_(None))
        if source:
            stmt = stmt.where(HubItemRow.source == source)
        with self._sf() as db:
            return int(db.execute(stmt).scalar_one())
