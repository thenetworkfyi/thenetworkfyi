"""Shared pytest fixtures: seeded people + memories."""

from __future__ import annotations

import os
import socket
import ssl
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace

import pytest
import pydantic_ai.models as pydantic_ai_models
from dotenv import dotenv_values

# Default-deny every provider-backed model request in the test process. The
# autouse fixture below opens this gate only for a test carrying live_model.
pydantic_ai_models.ALLOW_MODEL_REQUESTS = False


# The model settings are deliberately required (no defaults - see
# thenetwork/settings.py), and thenetwork.worker.tasks reads Settings at import
# time, so collection needs values from somewhere. Supply test placeholders
# only for keys that neither the environment nor the repo .env already
# provides: in CI (no .env) every placeholder gets set; locally the .env
# values stay authoritative, which tests/scenarios/test_live_archetypes.py
# depends on to reach the real configured model. The .env path is
# cwd-relative to match how pydantic-settings itself resolves env_file.
_dotenv = dotenv_values(Path(".env"))
for _key, _placeholder in (
    ("AGENT_MODEL", "test:model"),
    ("SMALL_AGENT_MODEL", "test:model"),
    ("EMBED_MODEL", "test:embed"),
    ("RELAY_DOMAIN", "relay.example.test"),
):
    if _key not in os.environ and not _dotenv.get(_key):
        os.environ[_key] = _placeholder

from thenetwork.db.models import Person  # noqa: E402

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://network:network@localhost:5432/test_thenetwork",
)


@pytest.fixture(autouse=True)
def model_request_gate(request, monkeypatch):
    """Allow provider models only for tests explicitly marked ``live_model``."""
    monkeypatch.setattr(
        pydantic_ai_models,
        "ALLOW_MODEL_REQUESTS",
        request.node.get_closest_marker("live_model") is not None,
    )


@dataclass
class _CapturedEnvelope:
    peer: tuple
    mail_from: str | None
    rcpt_tos: list[str] = field(default_factory=list)
    data: bytes = b""


class _CapturingHandler:
    """aiosmtpd handler that records every DATA command instead of relaying it."""

    def __init__(self) -> None:
        self.messages: list[_CapturedEnvelope] = []

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(
            _CapturedEnvelope(
                peer=session.peer,
                mail_from=envelope.mail_from,
                rcpt_tos=list(envelope.rcpt_tos),
                data=envelope.content,
            )
        )
        return "250 Message accepted for delivery"


@pytest.fixture(scope="session")
def _smtp_sink_server():
    """Session-scoped in-process SMTP+STARTTLS server for outbound mail tests.

    thenetwork/email/outbound.py's send functions call the real smtplib.SMTP
    against this server instead of being replaced by a mock, so the actual
    STARTTLS/AUTH/DATA wire path (headers, MIME structure) is exercised. The
    ephemeral port is pre-allocated via a throwaway socket bind: passing
    port=0 straight to aiosmtpd's Controller triggers a connection-refused
    race because it never learns the real bound port before its own
    readiness probe.
    """
    import trustme
    from aiosmtpd.controller import Controller
    from aiosmtpd.smtp import AuthResult

    ca = trustme.CA()
    server_cert = ca.issue_cert("127.0.0.1")
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_cert.configure_cert(tls_context)

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    handler = _CapturingHandler()
    controller = Controller(
        handler,
        hostname="127.0.0.1",
        port=port,
        tls_context=tls_context,
        authenticator=lambda *args, **kwargs: AuthResult(success=True),
        auth_require_tls=True,
    )
    controller.start()
    try:
        with ca.cert_pem.tempfile() as ca_cert_path:
            yield SimpleNamespace(
                host="127.0.0.1",
                port=port,
                handler=handler,
                ca_cert_path=ca_cert_path,
            )
    finally:
        controller.stop()


@pytest.fixture
def smtp_sink(_smtp_sink_server, monkeypatch):
    """Point the real Settings singleton at the in-process SMTP sink.

    Tests exercise thenetwork.email.outbound's send functions unmodified;
    only Settings.smtp_host/smtp_port (and the other fields that path reads)
    are swapped, mirroring the pg_engine fixture's settings-swap pattern. No
    test configuration reaches the deployment SMTP_HOST default
    (smtp.gmail.com) because every field this path reads is overridden here.
    """
    import thenetwork.settings as settings_module

    server = _smtp_sink_server
    server.handler.messages.clear()
    monkeypatch.setenv("SSL_CERT_FILE", server.ca_cert_path)

    sink_settings = settings_module.get_settings().model_copy(
        update={
            "smtp_host": server.host,
            "smtp_port": server.port,
            "smtp_account": "sink-agent@example.com",
            "smtp_password": "sink-password",
            "email_from": "agent@example.com",
            "imap_account": "agent@example.com",
            "imap_password": "secret",
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_sent_folder": "Sent",
            "relay_domain": "relay.example.com",
            "admin_emails": [],
        }
    )
    monkeypatch.setattr(settings_module, "_settings", sink_settings)

    class _Sink:
        host = server.host
        port = server.port

        @staticmethod
        def override(**overrides) -> None:
            """Layer additional Settings overrides onto the sink config."""
            monkeypatch.setattr(
                settings_module,
                "_settings",
                settings_module.get_settings().model_copy(update=overrides),
            )

        @property
        def envelopes(self) -> list[_CapturedEnvelope]:
            return list(server.handler.messages)

        @property
        def raw_messages(self) -> list[bytes]:
            return [envelope.data for envelope in server.handler.messages]

        @property
        def messages(self):
            # Normalize wire CRLF to LF before parsing so get_content() matches
            # the LF-terminated text callers compose with EmailMessage/policy
            # default, rather than asserting on an SMTP-transport artifact.
            return [
                BytesParser(policy=policy.default).parsebytes(
                    envelope.data.replace(b"\r\n", b"\n")
                )
                for envelope in server.handler.messages
            ]

    return _Sink()


