"""Tests for the admin channel: PGP/MIME auth, command parsing, task routing."""
from __future__ import annotations

import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import gnupg
import pytest


# ─── PGP/MIME test fixtures ──────────────────────────────────────────────────
#
# Real gpg-signed messages, not mocks: the property under test is "a detached
# signature over these exact bytes verifies against this exact key", which a
# mock of gnupg.GPG can't exercise. Keygen is ~0.1s with no_protection=True
# (no passphrase), so a real per-module keypair is cheap.
#
# passphrase must be omitted (or None), never "" -- python-gnupg's sign_file()
# treats `passphrase is not None` as "write a passphrase to gpg's stdin" but
# `if passphrase:` (falsy for "") as "don't actually write it", which leaves
# gpg's --passphrase-fd 0 expecting a line that never comes and desyncs the
# whole stdin stream, silently corrupting the signed data. Keys generated with
# no_protection=True need no passphrase at all, so the bug is simply avoided.

def _gen_gpg_identity(name_email: str) -> SimpleNamespace:
    home = tempfile.mkdtemp(prefix="thenetwork-test-gpg-")
    gpg = gnupg.GPG(gnupghome=home)
    gpg.encoding = "utf-8"
    key_input = gpg.gen_key_input(
        key_type="eddsa",
        key_curve="ed25519",
        name_email=name_email,
        expire_date="0",
        no_protection=True,
    )
    key = gpg.gen_key(key_input)
    fingerprint = str(key)
    if not fingerprint:
        pytest.skip(f"gpg keygen failed: {key.stderr}")
    return SimpleNamespace(gpg=gpg, fingerprint=fingerprint, public_key=gpg.export_keys(fingerprint))


@pytest.fixture(scope="module")
def admin_identity():
    """The trusted admin signer: its public key is what ADMIN_GPG_PUBLIC_KEY holds."""
    return _gen_gpg_identity("admin@example.com")


@pytest.fixture(scope="module")
def attacker_identity():
    """A different keypair, never imported as the trusted key."""
    return _gen_gpg_identity("attacker@evil.com")


def _build_signed_part(cleartext: str) -> bytes:
    return b"Content-Type: text/plain; charset=us-ascii\r\n\r\n" + cleartext.encode("ascii")


def _build_pgp_mime_message(
    identity: SimpleNamespace,
    *,
    signed_part: bytes,
    sender: str = "admin@example.com",
    subject: str = "ADMIN: status",
    boundary: str = "THENETWORKTESTBOUNDARY",
) -> bytes:
    """Build a raw multipart/signed (RFC 3156) message, signed for real."""
    sig = identity.gpg.sign(signed_part, keyid=identity.fingerprint, detach=True, passphrase=None)
    assert sig.status == "signature created", sig.stderr
    sig_bytes = bytes(sig.data)

    header = (
        f"From: {sender}\r\n"
        f"To: agent@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"MIME-Version: 1.0\r\n"
        f'Content-Type: multipart/signed; boundary="{boundary}"; '
        f'protocol="application/pgp-signature"; micalg="pgp-sha256"\r\n'
        f"\r\n"
    ).encode("ascii")
    body = (
        f"--{boundary}\r\n".encode("ascii")
        + signed_part
        + f"\r\n--{boundary}\r\n".encode("ascii")
        + b'Content-Type: application/pgp-signature; name="signature.asc"\r\n'
        + b"Content-Description: OpenPGP digital signature\r\n"
        + b"Content-Transfer-Encoding: 7bit\r\n\r\n"
        + sig_bytes
        + f"\r\n--{boundary}--\r\n".encode("ascii")
    )
    return header + body


