"""Change engine: propose → submit (Editor) → final approval (Approver) → execute.

Nothing reaches a vendor until the Approver approves. ``execute`` is private to ``final_approve``.
Approval is bound to a hash of the exact requests; every "before" snapshot is re-read just before
executing and the change becomes ``stale`` if the target changed since the preview.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import func, select, update

from app.db.repository import get_session_factory
from app.db.schema import ChangeEvent, ChangeRequest
from app.services import vendor_client
from app.services.commands import CommandError, get_command, validate_params
from app.services.commands.actions import ACTIONS, LOCAL, READERS

log = structlog.get_logger(__name__)

PROPOSED_TTL = timedelta(hours=24)
FINAL_TTL = timedelta(hours=72)
OPEN = ("proposed", "awaiting_final_approval")
_VOLATILE = re.compile(r"^(_info|last.*|updated.*|.*date|.*_at|.*At|.*Time|.*time|lastUpdated)$", re.I)


class ChangeError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def payload_hash(steps: list[dict[str, Any]]) -> str:
    core = [{k: s.get(k) for k in ("kind", "platform", "method", "path", "params", "body", "action", "args")}
            for s in steps]
    return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in sorted(value.items()) if not _VOLATILE.match(str(k))}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def _comparable(snapshot: Any, keys: list[str] | None) -> Any:
    if keys and isinstance(snapshot, dict):
        snapshot = {k: _ref(snapshot.get(k)) for k in keys}
    return _normalize(snapshot)


def _ref(value: Any) -> Any:
    """Reference objects ({id, name, _info, ...}) compare by id only."""
    if isinstance(value, dict) and "id" in value:
        return {"id": value["id"]}
    return value


async def _verify_patch(step: dict[str, Any]) -> str | None:
    """After a JSON-Patch style update, re-read the resource and confirm each replaced value stuck."""
    body, ref = step.get("body"), step.get("before_ref")
    if step.get("method") != "PATCH" or not isinstance(body, list) or not ref:
        return None
    r = await vendor_client.get(ref["platform"], ref["path"], ref.get("params") or None)
    if not r["ok"] or not isinstance(r["body"], dict):
        return "could not re-read the record to verify"
    problems = []
    for op in body:
        if not isinstance(op, dict) or op.get("op") != "replace" or "/" in str(op.get("path", "")):
            continue
        field, want = op["path"], op.get("value")
        have = r["body"].get(field)
        if field == "customFields":
            continue  # array of objects; vendor may reorder/augment — checked by the next sync
        if _ref(have) != _ref(want):
            problems.append(f"{field} is {have!r}, expected {want!r}")
    return ("vendor accepted the request but the value didn't change: " + "; ".join(problems)) if problems else None


_STEP_REF = re.compile(r"\{\{step:(\d+)\.([A-Za-z0-9_.]+)\}\}")


def _lookup(outputs: dict[int, Any], n: int, path: str) -> Any:
    value: Any = outputs.get(n)
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def resolve_refs(obj: Any, outputs: dict[int, Any]) -> Any:
    """Replace ``{{step:N.field}}`` with values from earlier steps' responses (1-based step numbers).

    A string that is exactly one reference keeps the value's type (e.g. an integer id).
    """
    if isinstance(obj, str):
        whole = _STEP_REF.fullmatch(obj)
        if whole:
            value = _lookup(outputs, int(whole.group(1)), whole.group(2))
            if value is None:
                raise ChangeError(f"Unresolved reference {obj}")
            return value

        def sub(m: re.Match[str]) -> str:
            value = _lookup(outputs, int(m.group(1)), m.group(2))
            if value is None:
                raise ChangeError(f"Unresolved reference {m.group(0)}")
            return str(value)

        return _STEP_REF.sub(sub, obj)
    if isinstance(obj, dict):
        return {k: resolve_refs(v, outputs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_refs(v, outputs) for v in obj]
    return obj


async def _run_assert(step: dict[str, Any]) -> str | None:
    """Pre-check before any write. Returns a failure description, or None when it holds."""
    r = await vendor_client.get(step["platform"], step["path"], step.get("params") or None)
    if not r["ok"]:
        return f"could not check: HTTP {r['status']}"
    body = r["body"]
    items = body.get("data", body) if isinstance(body, dict) else body
    if step.get("expect", "empty") == "empty" and items:
        names = ", ".join(str(i.get("name") or i.get("identifier") or i.get("id")) for i in items[:5]
                          if isinstance(i, dict))
        return f"{step.get('description')}: found {names or len(items)}"
    return None


async def _read_before(step: dict[str, Any]) -> tuple[bool, Any]:
    """Return (readable, current_state) for a step's before_ref."""
    ref = step.get("before_ref")
    if not ref or "{{step:" in json.dumps(ref):
        return False, None  # nothing to compare yet (target is created by an earlier step)
    if "action" in ref:
        reader = READERS.get(ref["action"])
        return (True, await reader(ref.get("args") or {})) if reader else (False, None)
    r = await vendor_client.get(ref["platform"], ref["path"], ref.get("params") or None)
    return True, (r["body"] if r["ok"] else None)


