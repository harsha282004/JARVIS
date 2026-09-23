"""DocumentRepository: persistence of document metadata (`rag_documents`)."""

from collections.abc import Callable, Sequence
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agent.rag.models import Document, DocumentStatus, RAGStorageError, SourceType
from backend.models.rag import RagDocument

SessionFactory = Callable[[], Session]


def _aware(value: datetime | None) -> datetime | None:
    # SQLite drops tzinfo; PostgreSQL keeps it. Everything is UTC.
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _to_model(row: RagDocument) -> Document:
    return Document(
        document_id=row.id, filename=row.filename, title=row.title, source_type=SourceType(row.source_type),
        source_location=row.source_location, content_hash=row.content_hash, status=DocumentStatus(row.status),
        last_error=row.last_error, page_count=row.page_count, chunk_count=row.chunk_count,
        metadata=row.extra or {}, created_at=_aware(row.created_at), updated_at=_aware(row.updated_at),
        indexed_at=_aware(row.indexed_at),
    )


def _apply(row: RagDocument, doc: Document) -> None:
    row.filename = doc.filename[:260]
    row.title = doc.title[:300] if doc.title else None
    row.source_type = doc.source_type.value
    row.source_location = doc.source_location[:1024]
    row.content_hash = doc.content_hash
    row.status = doc.status.value
    row.last_error = doc.last_error[:300] if doc.last_error else None
    row.page_count = doc.page_count
    row.chunk_count = doc.chunk_count
    row.extra = dict(doc.metadata)
    row.created_at = doc.created_at
    row.updated_at = doc.updated_at
    row.indexed_at = doc.indexed_at


class DocumentRepository:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    def _run(self, work: Callable[[Session], object]):
        try:
            with self._session_factory() as session:
                result = work(session)
                session.commit()
                return result
        except SQLAlchemyError as exc:
            raise RAGStorageError(f"Document database error ({type(exc).__name__})") from None

    def add(self, doc: Document) -> Document:
        def work(session: Session) -> None:
            row = RagDocument(id=doc.document_id)
            _apply(row, doc)
            session.add(row)

        self._run(work)
        return doc

    def save(self, doc: Document) -> bool:
        def work(session: Session) -> bool:
            row = session.get(RagDocument, doc.document_id)
            if row is None:
                return False
            _apply(row, doc)
            return True

        return bool(self._run(work))

    def get(self, document_id: str) -> Document | None:
        def work(session: Session) -> Document | None:
            row = session.get(RagDocument, document_id)
            return _to_model(row) if row else None

        return self._run(work)

    def get_by_location(self, source_location: str) -> Document | None:
        stmt = (
            select(RagDocument).where(RagDocument.source_location == source_location)
            .order_by(RagDocument.updated_at.desc()).limit(1)
        )
        return self._run(lambda s: (lambda r: _to_model(r) if r else None)(s.scalars(stmt).first()))

    def find_indexed_by_hash(self, content_hash: str) -> Document | None:
        stmt = (
            select(RagDocument)
            .where(RagDocument.content_hash == content_hash, RagDocument.status == DocumentStatus.INDEXED.value)
            .order_by(RagDocument.created_at).limit(1)
        )
        return self._run(lambda s: (lambda r: _to_model(r) if r else None)(s.scalars(stmt).first()))

    def list(self, statuses: Sequence[DocumentStatus] | None = None) -> list[Document]:
        stmt = select(RagDocument).order_by(RagDocument.created_at, RagDocument.id)
        if statuses is not None:
            stmt = stmt.where(RagDocument.status.in_([s.value for s in statuses]))
        return self._run(lambda s: [_to_model(r) for r in s.scalars(stmt)])