def _admin_message(
    identity: SimpleNamespace,
    *,
    command: str = "status",
    extra_body: str = "",
    sender: str = "admin@example.com",
    subject: str | None = None,
) -> bytes:
    lines = [f"COMMAND: {command}", ""]
    if extra_body:
        lines.append(extra_body)
    cleartext = "\r\n".join(lines)
    signed_part = _build_signed_part(cleartext)
    return _build_pgp_mime_message(
        identity,
        signed_part=signed_part,
        sender=sender,
        subject=subject if subject is not None else f"ADMIN: {command}",
    )


def _settings(emails=("admin@example.com",), public_key="", window=300):
    s = MagicMock()
    s.admin_emails = list(emails)
    s.admin_gpg_public_key = public_key
    s.admin_replay_window_seconds = window
    return s


def _fresh_nonce_session():
    """A get_session() mock whose nonce store is always empty (no replay seen)."""
    session = MagicMock()
    session.get.return_value = None
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)
    return cm, session


@pytest.fixture(autouse=True)
def _reset_gpg_cache():
    """auth.py caches a module-level GPG instance keyed by public key material;
    clear it so each test's _settings(public_key=...) is actually re-imported."""
    import thenetwork.admin.auth as auth_mod

    auth_mod._gpg_instance = None
    auth_mod._gpg_key_material = None
    yield
    auth_mod._gpg_instance = None
    auth_mod._gpg_key_material = None


# ─── Auth ────────────────────────────────────────────────────────────────────

def test_verify_admin_request_valid(admin_identity):
    from thenetwork.admin.auth import verify_admin_request

    raw = _admin_message(admin_identity, command="status")
    cm, session = _fresh_nonce_session()
    with patch(
        "thenetwork.admin.auth.get_settings",
        return_value=_settings(public_key=admin_identity.public_key),
    ), patch("thenetwork.admin.auth.get_session", return_value=cm):
        result = verify_admin_request("admin@example.com", "ADMIN: status", raw)

    assert result is not None
    assert "COMMAND: status" in result
    session.add.assert_called_once()


def test_verify_admin_request_case_insensitive_subject(admin_identity):
    from thenetwork.admin.auth import verify_admin_request

    raw = _admin_message(admin_identity, command="status", subject="admin: status")
    cm, _ = _fresh_nonce_session()
    with patch(
        "thenetwork.admin.auth.get_settings",
        return_value=_settings(public_key=admin_identity.public_key),
    ), patch("thenetwork.admin.auth.get_session", return_value=cm):
        assert verify_admin_request("admin@example.com", "admin: status", raw) is not None


def test_verify_admin_request_wrong_sender(admin_identity):
    from thenetwork.admin.auth import verify_admin_request

    raw = _admin_message(admin_identity, command="status")
    with patch(
        "thenetwork.admin.auth.get_settings",
        return_value=_settings(public_key=admin_identity.public_key),
    ):
        assert verify_admin_request("attacker@evil.com", "ADMIN: status", raw) is None


def test_verify_admin_request_signed_by_untrusted_key(admin_identity, attacker_identity):
    """A signature that verifies fine against the signer's own key must still
    be rejected if that key isn't the one configured in ADMIN_GPG_PUBLIC_KEY."""
    from thenetwork.admin.auth import verify_admin_request

    raw = _admin_message(attacker_identity, command="status", sender="admin@example.com")
    with patch(
        "thenetwork.admin.auth.get_settings",
        return_value=_settings(public_key=admin_identity.public_key),
    ):
        assert verify_admin_request("admin@example.com", "ADMIN: status", raw) is None


def test_verify_admin_request_missing_command(admin_identity):
    from thenetwork.admin.auth import verify_admin_request

    signed_part = _build_signed_part("Just some text without the required lines.")
    raw = _build_pgp_mime_message(admin_identity, signed_part=signed_part)
    with patch(
        "thenetwork.admin.auth.get_settings",
        return_value=_settings(public_key=admin_identity.public_key),
    ):
        assert verify_admin_request("admin@example.com", "ADMIN: status", raw) is None


