from __future__ import annotations

from thenetwork.db.models import Memory


def seal_text(memory: Memory, sender_id: str | None) -> str:
    refs = memory.refs or []
    if not refs:
        return memory.text
    if len(refs) == 1 and refs[0] == sender_id:
        return memory.text
    if memory.gist is None:
        raise ValueError(
            f'Memory {memory.id} has refs {refs!r} but no gist; run sanitization first'
        )
    return memory.gist
