# Database

JARVIS uses **PostgreSQL** as its primary datastore. Vector storage (via
`pgvector` or an equivalent) is planned for a later phase once memory/RAG
is implemented — it is not set up yet.

## Layout

- `backend/core/database.py` — engine/session setup and a connectivity
  check (`check_database_connection`). This is the only piece of database
  infrastructure Phase 0 implements.
- `backend/models/base.py` — the shared SQLAlchemy `Base` and a
  `TimestampMixin`. No domain models exist yet.
- `database/migrations/` — Alembic migration environment, configured but
  with no revisions yet (there is no schema to migrate to).
- `database/alembic.ini` — Alembic configuration, reading `DATABASE_URL`
  from the application settings at runtime.

## Local setup

1. Install PostgreSQL locally (or run it in a container).
2. Create a database and user matching your `.env` `DATABASE_URL`, e.g.:
   ```sql
   CREATE USER jarvis WITH PASSWORD 'jarvis';
   CREATE DATABASE jarvis OWNER jarvis;
   ```
3. Copy `.env.example` to `.env` and adjust `DATABASE_URL` if needed.
4. Verify connectivity:
   ```
   python scripts/check_db.py
   ```

If PostgreSQL is not running, the application will not pretend the
connection succeeded — `check_database_connection()` returns `False` and
logs the underlying error.

## Migrations

Once real models exist, generate and apply migrations with:
```
alembic -c database/alembic.ini revision --autogenerate -m "description"
alembic -c database/alembic.ini upgrade head
```
No revisions are included in Phase 0.