def test_verify_admin_request_not_admin_subject(admin_identity):
    from thenetwork.admin.auth import verify_admin_request

    raw = _admin_message(admin_identity, command="status", subject="Hello there")
    with patch(
        "thenetwork.admin.auth.get_settings",
        return_value=_settings(public_key=admin_identity.public_key),
    ):
        assert verify_admin_request("admin@example.com", "Hello there", raw) is None


def test_verify_admin_request_disabled_when_no_public_key(admin_identity):
    from thenetwork.admin.auth import verify_admin_request

    raw = _admin_message(admin_identity, command="status")
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings(public_key="")):
        assert verify_admin_request("admin@example.com", "ADMIN: status", raw) is None


def test_verify_admin_request_disabled_when_no_emails(admin_identity):
    from thenetwork.admin.auth import verify_admin_request

    raw = _admin_message(admin_identity, command="status")
    with patch(
        "thenetwork.admin.auth.get_settings",
        return_value=_settings(emails=[], public_key=admin_identity.public_key),
    ):
        assert verify_admin_request("admin@example.com", "ADMIN: status", raw) is None


def test_verify_admin_request_expired_timestamp(admin_identity):
    """Freshness comes from the OpenPGP signature's own embedded creation
    timestamp, not an operator-typed TS: line -- simulate an old signature by
    moving the clock forward past the replay window instead."""
    from thenetwork.admin.auth import verify_admin_request

    raw = _admin_message(admin_identity, command="status")
    future = time.time() + 600
    with patch(
        "thenetwork.admin.auth.get_settings",
        return_value=_settings(public_key=admin_identity.public_key, window=300),
    ), patch("thenetwork.admin.auth.time.time", return_value=future):
        assert verify_admin_request("admin@example.com", "ADMIN: status", raw) is None


def test_verify_admin_request_rejects_replayed_signature(admin_identity):
    """Resubmitting the identical signed message a second time must be
    rejected: the dedup key is a hash of the signature bytes themselves, not
    an operator-typed NONCE, so replaying the exact bytes is caught even
    though nothing in the cleartext body changed."""
    from thenetwork.admin.auth import verify_admin_request

    raw = _admin_message(admin_identity, command="status")
    session = MagicMock()
    session.get.return_value = object()  # this signature's hash already seen
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)
    with patch(
        "thenetwork.admin.auth.get_settings",
        return_value=_settings(public_key=admin_identity.public_key),
    ), patch("thenetwork.admin.auth.get_session", return_value=cm):
        assert verify_admin_request("admin@example.com", "ADMIN: status", raw) is None
    session.add.assert_not_called()


def test_verify_admin_request_not_multipart_signed(admin_identity):
    """A plain, unsigned email must never be treated as an admin request,
    regardless of subject -- the subject alone carries no authority."""
    from thenetwork.admin.auth import verify_admin_request

    raw = (
        b"From: admin@example.com\r\n"
        b"Subject: ADMIN: status\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"Just do it, trust me.\r\n"
    )
    with patch(
        "thenetwork.admin.auth.get_settings",
        return_value=_settings(public_key=admin_identity.public_key),
    ):
        assert verify_admin_request("admin@example.com", "ADMIN: status", raw) is None


def test_verify_admin_request_no_raw_message(admin_identity):
    from thenetwork.admin.auth import verify_admin_request

    with patch(
        "thenetwork.admin.auth.get_settings",
        return_value=_settings(public_key=admin_identity.public_key),
    ):
        assert verify_admin_request("admin@example.com", "ADMIN: status", None) is None


def test_verify_admin_request_signature_bound_to_signed_content(admin_identity):
    """A signature made over one COMMAND must not authorize a tampered one --
    unlike the old HMAC-over-subject scheme, PGP/MIME signs the whole body,
    so this is really just re-confirming tampered content fails verification."""
    from thenetwork.admin.auth import verify_admin_request

    raw = _admin_message(admin_identity, command="status")
    tampered = raw.replace(b"COMMAND: status", b"COMMAND: forget xyz")
    with patch(
        "thenetwork.admin.auth.get_settings",
        return_value=_settings(public_key=admin_identity.public_key),
    ):
        assert verify_admin_request("admin@example.com", "ADMIN: status", tampered) is None


