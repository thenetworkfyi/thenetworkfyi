"""CLI and backend implementation for redacting PII from a Memory record."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from typing import Optional

from sqlmodel import Session

from thenetwork.audit import audit_event
from thenetwork.db.models import Memory
from thenetwork.db.session import get_session
from thenetwork.embed.embeddings import embed_text
from thenetwork.memory.sanitize import sanitize_memory_high_fidelity, sanitize_text


def _audit_blocked_write(memory: Memory, session: Session) -> None:
    """Roll back and audit a fail-closed refusal to write an unsanitized memory."""
    session.rollback()
    audit_event(
        "database.action",
        action="update",
        record_type="memory",
        outcome="blocked",
        refs_count=len(memory.refs) if memory.refs else 0,
    )


async def redact_memory_record(
    memory_id: str,
    session: Session,
    *,
    string_to_redact: Optional[str] = None,
    pattern_to_redact: Optional[str] = None,
    replacement: str = "[redacted]",
    commit: bool = False,
) -> dict:
    """Redact PII from a memory record and re-generate its gist and embedding.

    Returns a summary dictionary describing the changes.
    """
    memory = session.get(Memory, memory_id)
    if not memory:
        raise ValueError(f"Memory not found: {memory_id}")

    original_text = memory.text
    original_gist = memory.gist

    if string_to_redact:
        new_text = original_text.replace(string_to_redact, replacement)
    elif pattern_to_redact:
        new_text = re.sub(pattern_to_redact, replacement, original_text)
    else:
        new_text = sanitize_text(original_text)

    memory.text = new_text

    new_gist: Optional[str] = None
    if memory.refs:
        try:
            sanitized_gist = await sanitize_memory_high_fidelity(memory, session)
        except Exception as exc:
            _audit_blocked_write(memory, session)
            raise RuntimeError(f"Sanitization failed for memory {memory_id}") from exc
        if memory.gist is None and isinstance(sanitized_gist, str):
            memory.gist = sanitized_gist
        if not memory.gist:
            _audit_blocked_write(memory, session)
            raise RuntimeError(
                f"Sanitization failed to produce gist for memory {memory_id}"
            )
        new_gist = memory.gist
        embedding_source = memory.gist
    else:
        memory.gist = None
        new_gist = None
        embedding_source = memory.text

    if commit:
        new_embedding = await embed_text(embedding_source)
        memory.embedding = new_embedding

    summary = {
        "memory_id": memory_id,
        "original_text": original_text,
        "new_text": new_text,
        "original_gist": original_gist,
        "new_gist": new_gist,
        "refs": memory.refs,
        "text_changed": original_text != new_text,
        "gist_changed": original_gist != new_gist,
        "committed": commit,
    }

    if commit:
        session.commit()
        audit_event(
            "database.action",
            action="update",
            record_type="memory",
            outcome="success",
            result_count=1,
            refs_count=len(memory.refs) if memory.refs else 0,
        )
    else:
        session.rollback()
        audit_event(
            "database.action",
            action="update",
            record_type="memory",
            outcome="dry_run",
            result_count=1,
            refs_count=len(memory.refs) if memory.refs else 0,
        )

    return summary


def _non_empty_str(value: str) -> str:
    if value == "":
        raise argparse.ArgumentTypeError("value must not be empty")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Redact PII from a Memory record in the database."
    )
    parser.add_argument("memory_id", help="UUID of the Memory record to redact")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Commit changes to database (default: dry-run)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--string",
        dest="string_to_redact",
        type=_non_empty_str,
        help="Specific exact text string to redact from memory text",
    )
    group.add_argument(
        "--pattern",
        dest="pattern_to_redact",
        type=_non_empty_str,
        help="Regex pattern to redact from memory text",
    )
    parser.add_argument(
        "--replacement",
        default="[redacted]",
        help="Replacement string for redacted text (default: '[redacted]')",
    )
    return parser


def main(args_list: Optional[list[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(args_list)

    with get_session() as session:
        try:
            summary = asyncio.run(
                redact_memory_record(
                    args.memory_id,
                    session,
                    string_to_redact=args.string_to_redact,
                    pattern_to_redact=args.pattern_to_redact,
                    replacement=args.replacement,
                    commit=args.commit,
                )
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(2)
        except Exception as exc:
            print(f"Error executing redaction: {exc}", file=sys.stderr)
            sys.exit(1)

    print(f"Memory ID: {summary['memory_id']}")
    print(f"Committed: {summary['committed']}")
    print(f"Text Changed: {summary['text_changed']}")
    print(f"Original Text: {summary['original_text']}")
    print(f"New Text:      {summary['new_text']}")
    if summary["refs"]:
        print(f"Gist Changed: {summary['gist_changed']}")
        print(f"Original Gist: {summary['original_gist']}")
        print(f"New Gist:      {summary['new_gist']}")

    if not summary["committed"]:
        print(
            "\nDry run complete; no changes were committed to database. Embedding is recomputed only on --commit. Use --commit to apply."
        )


if __name__ == "__main__":
    main()