async def _event(db: Any, change_id: str, event: str, actor: str, details: dict | None = None) -> None:
    db.add(ChangeEvent(id=str(uuid4()), change_id=change_id, event=event, actor=actor, details=details, at=_now()))


def summarize(c: ChangeRequest, base_url: str = "") -> dict[str, Any]:
    return {
        "id": c.id, "number": c.number, "title": c.title, "command": c.command, "risk": c.risk,
        "status": c.status, "client_id": c.client_id, "source": c.source, "requested_by": c.requested_by,
        "submitted_by": c.submitted_by, "approved_by": c.approved_by, "rejected_reason": c.rejected_reason,
        "created_at": c.created_at, "expires_at": c.expires_at, "executed_at": c.executed_at,
        "step_count": len(c.steps or []), "warnings": c.warnings or [],
        "url": f"{base_url}/changes/{c.id}" if base_url else f"/changes/{c.id}",
    }


def detail(c: ChangeRequest, events: list[ChangeEvent], base_url: str = "") -> dict[str, Any]:
    return {**summarize(c, base_url), "params": c.params, "steps": c.steps, "results": c.results,
            "payload_hash": c.payload_hash,
            "events": [{"event": e.event, "actor": e.actor, "details": e.details, "at": e.at} for e in events]}


# ── Gate 1: propose ───────────────────────────────────────────────────────


async def propose(command: str, params: dict[str, Any], *, source: str, requested_by: str) -> ChangeRequest:
    try:
        cmd = get_command(command)
        plan = await cmd.plan(validate_params(cmd, dict(params or {})))
    except CommandError as exc:
        raise ChangeError(str(exc)) from exc
    if not plan.steps:
        raise ChangeError("Nothing to change")
    async with get_session_factory()() as db:
        number = int((await db.execute(select(func.coalesce(func.max(ChangeRequest.number), 0)))).scalar() or 0) + 1
        c = ChangeRequest(
            id=str(uuid4()), number=number, title=plan.title[:500], command=command, params=params,
            client_id=plan.client_id, risk=plan.risk if plan.risk in ("create", "update", "delete") else "update",
            steps=plan.steps, payload_hash=payload_hash(plan.steps), status="proposed", source=source,
            requested_by=requested_by, warnings=plan.warnings, created_at=_now(), expires_at=_now() + PROPOSED_TTL,
        )
        db.add(c)
        await _event(db, c.id, "proposed", requested_by, {"command": command, "source": source})
        await db.commit()
    log.info("change.proposed", number=c.number, command=command, by=requested_by, steps=len(plan.steps))
    return c


async def get(change_id: str) -> tuple[ChangeRequest, list[ChangeEvent]]:
    async with get_session_factory()() as db:
        c = await db.get(ChangeRequest, change_id)
        if not c:
            raise ChangeError("Change not found", 404)
        events = list((await db.execute(
            select(ChangeEvent).where(ChangeEvent.change_id == change_id).order_by(ChangeEvent.at))).scalars())
    return c, events