def test_extract_command():
    from thenetwork.admin.auth import extract_command

    assert extract_command("COMMAND: status\n\nBody.") == "status"
    assert extract_command("COMMAND: search rust engineers") == "search rust engineers"
    assert extract_command("COMMAND: forget abc-123\n") == "forget abc-123"


def test_extract_body_text_strips_signature_and_quotes():
    from thenetwork.admin.auth import extract_body_text

    cleartext = (
        "COMMAND: remember\n\n"
        "Real content here.\n> Quoted line\nMore content."
    )
    result = extract_body_text(cleartext)
    assert "COMMAND:" not in result
    assert "Quoted line" not in result
    assert "Real content here." in result
    assert "More content." in result


# ─── Task routing ────────────────────────────────────────────────────────────

def test_process_email_routes_admin_to_handler():
    """Admin emails are handled by admin channel, not the agent."""
    import asyncio

    from thenetwork.worker.tasks import process_email

    mock_reply = AsyncMock(return_value="People:   3\nMemories: 10\n")
    mock_send = MagicMock()
    verified_cleartext = "COMMAND: status\n"

    with patch("thenetwork.worker.tasks.check_rate_limit", return_value=True), \
         patch("thenetwork.worker.tasks.scan_content", return_value=(True, None)), \
         patch("thenetwork.worker.tasks.verify_admin_request", return_value=verified_cleartext), \
         patch("thenetwork.worker.tasks.extract_command", return_value="status"), \
         patch("thenetwork.worker.tasks.extract_body_text", return_value=""), \
         patch("thenetwork.worker.tasks.handle_admin_command", mock_reply), \
         patch("thenetwork.worker.tasks.send_reply", mock_send), \
         patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as mock_agent:
        asyncio.run(process_email.func(
            sender_email="admin@example.com",
            subject="ADMIN: status",
            body="COMMAND: status",
            inbound_message_id="<admin123@example.com>",
            inbound_references="<root@example.com>",
        ))

    mock_reply.assert_called_once_with("status", "")
    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs["in_reply_to"] == "<admin123@example.com>"
    assert mock_send.call_args.kwargs["references"] == "<root@example.com> <admin123@example.com>"
    assert "quoted_body_text" not in mock_send.call_args.kwargs
    assert "quoted_date" not in mock_send.call_args.kwargs
    mock_agent.assert_not_called()


def test_process_email_authenticated_non_admin_goes_to_agent():
    """Normal authenticated emails are not intercepted by the admin channel."""
    import asyncio

    from thenetwork.worker.tasks import process_email

    mock_agent = AsyncMock()

    with patch("thenetwork.worker.tasks.check_rate_limit", return_value=True), \
         patch("thenetwork.worker.tasks.scan_content", return_value=(True, None)), \
         patch("thenetwork.worker.tasks.verify_admin_request", return_value=None), \
         patch("thenetwork.worker.tasks.get_session") as mock_gs, \
         patch("thenetwork.worker.tasks.run_agent_for_email", mock_agent):
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.first.return_value = None
        mock_gs.return_value = mock_session
        asyncio.run(process_email.func(
            sender_email="user@example.com",
            subject="Hello",
            body="I'm looking for a cofounder.",
            sender_authenticated=True,
        ))

    mock_agent.assert_called_once()