@pytest.fixture
def seeded_people():
    """In-memory Person objects for unit tests that don't need a DB."""
    return [
        Person(id=str(uuid.uuid4()), name="Alice", email="alice@test.com"),
        Person(id=str(uuid.uuid4()), name="Bob", email="bob@test.com"),
        Person(id=str(uuid.uuid4()), name="Carol", email="carol@test.com"),
        Person(id=str(uuid.uuid4()), name="Dave", email="dave@test.com"),
    ]


@pytest.fixture(scope="session")
def pg_engine():
    """Migrated pgvector engine, with a container fallback for developer hosts."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    import thenetwork.settings as settings_module

    database_url = TEST_DATABASE_URL
    container = None
    try:
        engine = create_engine(database_url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
    except Exception as direct_error:
        engine.dispose()
        try:
            from testcontainers.postgres import PostgresContainer

            container = PostgresContainer("pgvector/pgvector:pg18")
            container.start()
            database_url = container.get_connection_url().replace(
                "postgresql+psycopg2://", "postgresql+psycopg://", 1
            )
            engine = create_engine(database_url, pool_pre_ping=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                conn.commit()
        except Exception as fallback_error:
            if container is not None:
                container.stop()
            pytest.skip(
                "pgvector DB is unavailable and the testcontainers fallback "
                f"could not start (direct={direct_error!r}, fallback={fallback_error!r})"
            )

    # Alembic owns the schema. The box image/bootstrap owns only the server,
    # databases, and vector extension because the repository migrations are not
    # present when that image is built. Temporarily point the cached Settings at
    # the selected test database because alembic/env.py deliberately constructs
    # its URL from Settings rather than trusting alembic.ini.
    parsed_url = make_url(database_url)
    original_settings = settings_module._settings
    migration_settings = settings_module.get_settings().model_copy(
        update={
            "postgres_host": parsed_url.host or "localhost",
            "postgres_port": parsed_url.port or 5432,
            "postgres_db": parsed_url.database or "test_thenetwork",
            "postgres_user": parsed_url.username or "network",
            "postgres_password": parsed_url.password or "network",
        }
    )
    settings_module._settings = migration_settings
    try:
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(config, "head")
    except Exception as exc:
        raise RuntimeError(
            f"failed to migrate scenario test database {database_url!r}"
        ) from exc
    finally:
        settings_module._settings = original_settings

    try:
        yield engine
    finally:
        engine.dispose()
        if container is not None:
            container.stop()


@pytest.fixture
def scenario_database(pg_engine):
    """Give one scenario run an isolated real PostgreSQL schema and sessions.

    A dataset evaluates cases concurrently and intentionally reuses opaque ids
    such as ``user-maya``. Separate schemas keep those realistic database
    writes independent without replacing SQLModel sessions with MagicMocks.
    """
    from sqlalchemy.orm import sessionmaker
    from sqlmodel import Session, SQLModel

    @contextmanager
    def isolated_session_factory():
        schema = f"scenario_{uuid.uuid4().hex}"
        with pg_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            connection.exec_driver_sql(f'SET search_path TO "{schema}", public')
            # PostgreSQL's table-existence check follows the full search_path;
            # public contains the migrated schema, so checkfirst would mistake
            # those tables for this run's and send writes back to public.
            SQLModel.metadata.create_all(connection, checkfirst=False)
            connection.commit()
            factory = sessionmaker(
                bind=connection,
                class_=Session,
                autocommit=False,
                autoflush=False,
            )

            @contextmanager
            def open_session():
                session = factory()
                try:
                    yield session
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                finally:
                    session.close()

            try:
                yield open_session
            finally:
                connection.rollback()

        with pg_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')

    return isolated_session_factory


@pytest.fixture
def seeded_db(pg_engine, monkeypatch):
    """Persist people + memories with embeddings to real pgvector DB.

    People: alice, bob, carol, dave.
    Memories (all have gist so they are cross-user eligible):
      alice-mem:  refs=[alice_id], e0            gist="ml engineer"
      bob-mem:    refs=[bob_id],   e1            gist="systems programmer"
      carol-mem:  refs=[carol_id], 0.9*e0+0.1*e1 gist="llm builder"
      intro-mem:  refs=[alice_id, carol_id], e0  gist="connected two ml people"

    query_ml = e0 - nearest memories: alice-mem ≈ intro-mem > carol-mem > bob-mem.
    """
    from sqlalchemy import text
    from sqlalchemy.orm import sessionmaker
    from sqlmodel import Session
    import thenetwork.db.session as sess_mod

    def _vec_str(dim0: float = 0.0, dim1: float = 0.0) -> str:
        v = [0.0] * 1536
        v[0] = dim0
        v[1] = dim1
        return "[" + ",".join(str(x) for x in v) + "]"

    def _vec(dim0: float = 0.0, dim1: float = 0.0) -> list[float]:
        v = [0.0] * 1536
        v[0] = dim0
        v[1] = dim1
        return v

    alice_id = str(uuid.uuid4())
    bob_id = str(uuid.uuid4())
    carol_id = str(uuid.uuid4())
    dave_id = str(uuid.uuid4())
    mem_alice_id = str(uuid.uuid4())
    mem_bob_id = str(uuid.uuid4())
    mem_carol_id = str(uuid.uuid4())
    mem_intro_id = str(uuid.uuid4())

    test_factory = sessionmaker(
        bind=pg_engine,
        class_=Session,
        autocommit=False,
        autoflush=False,
    )
    monkeypatch.setattr(sess_mod, "_engine", pg_engine)
    monkeypatch.setattr(sess_mod, "_SessionLocal", test_factory)

    test_emails = ["alice@test.com", "bob@test.com", "carol@test.com", "dave@test.com"]

    with pg_engine.connect() as conn:
        conn.execute(
            text("""
            DELETE FROM memories
            WHERE refs && ARRAY(SELECT id FROM people WHERE email = ANY(:e))::text[]
        """),
            {"e": test_emails},
        )
        conn.execute(
            text("DELETE FROM people WHERE email = ANY(:e)"), {"e": test_emails}
        )
        conn.commit()

    with pg_engine.connect() as conn:
        conn.execute(
            text("""
            INSERT INTO people (id, name, email) VALUES
              (:aid, 'Alice', 'alice@test.com'),
              (:bid, 'Bob',   'bob@test.com'),
              (:cid, 'Carol', 'carol@test.com'),
              (:did, 'Dave',  'dave@test.com')
        """),
            {"aid": alice_id, "bid": bob_id, "cid": carol_id, "did": dave_id},
        )
        conn.commit()

    mem_rows = [
        (
            mem_alice_id,
            "Alice is an ML engineer",
            (1.0, 0.0),
            [alice_id],
            "ml engineer",
        ),
        (
            mem_bob_id,
            "Bob writes systems software in Rust",
            (0.0, 1.0),
            [bob_id],
            "systems programmer",
        ),
        (
            mem_carol_id,
            "Carol builds LLM products",
            (0.9, 0.1),
            [carol_id],
            "llm builder",
        ),
        (
            mem_intro_id,
            "Introduced Alice and Carol",
            (1.0, 0.0),
            [alice_id, carol_id],
            "connected two ml people",
        ),
    ]

    with pg_engine.connect() as conn:
        for mem_id, mem_text, emb_dims, refs, gist in mem_rows:
            refs_sql = "ARRAY[" + ",".join(f"'{r}'" for r in refs) + "]::text[]"
            conn.execute(
                text(f"""
                INSERT INTO memories (id, text, embedding, refs, gist, created_at)
                VALUES (:mid, :txt, CAST(:emb AS vector), {refs_sql}, :gist, NOW())
            """),
                {
                    "mid": mem_id,
                    "txt": mem_text,
                    "emb": _vec_str(*emb_dims),
                    "gist": gist,
                },
            )
        conn.commit()

    yield {
        "alice_id": alice_id,
        "bob_id": bob_id,
        "carol_id": carol_id,
        "dave_id": dave_id,
        "mem_alice_id": mem_alice_id,
        "mem_bob_id": mem_bob_id,
        "mem_carol_id": mem_carol_id,
        "mem_intro_id": mem_intro_id,
        "query_ml": _vec(1.0, 0.0),
    }

    with pg_engine.connect() as conn:
        conn.execute(
            text("DELETE FROM memories WHERE id = ANY(:ids)"),
            {"ids": [mem_alice_id, mem_bob_id, mem_carol_id, mem_intro_id]},
        )
        conn.execute(
            text("DELETE FROM people WHERE id = ANY(:ids)"),
            {"ids": [alice_id, bob_id, carol_id, dave_id]},
        )
        conn.commit()