async def list_changes(status: str | None = None, limit: int = 100) -> list[ChangeRequest]:
    await expire_old()
    async with get_session_factory()() as db:
        q = select(ChangeRequest).order_by(ChangeRequest.number.desc()).limit(limit)
        if status:
            q = q.where(ChangeRequest.status.in_(status.split(",")))
        return list((await db.execute(q)).scalars())


async def expire_old() -> int:
    async with get_session_factory()() as db:
        res = await db.execute(
            update(ChangeRequest).where(ChangeRequest.status.in_(OPEN), ChangeRequest.expires_at < _now())
            .values(status="expired"), execution_options={"synchronize_session": False})
        await db.commit()
    return res.rowcount or 0


async def _transition(change_id: str, from_status: str, values: dict[str, Any]) -> bool:
    """Atomic status change: succeeds only if the row is still in ``from_status``."""
    async with get_session_factory()() as db:
        res = await db.execute(
            update(ChangeRequest).where(ChangeRequest.id == change_id, ChangeRequest.status == from_status)
            .values(**values), execution_options={"synchronize_session": False})
        await db.commit()
    return (res.rowcount or 0) == 1


async def _log(change_id: str, event: str, actor: str, details: dict | None = None) -> None:
    async with get_session_factory()() as db:
        await _event(db, change_id, event, actor, details)
        await db.commit()


def _check_open(c: ChangeRequest, expected: str) -> None:
    if _aware(c.expires_at) < _now() and c.status in OPEN:
        raise ChangeError("This change has expired — propose it again", 409)
    if c.status != expected:
        raise ChangeError(f"Change is '{c.status}', expected '{expected}'", 409)


# ── Gate 2: submit (Editor) ───────────────────────────────────────────────


async def submit(change_id: str, *, editor: str, typed_confirmation: str = "") -> ChangeRequest:
    c, _ = await get(change_id)
    _check_open(c, "proposed")
    if c.risk == "delete" and typed_confirmation.strip() != "DELETE":
        raise ChangeError("Type DELETE to confirm a change that removes or decommissions something")
    if payload_hash(c.steps or []) != c.payload_hash:
        raise ChangeError("Change content doesn't match its hash — refusing", 409)
    ok = await _transition(change_id, "proposed", {
        "status": "awaiting_final_approval", "submitted_by": editor, "submitted_at": _now(),
        "expires_at": _now() + FINAL_TTL})
    if not ok:
        raise ChangeError("Change was modified by someone else — reload", 409)
    await _log(change_id, "submitted", editor)
    log.info("change.submitted", number=c.number, by=editor)
    return (await get(change_id))[0]


# ── Gate 3: final approval (Approver) → execute ───────────────────────────


async def reject(change_id: str, *, actor: str, reason: str) -> ChangeRequest:
    if not reason.strip():
        raise ChangeError("A reason is required to reject")
    c, _ = await get(change_id)
    if c.status not in OPEN:
        raise ChangeError(f"Change is '{c.status}' and can't be rejected", 409)
    if not await _transition(change_id, c.status, {"status": "rejected", "rejected_reason": reason[:2000]}):
        raise ChangeError("Change was modified by someone else — reload", 409)
    await _log(change_id, "rejected", actor, {"reason": reason[:2000]})
    return (await get(change_id))[0]


async def cancel(change_id: str, *, actor: str) -> ChangeRequest:
    c, _ = await get(change_id)
    if c.status != "proposed":
        raise ChangeError("Only proposed (not yet submitted) changes can be cancelled", 409)
    if c.requested_by != actor:
        raise ChangeError("Only the requester can cancel this change", 403)
    await _transition(change_id, "proposed", {"status": "cancelled"})
    await _log(change_id, "cancelled", actor)
    return (await get(change_id))[0]