def test_process_email_drops_unauthenticated_unknown_sender_before_agent():
    """Unauthenticated first contact is rejected before model invocation."""
    import asyncio

    from thenetwork.worker.tasks import process_email

    mock_agent = AsyncMock()

    with patch("thenetwork.worker.tasks.check_rate_limit", return_value=True), \
         patch("thenetwork.worker.tasks.scan_content", return_value=(True, None)), \
         patch("thenetwork.worker.tasks.verify_admin_request", return_value=None), \
         patch("thenetwork.worker.tasks.get_session") as mock_gs, \
         patch("thenetwork.worker.tasks.audit_event") as mock_audit, \
         patch("thenetwork.worker.tasks.run_agent_for_email", mock_agent):
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.first.return_value = None
        mock_gs.return_value = mock_session
        asyncio.run(process_email.func(
            sender_email="stranger@example.com",
            subject="Hello",
            body="Please add me.",
            sender_authenticated=False,
        ))

    mock_agent.assert_not_called()
    mock_audit.assert_any_call(
        "worker.message_rejected",
        reason="unauthenticated_unknown_sender",
    )


def test_process_email_dev_auth_bypass_still_goes_to_agent():
    """When intake marks auth as bypassed, an unknown sender can reach the agent."""
    import asyncio

    from thenetwork.worker.tasks import process_email

    mock_agent = AsyncMock()

    with patch("thenetwork.worker.tasks.check_rate_limit", return_value=True), \
         patch("thenetwork.worker.tasks.scan_content", return_value=(True, None)), \
         patch("thenetwork.worker.tasks.verify_admin_request", return_value=None), \
         patch("thenetwork.worker.tasks.get_session") as mock_gs, \
         patch("thenetwork.worker.tasks.run_agent_for_email", mock_agent):
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.first.return_value = None
        mock_gs.return_value = mock_session
        asyncio.run(process_email.func(
            sender_email="dev@example.com",
            subject="Hello",
            body="Please add me.",
            sender_authenticated=True,
        ))

    mock_agent.assert_called_once()


# ─── Command dispatch ─────────────────────────────────────────────────────────

def test_handle_admin_command_unknown():
    import asyncio
    from thenetwork.admin.commands import handle_admin_command
    result = asyncio.run(handle_admin_command("explode", ""))
    assert "Unknown command" in result
    assert "explode" in result


def test_handle_admin_command_search_no_query():
    import asyncio
    from thenetwork.admin.commands import handle_admin_command
    result = asyncio.run(handle_admin_command("search", ""))
    assert "Usage" in result


def test_handle_admin_command_show_no_arg():
    import asyncio
    from thenetwork.admin.commands import handle_admin_command
    result = asyncio.run(handle_admin_command("show", ""))
    assert "Usage" in result


def test_handle_admin_command_forget_no_arg():
    import asyncio
    from thenetwork.admin.commands import handle_admin_command
    result = asyncio.run(handle_admin_command("forget", ""))
    assert "Usage" in result


def test_handle_admin_command_remember_no_body():
    import asyncio
    from thenetwork.admin.commands import handle_admin_command
    result = asyncio.run(handle_admin_command("remember", "   "))
    assert "No memory text" in result


def test_handle_admin_command_remember_refs_awaits_high_fidelity_sanitizer():
    import asyncio
    from thenetwork.admin.commands import handle_admin_command
    from thenetwork.db.models import Person

    person = MagicMock(spec=Person)
    person.id = "user-alice"

    resolve_session = MagicMock()
    resolve_session.exec.return_value.first.return_value = person
    resolve_cm = MagicMock()
    resolve_cm.__enter__ = MagicMock(return_value=resolve_session)
    resolve_cm.__exit__ = MagicMock(return_value=False)

    write_session = MagicMock()
    added: list[object] = []
    write_session.add.side_effect = added.append
    write_cm = MagicMock()
    write_cm.__enter__ = MagicMock(return_value=write_session)
    write_cm.__exit__ = MagicMock(return_value=False)

    async def fake_sanitize(memory, session):
        memory.gist = "[name] knows privacy-preserving ML."
        return memory.gist

    with patch("thenetwork.admin.commands.embed_text", new=AsyncMock(return_value=[0.0] * 1536)) as mock_embed, \
         patch("thenetwork.admin.commands.get_session", side_effect=[resolve_cm, write_cm]), \
         patch(
             "thenetwork.admin.commands.sanitize_memory_high_fidelity",
             new=AsyncMock(side_effect=fake_sanitize),
         ) as mock_sanitize:
        result = asyncio.run(
            handle_admin_command(
                "remember refs:alice@example.com",
                "Alice Smith knows Bob through privacy-preserving ML.",
            )
        )

    assert "Stored memory" in result
    mock_sanitize.assert_awaited_once()
    mock_embed.assert_awaited_once_with("[name] knows privacy-preserving ML.")
    assert added[0].refs == ["user-alice"]
    assert "Alice" not in added[0].gist
    assert "Bob" not in added[0].gist


