"""Agent API — natural-language request endpoint with workflow learning.

Stages
------
**Stage 3 (fast path):**
    Look up an active workflow for the classified intent. If found and the
    parameters validate, replay it directly. ~300ms typical.

**Stage 1 (smart path):**
    No saved workflow — fall back to the planner's pre-canned plan for
    that intent. Execute the plan.

**Stage 2 (learn):**
    On a *successful* Stage 1 run for a previously unknown intent, save
    the plan as a new ``AgentWorkflow``. Next call hits Stage 3.

Routes
------
``POST /api/agent/request``
    Body: ``{text, parameters?, dry_run?, approved?, intent?}``
    Returns the workflow execution result + which stage handled it.

``GET  /api/agent/workflows``                    list active workflows
``GET  /api/agent/workflows/{id}``               workflow detail
``GET  /api/agent/workflows/intent/{intent}``    versions for an intent
``GET  /api/agent/requests``                     recent NL audit log
``POST /api/agent/workflows/{id}/replay``        re-run with new params
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.companies import _get_session
from app.db.agent_repository import (
    AgentRequestRepository,
    AgentWorkflowRepository,
    WorkflowExecutionRepository,
)
from app.services.agent_planner import (
    classify_intent,
    extract_parameters,
    get_base_plan,
    known_intents,
)
from app.services.workflow_executor import execute_plan

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])


# ── Request / response models ────────────────────────────────────


class AgentRequestBody(BaseModel):
    text: str = Field(..., description="Natural-language request")
    intent: str | None = Field(None, description="Override: skip classification")
    parameters: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = Field(False, description="Plan without executing")
    approved: bool = Field(False, description="Authorise destructive steps")
    user_id: str = Field("anonymous")


class AgentReplyResponse(BaseModel):
    request_id: str
    intent: str
    stage: str            # "stage1" | "stage3" | "unknown"
    workflow_id: str | None
    workflow_version: int | None
    outcome: str          # "success" | "partial" | "failure"
    duration_ms: int
    parameters: dict[str, Any]
    steps: list[dict[str, Any]]
    auto_saved_as_workflow: bool = False


# ── Main endpoint ────────────────────────────────────────────────


@router.post("/request", response_model=AgentReplyResponse,
             summary="Submit a natural-language request to the agent")
async def agent_request(
    body: AgentRequestBody,
    session: AsyncSession = Depends(_get_session),
) -> AgentReplyResponse:
    """Process an NL request through Stage 3 → Stage 1 → Stage 2 (learn)."""
    req_repo = AgentRequestRepository(session)
    wf_repo = AgentWorkflowRepository(session)
    ex_repo = WorkflowExecutionRepository(session)

    # ── Classify ──
    intent = body.intent or classify_intent(body.text)
    extracted = extract_parameters(body.text, intent) if not body.intent else {}
    # Explicit parameters override extracted ones
    parameters = {**extracted, **body.parameters}

    # ── Audit row up front ──
    req = await req_repo.create(
        raw_text=body.text,
        user_id=body.user_id,
        intent=intent,
        parameters=parameters,
    )

    if intent == "unknown":
        await req_repo.update_outcome(
            req.id, outcome="failure", stage="unknown",
            workflow_id=None, duration_ms=0,
            error=f"could not classify intent. Known: {known_intents()}",
        )
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unknown_intent",
                "message": "Could not classify the request. Pass `intent` explicitly or rephrase.",
                "known_intents": known_intents(),
            },
        )

    # ── Stage 3: cached workflow? ──
    workflow = await wf_repo.get_active_by_intent(intent)
    stage = "stage3" if workflow else "stage1"
    auto_saved = False

    if workflow is None:
        # Stage 1: use the planner's base plan
        plan = get_base_plan(intent)
        if plan is None:
            await req_repo.update_outcome(
                req.id, outcome="failure", stage="unknown",
                workflow_id=None, duration_ms=0,
                error=f"no plan for intent '{intent}'",
            )
            raise HTTPException(status_code=404, detail=f"No plan for intent '{intent}'")

        steps = plan["steps"]
    else:
        steps = workflow.steps  # type: ignore[assignment]

    # ── Execute ──
    execution = await ex_repo.create(
        workflow_id=workflow.id if workflow else "stage1-adhoc",
        request_id=req.id,
        parameters=parameters,
        dry_run=body.dry_run,
    )

    outcome, step_results, duration_ms = await execute_plan(
        steps, parameters, approved=body.approved, dry_run=body.dry_run,
    )

    await ex_repo.finalise(
        execution.id,
        outcome=outcome,
        step_results=step_results,
        duration_ms=duration_ms,
        error="" if outcome == "success" else "; ".join(
            r.get("error", "") for r in step_results if r.get("error")
        ),
    )

    workflow_id_used: str | None = workflow.id if workflow else None
    workflow_version: int | None = workflow.version if workflow else None

    # ── Stage 2: learn (save plan as workflow on first successful run) ──
    if workflow is None and outcome == "success" and not body.dry_run:
        plan = get_base_plan(intent)  # re-lookup for metadata
        assert plan is not None
        new_wf = await wf_repo.create(
            intent=intent,
            name=plan["name"],
            description=plan["description"],
            steps=plan["steps"],
            parameters_schema=plan.get("parameters_schema"),
            auto_generated=True,
        )
        workflow_id_used = new_wf.id
        workflow_version = new_wf.version
        auto_saved = True
        log.info("agent.workflow.auto_saved", intent=intent, workflow_id=new_wf.id)

    # ── Update workflow stats and audit log ──
    if workflow_id_used and workflow_id_used != "stage1-adhoc":
        await wf_repo.record_execution(
            workflow_id_used,
            success=(outcome == "success"),
            duration_ms=duration_ms,
        )

    summary = _summarise_outcome(intent, parameters, outcome, step_results)
    await req_repo.update_outcome(
        req.id,
        outcome=outcome,
        stage=stage,
        workflow_id=workflow_id_used,
        duration_ms=duration_ms,
        result_summary=summary,
    )

    return AgentReplyResponse(
        request_id=req.id,
        intent=intent,
        stage=stage,
        workflow_id=workflow_id_used,
        workflow_version=workflow_version,
        outcome=outcome,
        duration_ms=duration_ms,
        parameters=parameters,
        steps=step_results,
        auto_saved_as_workflow=auto_saved,
    )


def _summarise_outcome(
    intent: str, params: dict[str, Any], outcome: str, results: list[dict[str, Any]]
) -> str:
    """Build a one-line human-readable summary for the audit log."""
    target = params.get("company") or params.get("device") or ""
    bits = [f"{intent}={outcome}"]
    if target:
        bits.append(f"target={target}")
    bits.append(f"steps={len(results)}")
    return " ".join(bits)


# ── Inspection / management endpoints ────────────────────────────


@router.get("/workflows", summary="List all active workflows")
async def list_workflows(session: AsyncSession = Depends(_get_session)) -> list[dict[str, Any]]:
    repo = AgentWorkflowRepository(session)
    workflows = await repo.list_active()
    return [
        {
            "id": w.id,
            "intent": w.intent,
            "name": w.name,
            "description": w.description,
            "version": w.version,
            "auto_generated": w.auto_generated,
            "step_count": len(w.steps or []),
            "success_count": w.success_count,
            "failure_count": w.failure_count,
            "avg_duration_ms": w.avg_duration_ms,
            "last_used_at": w.last_used_at.isoformat() if w.last_used_at else None,
        }
        for w in workflows
    ]


@router.get("/workflows/{workflow_id}", summary="Workflow detail")
async def get_workflow(
    workflow_id: str, session: AsyncSession = Depends(_get_session)
) -> dict[str, Any]:
    repo = AgentWorkflowRepository(session)
    w = await repo.get_by_id(workflow_id)
    if w is None:
        raise HTTPException(404, "Workflow not found")
    return {
        "id": w.id, "intent": w.intent, "name": w.name, "description": w.description,
        "version": w.version, "active": w.active, "auto_generated": w.auto_generated,
        "steps": w.steps, "parameters_schema": w.parameters_schema,
        "success_count": w.success_count, "failure_count": w.failure_count,
        "avg_duration_ms": w.avg_duration_ms,
        "created_at": w.created_at.isoformat(),
        "last_used_at": w.last_used_at.isoformat() if w.last_used_at else None,
    }


@router.get("/workflows/intent/{intent}", summary="All versions of a workflow")
async def list_versions(
    intent: str, session: AsyncSession = Depends(_get_session)
) -> list[dict[str, Any]]:
    repo = AgentWorkflowRepository(session)
    versions = await repo.list_versions(intent)
    return [
        {
            "id": w.id, "version": w.version, "active": w.active,
            "auto_generated": w.auto_generated,
            "success_count": w.success_count, "failure_count": w.failure_count,
            "created_at": w.created_at.isoformat(),
        }
        for w in versions
    ]


@router.post("/workflows/{workflow_id}/replay", summary="Re-run a workflow with parameters")
async def replay_workflow(
    workflow_id: str,
    parameters: dict[str, Any],
    dry_run: bool = False,
    approved: bool = False,
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    wf_repo = AgentWorkflowRepository(session)
    ex_repo = WorkflowExecutionRepository(session)
    w = await wf_repo.get_by_id(workflow_id)
    if w is None:
        raise HTTPException(404, "Workflow not found")

    execution = await ex_repo.create(
        workflow_id=w.id, request_id=None,
        parameters=parameters, dry_run=dry_run,
    )
    outcome, results, duration_ms = await execute_plan(
        w.steps, parameters, approved=approved, dry_run=dry_run,  # type: ignore[arg-type]
    )
    await ex_repo.finalise(
        execution.id, outcome=outcome, step_results=results,
        duration_ms=duration_ms,
    )
    await wf_repo.record_execution(
        w.id, success=(outcome == "success"), duration_ms=duration_ms,
    )
    return {
        "execution_id": execution.id, "workflow_id": w.id, "intent": w.intent,
        "outcome": outcome, "duration_ms": duration_ms, "steps": results,
    }


@router.get("/requests", summary="Recent NL request audit log")
async def list_requests(
    limit: int = 50, session: AsyncSession = Depends(_get_session)
) -> list[dict[str, Any]]:
    repo = AgentRequestRepository(session)
    reqs = await repo.list_recent(limit=limit)
    return [
        {
            "id": r.id, "received_at": r.received_at.isoformat(),
            "user_id": r.user_id, "raw_text": r.raw_text[:200],
            "intent": r.intent, "stage": r.stage, "outcome": r.outcome,
            "workflow_id": r.workflow_id, "duration_ms": r.duration_ms,
            "result_summary": r.result_summary,
        }
        for r in reqs
    ]


@router.get("/intents", summary="Known intents the planner can handle")
async def list_intents() -> dict[str, Any]:
    return {"intents": known_intents()}