async def final_approve(change_id: str, *, approver: str) -> ChangeRequest:
    c, _ = await get(change_id)
    _check_open(c, "awaiting_final_approval")
    if payload_hash(c.steps or []) != c.payload_hash:
        raise ChangeError("Change content doesn't match its hash — refusing", 409)

    # Pre-checks + stale check: run every assert and re-read every before snapshot BEFORE running anything.
    stale = []
    for i, step in enumerate(c.steps or []):
        if step.get("kind") == "assert":
            failure = await _run_assert(step)
            if failure:
                stale.append({"step": i + 1, "description": failure})
            continue
        readable, current = await _read_before(step)
        if readable and _comparable(current, step.get("compare_keys")) != _comparable(step.get("before"),
                                                                                    step.get("compare_keys")):
            stale.append({"step": i + 1, "description": step.get("description")})
    if stale:
        await _transition(change_id, "awaiting_final_approval", {"status": "stale"})
        await _log(change_id, "stale", approver, {"steps": stale})
        detail = "; ".join(s["description"] or f"step {s['step']}" for s in stale)
        raise ChangeError(f"Pre-check failed or target changed since the preview — nothing was run. {detail}", 409)

    # Write mode: a read-only platform refuses even approved changes (checked before anything runs).
    from fastapi import HTTPException

    from app.auth.write_mode import enforce_change_execution

    for platform in sorted({s["platform"] for s in c.steps or [] if s.get("kind") != "assert" and s.get("platform")}):
        try:
            await enforce_change_execution(platform)
        except HTTPException as exc:
            raise ChangeError(str(exc.detail), exc.status_code) from exc

    if not await _transition(change_id, "awaiting_final_approval", {
            "status": "executing", "approved_by": approver, "approved_at": _now()}):
        raise ChangeError("Change was already approved or modified — reload", 409)
    await _log(change_id, "approved", approver)
    log.info("change.approved", number=c.number, by=approver)
    if any(s.get("slow") for s in c.steps or []):
        # Steps that wait on a vendor (e.g. ESET suspension) would outlive the request — run in the background;
        # the change page polls while the status is "executing".
        task = asyncio.create_task(_execute(change_id, approver))
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
        return (await get(change_id))[0]
    return await _execute(change_id, approver)


_BACKGROUND: set[asyncio.Task[Any]] = set()


async def _execute(change_id: str, approver: str) -> ChangeRequest:
    c, _ = await get(change_id)
    results: list[dict[str, Any]] = []
    outputs: dict[int, Any] = {}
    failed = False
    for i, raw_step in enumerate(c.steps or []):
        if failed:
            results.append({"step": i + 1, "status": "skipped"})
            continue
        if raw_step.get("kind") == "assert":
            results.append({"step": i + 1, "status": "ok", "response": "pre-check passed"})
            continue
        try:
            step = resolve_refs(raw_step, outputs)
            if step["kind"] == "http":
                r = await vendor_client.request(step["platform"], step["method"], step["path"],
                                                step.get("params") or None, step.get("body"))
            elif step["kind"] == "local":
                r = await LOCAL[step["action"]]({**(step.get("args") or {}), "_change_id": change_id})
            else:
                r = await ACTIONS[step["action"]]({**(step.get("args") or {}), "_change_id": change_id})
        except Exception as exc:  # noqa: BLE001
            step = raw_step
            r = {"ok": False, "status": 0, "body": {"error": str(exc)}}
        outputs[i + 1] = r.get("body")
        verify_error = None
        if r.get("ok") and step["kind"] == "http":
            try:
                verify_error = await _verify_patch(step)
            except Exception as exc:  # noqa: BLE001
                verify_error = f"verification error: {exc}"
        body = r.get("body")
        text = json.dumps(body, default=str)
        ok = bool(r.get("ok")) and not verify_error
        results.append({"step": i + 1, "status": "ok" if ok else "failed", "http_status": r.get("status"),
                        **({"error": verify_error} if verify_error else {}),
                        "response": body if len(text) < 20000 else text[:20000] + "…"})
        failed = not ok
    status = "failed" if failed else "done"
    async with get_session_factory()() as db:
        await db.execute(update(ChangeRequest).where(ChangeRequest.id == change_id)
                         .values(status=status, results=results, executed_at=_now()),
                         execution_options={"synchronize_session": False})
        await _event(db, change_id, "executed" if not failed else "failed", approver,
                     {"ok": sum(1 for r in results if r["status"] == "ok"), "steps": len(results)})
        await db.commit()
    log.info("change.executed", number=c.number, status=status)
    return (await get(change_id))[0]