def test_handle_admin_command_remember_without_refs_does_not_sanitize():
    import asyncio
    from thenetwork.admin.commands import handle_admin_command

    write_session = MagicMock()
    added: list[object] = []
    write_session.add.side_effect = added.append
    write_cm = MagicMock()
    write_cm.__enter__ = MagicMock(return_value=write_session)
    write_cm.__exit__ = MagicMock(return_value=False)

    raw = "General note with no refs."

    with patch("thenetwork.admin.commands.embed_text", new=AsyncMock(return_value=[0.0] * 1536)) as mock_embed, \
         patch("thenetwork.admin.commands.get_session", return_value=write_cm), \
         patch("thenetwork.admin.commands.sanitize_memory_high_fidelity", new_callable=AsyncMock) as mock_sanitize:
        result = asyncio.run(handle_admin_command("remember", raw))

    assert "Stored memory" in result
    mock_sanitize.assert_not_awaited()
    mock_embed.assert_awaited_once_with(raw)
    assert added[0].refs == []
    assert added[0].gist is None


def test_handle_admin_command_ban_unban():
    import asyncio
    from thenetwork.admin.commands import handle_admin_command
    from thenetwork.db.models import BannedEmail

    # Test ban
    session = MagicMock()
    session.get.return_value = None  # not already banned
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)

    with patch("thenetwork.admin.commands.get_session", return_value=cm), \
         patch("thenetwork.admin.commands.audit_event") as mock_audit:
        result = asyncio.run(handle_admin_command("ban baduser@example.com", ""))

    assert "Banned email: baduser@example.com" in result
    session.add.assert_called_once()
    added_obj = session.add.call_args[0][0]
    assert isinstance(added_obj, BannedEmail)
    assert added_obj.email == "baduser@example.com"
    session.commit.assert_called_once()
    mock_audit.assert_called_once_with(
        "database.action", action="ban", record_type="person", outcome="success"
    )

    # Test ban already banned
    session = MagicMock()
    session.get.return_value = BannedEmail(email="baduser@example.com")
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)

    with patch("thenetwork.admin.commands.get_session", return_value=cm):
        result = asyncio.run(handle_admin_command("ban baduser@example.com", ""))
    assert "already banned" in result
    session.add.assert_not_called()

    # Test unban
    session = MagicMock()
    banned_obj = BannedEmail(email="baduser@example.com")
    session.get.return_value = banned_obj
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)

    with patch("thenetwork.admin.commands.get_session", return_value=cm), \
         patch("thenetwork.admin.commands.audit_event") as mock_audit:
        result = asyncio.run(handle_admin_command("unban baduser@example.com", ""))

    assert "Unbanned email: baduser@example.com" in result
    session.delete.assert_called_once_with(banned_obj)
    session.commit.assert_called_once()
    mock_audit.assert_called_once_with(
        "database.action", action="unban", record_type="person", outcome="success"
    )

    # Test unban not banned
    session = MagicMock()
    session.get.return_value = None
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)

    with patch("thenetwork.admin.commands.get_session", return_value=cm):
        result = asyncio.run(handle_admin_command("unban baduser@example.com", ""))
    assert "not banned" in result
    session.delete.assert_not_called()


