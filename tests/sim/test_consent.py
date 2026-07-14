from __future__ import annotations

from email.message import EmailMessage

from thenetwork.sim.personas.consent import (
    digest_token,
    intro_token,
    make_reply_thread_faithful,
    thread_token_of,
)


TOKEN_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TOKEN_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DIGEST_TOKEN = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def test_intro_token_reads_subject_first():
    message = EmailMessage()
    message["Subject"] = f"Possible introduction [intro:{TOKEN_A}]"
    message.set_content(f"[intro:{TOKEN_B}]")
    assert intro_token(message) == TOKEN_A


def test_intro_token_falls_back_to_visible_body_lines():
    message = EmailMessage()
    message["Subject"] = "Re: hello"
    message.set_content(f"see below\n[intro:{TOKEN_A}]\n> [intro:{TOKEN_B}]")
    assert intro_token(message) == TOKEN_A


def test_intro_token_ignores_quoted_lines_and_tokenless_mail():
    message = EmailMessage()
    message["Subject"] = "Re: hello"
    message.set_content(f"thanks\n> [intro:{TOKEN_B}]")
    assert intro_token(message) is None


def test_decision_with_bundled_tokens_keeps_only_the_thread_token():
    body = f"Yes\n[intro:{TOKEN_A}]\n[intro:{TOKEN_B}]"
    assert make_reply_thread_faithful(body, TOKEN_A) == f"Yes\n[intro:{TOKEN_A}]"


def test_decision_with_wrong_token_is_rebound_to_the_thread():
    body = f"No\n[intro:{TOKEN_B}]"
    assert make_reply_thread_faithful(body, TOKEN_A) == f"No\n[intro:{TOKEN_A}]"


def test_decision_without_any_token_gains_the_thread_token():
    assert make_reply_thread_faithful("REVOKE", TOKEN_A) == f"REVOKE\n[intro:{TOKEN_A}]"


def test_prose_with_inline_token_is_not_normalized_as_a_decision():
    body = f"Yes [intro:{TOKEN_A}], happy to meet."
    assert make_reply_thread_faithful(body, TOKEN_A) == body


def test_question_keeps_own_token_and_drops_foreign_ones():
    body = f"Why this match?\n[intro:{TOKEN_A}]\n[intro:{TOKEN_B}]"
    assert make_reply_thread_faithful(body, TOKEN_A) == (
        f"Why this match?\n[intro:{TOKEN_A}]"
    )


def test_no_thread_strips_every_token():
    body = f"Thanks.\n[intro:{TOKEN_A}]\n[intro:{TOKEN_B}]"
    assert make_reply_thread_faithful(body, None) == "Thanks."


def test_token_free_body_passes_through_unchanged():
    body = "Just an update: I moved to Lisbon.\nMore soon."
    assert make_reply_thread_faithful(body, TOKEN_A) == body
    assert make_reply_thread_faithful(body, None) == body


def test_body_reduced_to_nothing_becomes_empty():
    assert make_reply_thread_faithful(f"[intro:{TOKEN_B}]", None) == ""


def test_digest_token_reads_subject_first():
    message = EmailMessage()
    message["Subject"] = f"Possible introductions [digest:{DIGEST_TOKEN}]"
    message.set_content(f"[digest:{TOKEN_B}]")
    assert digest_token(message) == DIGEST_TOKEN


def test_thread_token_of_distinguishes_intro_and_digest():
    intro_message = EmailMessage()
    intro_message["Subject"] = f"Possible introduction [intro:{TOKEN_A}]"
    intro_message.set_content("Reply YES to opt in.")
    assert thread_token_of(intro_message) == ("intro", TOKEN_A)

    digest_message = EmailMessage()
    digest_message["Subject"] = f"Possible introductions [digest:{DIGEST_TOKEN}]"
    digest_message.set_content("A. some gist\n\nReply with a letter, or NONE.")
    assert thread_token_of(digest_message) == ("digest", DIGEST_TOKEN)

    plain_message = EmailMessage()
    plain_message["Subject"] = "Re: hello"
    plain_message.set_content("thanks")
    assert thread_token_of(plain_message) is None


def test_digest_selection_binds_to_the_digest_thread():
    body = f"A\n[digest:{DIGEST_TOKEN}]"
    assert (
        make_reply_thread_faithful(body, DIGEST_TOKEN, "digest")
        == f"A\n[digest:{DIGEST_TOKEN}]"
    )


def test_digest_selection_with_stray_intro_token_keeps_only_the_digest_token():
    body = f"A, C\n[digest:{DIGEST_TOKEN}]\n[intro:{TOKEN_A}]"
    assert (
        make_reply_thread_faithful(body, DIGEST_TOKEN, "digest")
        == f"A, C\n[digest:{DIGEST_TOKEN}]"
    )


def test_digest_none_selection_gains_the_thread_token():
    assert (
        make_reply_thread_faithful("NONE", DIGEST_TOKEN, "digest")
        == f"NONE\n[digest:{DIGEST_TOKEN}]"
    )
