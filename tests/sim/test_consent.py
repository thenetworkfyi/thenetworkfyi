from __future__ import annotations

from email.message import EmailMessage

from thenetwork.sim.personas.consent import intro_token, make_reply_thread_faithful


TOKEN_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TOKEN_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


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


def test_decision_with_inline_token_moves_it_to_the_second_line():
    body = f"Yes [intro:{TOKEN_A}], happy to meet."
    assert make_reply_thread_faithful(body, TOKEN_A) == (
        f"Yes , happy to meet.\n[intro:{TOKEN_A}]"
    )


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