def test_handle_admin_command_ban_canonicalizes_gmail_alias():
    # Banning an alias form must store the canonical identity, so a later
    # lookup for a *different* alias of the same mailbox still matches.
    import asyncio
    from thenetwork.admin.commands import handle_admin_command
    from thenetwork.db.models import BannedEmail

    session = MagicMock()
    session.get.return_value = None
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)

    with patch("thenetwork.admin.commands.get_session", return_value=cm):
        result = asyncio.run(
            handle_admin_command("ban ba.nned+spam@googlemail.com", "")
        )

    assert "Banned email: ba.nned+spam@googlemail.com" in result
    added_obj = session.add.call_args[0][0]
    assert isinstance(added_obj, BannedEmail)
    assert added_obj.email == "banned@gmail.com"
    session.get.assert_called_once_with(BannedEmail, "banned@gmail.com")


@pytest.mark.asyncio
async def test_process_email_drops_banned_email_without_reply():
    import asyncio
    from thenetwork.worker.tasks import process_email
    from thenetwork.db.models import BannedEmail

    mock_agent = AsyncMock()
    mock_send_reply = MagicMock()

    banned_obj = BannedEmail(email="banned@example.com")
    session = MagicMock()
    session.get.return_value = banned_obj
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)

    with patch("thenetwork.worker.tasks.check_rate_limit", return_value=True), \
         patch("thenetwork.worker.tasks.scan_content", return_value=(True, None)), \
         patch("thenetwork.worker.tasks.verify_admin_request", return_value=None), \
         patch("thenetwork.worker.tasks.get_session", return_value=cm), \
         patch("thenetwork.worker.tasks.send_reply", mock_send_reply), \
         patch("thenetwork.worker.tasks.audit_event") as mock_audit, \
         patch("thenetwork.worker.tasks.run_agent_for_email", mock_agent):
        await process_email.func(
            sender_email="banned@example.com",
            subject="Hello",
            body="Hey",
            sender_authenticated=True,
        )

    mock_agent.assert_not_called()
    mock_send_reply.assert_not_called()
    mock_audit.assert_any_call(
        "worker.message_rejected",
        reason="banned",
    )


@pytest.mark.asyncio
async def test_process_email_drops_banned_email_alias():
    # A sender using a gmail dot/plus alias of a banned mailbox must still be
    # rejected: the lookup key has to match what _cmd_ban stored.
    from thenetwork.worker.tasks import process_email
    from thenetwork.db.models import BannedEmail

    mock_agent = AsyncMock()
    mock_send_reply = MagicMock()

    banned_obj = BannedEmail(email="banned@gmail.com")
    session = MagicMock()
    session.get.return_value = banned_obj
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)

    with patch("thenetwork.worker.tasks.check_rate_limit", return_value=True), \
         patch("thenetwork.worker.tasks.scan_content", return_value=(True, None)), \
         patch("thenetwork.worker.tasks.verify_admin_request", return_value=None), \
         patch("thenetwork.worker.tasks.get_session", return_value=cm), \
         patch("thenetwork.worker.tasks.send_reply", mock_send_reply), \
         patch("thenetwork.worker.tasks.audit_event") as mock_audit, \
         patch("thenetwork.worker.tasks.run_agent_for_email", mock_agent):
        await process_email.func(
            sender_email="ba.nned+spam@gmail.com",
            subject="Hello",
            body="Hey",
            sender_authenticated=True,
        )

    session.get.assert_called_once_with(BannedEmail, "banned@gmail.com")
    mock_agent.assert_not_called()
    mock_send_reply.assert_not_called()
    mock_audit.assert_any_call(
        "worker.message_rejected",
        reason="banned",
    )

