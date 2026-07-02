"""Admin command execution.

Commands are extracted from the email subject after 'ADMIN:' and executed
server-side with full DB access. The SEAL does not restrict admin reads —
admins can see raw memory text. Commands that write (remember/forget) apply
the same DB path as agent tools.

Command grammar (all positional, space-separated in subject):
  status                    — system stats
  search <query words…>     — semantic search, returns raw text
  show <email_or_person_id> — all memories referencing a person
  forget <memory_id>        — delete one memory
  remember [refs:e1,e2]     — store body text as a new memory
"""
from __future__ import annotations
from sqlalchemy import text
from sqlmodel import Session, select
from thenetwork.audit import audit_event, audit_span
from thenetwork.db.models import Memory, Person
from thenetwork.db.session import get_session
from thenetwork.embed.embeddings import embed_text
from thenetwork.memory.sanitize import sanitize_memory


async def handle_admin_command(command: str, body_text: str) -> str:
    parts = command.split(None, 1)
    verb = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    with audit_span("admin.command"):
        if verb == "status":
            return await _cmd_status()
        if verb == "search":
            return await _cmd_search(args)
        if verb == "show":
            return await _cmd_show(args.strip())
        if verb == "forget":
            return await _cmd_forget(args.strip())
        if verb == "remember":
            return await _cmd_remember(args.strip(), body_text)
        return f"Unknown command: {verb!r}. Valid: status, search, show, forget, remember"


async def _cmd_status() -> str:
    with get_session() as session:
        n_people = len(session.exec(select(Person)).all())
        n_mem = len(session.exec(select(Memory)).all())
    return f"People:   {n_people}\nMemories: {n_mem}\n"


async def _cmd_search(query: str) -> str:
    if not query:
        return "Usage: ADMIN: search <query>"
    vec = await embed_text(query)
    vec_literal = "[" + ",".join(str(v) for v in vec) + "]"
    sql = text("""
        SELECT m.id AS id, m.text AS text, m.refs AS refs,
               1 - (m.embedding <=> CAST(:vec AS vector)) AS sim
        FROM memories m
        WHERE m.embedding IS NOT NULL
        ORDER BY m.embedding <=> CAST(:vec AS vector)
        LIMIT 10
    """)
    with get_session() as session:
        rows = session.execute(sql, {"vec": vec_literal}).fetchall()
    audit_event(
        "database.action", action="search", record_type="memory",
        outcome="found" if rows else "not_found", result_count=len(rows),
    )
    if not rows:
        return "No memories found."
    lines = [f"Top {len(rows)} results for: {query!r}\n"]
    for r in rows:
        refs = ", ".join(r.refs) if r.refs else "(no refs)"
        lines.append(f"[{r.id}] sim={r.sim:.3f} refs={refs}\n{r.text}\n")
    return "\n".join(lines)


async def _cmd_show(ident: str) -> str:
    if not ident:
        return "Usage: ADMIN: show <email_or_person_id>"
    with get_session() as session:
        person = _resolve_person(session, ident)
        if not person:
            audit_event("database.action", action="lookup", record_type="person", outcome="not_found")
            return f"Person not found: {ident!r}"
        mems = session.exec(
            select(Memory).where(Memory.refs.contains([person.id]))
        ).all()
        name, email, pid = person.name, person.email, person.id
    audit_event(
        "database.action", action="lookup", record_type="person",
        outcome="found", result_count=len(mems),
    )
    if not mems:
        return f"No memories for {email} ({pid})"
    lines = [f"Memories for {name} <{email}> ({pid}):\n"]
    for m in mems:
        lines.append(f"[{m.id}] {m.created_at.date()}\n{m.text}\n")
    return "\n".join(lines)


async def _cmd_forget(memory_id: str) -> str:
    if not memory_id:
        return "Usage: ADMIN: forget <memory_id>"
    with get_session() as session:
        mem = session.get(Memory, memory_id)
        if not mem:
            audit_event("database.action", action="delete", record_type="memory", outcome="not_found")
            return f"Memory not found: {memory_id!r}"
        session.delete(mem)
        session.commit()
    audit_event("database.action", action="delete", record_type="memory", outcome="success")
    return f"Deleted memory {memory_id}"


async def _cmd_remember(args: str, body_text: str) -> str:
    text_content = body_text.strip()
    if not text_content:
        return "No memory text found in body."
    refs: list[str] = []
    if args.lower().startswith("refs:"):
        raw_refs = args[5:].strip()
        emails = [e.strip() for e in raw_refs.split(",") if e.strip()]
        with get_session() as session:
            for email in emails:
                p = session.exec(select(Person).where(Person.email == email)).first()
                if p:
                    refs.append(p.id)
                else:
                    return f"Person not found for ref email: {email!r}"
    vec = await embed_text(text_content)
    mem = Memory(text=text_content, embedding=vec, refs=refs)
    with get_session() as session:
        session.add(mem)
        session.flush()
        if refs:
            sanitize_memory(mem, session)
        session.commit()
        session.refresh(mem)
        mem_id = mem.id
    audit_event(
        "database.action", action="insert", record_type="memory",
        outcome="success", refs_count=len(refs),
    )
    return f"Stored memory {mem_id} (refs: {refs or 'none'})"


def _resolve_person(session: Session, ident: str) -> Person | None:
    if "@" in ident:
        return session.exec(select(Person).where(Person.email == ident)).first()
    return session.get(Person, ident)
