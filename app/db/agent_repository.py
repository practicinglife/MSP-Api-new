"""Repositories for the agent / workflow / fingerprint subsystems.

Kept separate from the main repository module so the agent stack can
evolve independently. All methods are async and use SQLAlchemy 2.x.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.schema import (
    AgentRequest,
    AgentWorkflow,
    PlatformFingerprint,
    WorkflowExecution,
)

log = structlog.get_logger(__name__)


# ── AgentWorkflow ────────────────────────────────────────────────


class AgentWorkflowRepository:
    """CRUD + intent lookup for reusable agent workflows."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_active_by_intent(self, intent: str) -> AgentWorkflow | None:
        """Return the currently active workflow for an intent (or None)."""
        stmt = (
            select(AgentWorkflow)
            .where(AgentWorkflow.intent == intent, AgentWorkflow.active.is_(True))
            .order_by(AgentWorkflow.version.desc())
            .limit(1)
        )
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        intent: str,
        name: str,
        description: str,
        steps: list[dict[str, Any]],
        parameters_schema: dict[str, Any] | None = None,
        auto_generated: bool = False,
    ) -> AgentWorkflow:
        """Create a new workflow (v1) or bump version if intent already exists."""
        existing = await self.get_active_by_intent(intent)
        version = 1
        if existing is not None:
            # Deactivate prior version
            existing.active = False
            version = existing.version + 1

        wf = AgentWorkflow(
            id=str(uuid4()),
            intent=intent,
            name=name or intent,
            description=description,
            version=version,
            active=True,
            auto_generated=auto_generated,
            steps=steps,
            parameters_schema=parameters_schema or {},
        )
        self._s.add(wf)
        await self._s.commit()
        await self._s.refresh(wf)
        log.info(
            "workflow.created",
            intent=intent,
            version=version,
            steps=len(steps),
            auto=auto_generated,
        )
        return wf

    async def list_active(self) -> list[AgentWorkflow]:
        stmt = (
            select(AgentWorkflow)
            .where(AgentWorkflow.active.is_(True))
            .order_by(AgentWorkflow.intent)
        )
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def list_versions(self, intent: str) -> list[AgentWorkflow]:
        stmt = (
            select(AgentWorkflow)
            .where(AgentWorkflow.intent == intent)
            .order_by(AgentWorkflow.version.desc())
        )
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, workflow_id: str) -> AgentWorkflow | None:
        return await self._s.get(AgentWorkflow, workflow_id)

    async def record_execution(
        self,
        workflow_id: str,
        *,
        success: bool,
        duration_ms: int,
    ) -> None:
        """Update success/failure counters and rolling average duration."""
        wf = await self._s.get(AgentWorkflow, workflow_id)
        if wf is None:
            return
        if success:
            wf.success_count += 1
        else:
            wf.failure_count += 1
        # rolling average over total runs
        total = wf.success_count + wf.failure_count
        if total > 0:
            wf.avg_duration_ms = int(
                ((wf.avg_duration_ms * (total - 1)) + duration_ms) / total
            )
        wf.last_used_at = datetime.now(UTC)
        await self._s.commit()


# ── AgentRequest (audit log) ─────────────────────────────────────


class AgentRequestRepository:
    """Audit log for every NL request received by the agent API."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        *,
        raw_text: str,
        user_id: str = "anonymous",
        intent: str = "",
        parameters: dict[str, Any] | None = None,
    ) -> AgentRequest:
        req = AgentRequest(
            id=str(uuid4()),
            raw_text=raw_text,
            user_id=user_id,
            intent=intent,
            parameters=parameters or {},
        )
        self._s.add(req)
        await self._s.commit()
        await self._s.refresh(req)
        return req

    async def update_outcome(
        self,
        request_id: str,
        *,
        outcome: str,
        stage: str,
        workflow_id: str | None,
        duration_ms: int,
        result_summary: str = "",
        error: str = "",
    ) -> None:
        req = await self._s.get(AgentRequest, request_id)
        if req is None:
            return
        req.outcome = outcome
        req.stage = stage
        req.workflow_id = workflow_id
        req.duration_ms = duration_ms
        req.result_summary = result_summary[:2000]
        req.error = error[:2000]
        await self._s.commit()

    async def list_recent(self, limit: int = 50) -> list[AgentRequest]:
        stmt = (
            select(AgentRequest)
            .order_by(AgentRequest.received_at.desc())
            .limit(limit)
        )
        result = await self._s.execute(stmt)
        return list(result.scalars().all())


# ── WorkflowExecution (per-run history) ──────────────────────────


class WorkflowExecutionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        *,
        workflow_id: str,
        request_id: str | None,
        parameters: dict[str, Any],
        dry_run: bool = False,
    ) -> WorkflowExecution:
        ex = WorkflowExecution(
            id=str(uuid4()),
            workflow_id=workflow_id,
            request_id=request_id,
            parameters=parameters,
            dry_run=dry_run,
        )
        self._s.add(ex)
        await self._s.commit()
        await self._s.refresh(ex)
        return ex

    async def finalise(
        self,
        execution_id: str,
        *,
        outcome: str,
        step_results: list[dict[str, Any]],
        duration_ms: int,
        error: str = "",
    ) -> None:
        ex = await self._s.get(WorkflowExecution, execution_id)
        if ex is None:
            return
        ex.outcome = outcome
        ex.step_results = step_results
        ex.duration_ms = duration_ms
        ex.finished_at = datetime.now(UTC)
        ex.error = error[:2000]
        await self._s.commit()

    async def list_for_workflow(
        self, workflow_id: str, limit: int = 50
    ) -> list[WorkflowExecution]:
        stmt = (
            select(WorkflowExecution)
            .where(WorkflowExecution.workflow_id == workflow_id)
            .order_by(WorkflowExecution.started_at.desc())
            .limit(limit)
        )
        result = await self._s.execute(stmt)
        return list(result.scalars().all())


# ── PlatformFingerprint (Layer 2 delta refresh) ──────────────────


class PlatformFingerprintRepository:
    """Stores per-company per-platform device fingerprints for delta detection."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, platform: str, company: str) -> PlatformFingerprint | None:
        stmt = select(PlatformFingerprint).where(
            PlatformFingerprint.platform == platform,
            PlatformFingerprint.company_canonical == company,
        )
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert(
        self,
        *,
        platform: str,
        company: str,
        device_count: int,
        devices_hash: str,
    ) -> tuple[PlatformFingerprint, bool]:
        """Insert or update a fingerprint row.

        Returns (row, changed) — ``changed`` is True if hash differs from the
        previously stored value (or this is a brand-new row).
        """
        existing = await self.get(platform, company)
        now = datetime.now(UTC)
        if existing is None:
            row = PlatformFingerprint(
                id=str(uuid4()),
                platform=platform,
                company_canonical=company,
                device_count=device_count,
                devices_hash=devices_hash,
                last_checked_at=now,
                last_changed_at=now,
            )
            self._s.add(row)
            await self._s.commit()
            await self._s.refresh(row)
            return row, True

        changed = existing.devices_hash != devices_hash
        existing.device_count = device_count
        existing.devices_hash = devices_hash
        existing.last_checked_at = now
        if changed:
            existing.last_changed_at = now
        await self._s.commit()
        return existing, changed

    async def list_all(self) -> list[PlatformFingerprint]:
        stmt = select(PlatformFingerprint).order_by(
            PlatformFingerprint.platform,
            PlatformFingerprint.company_canonical,
        )
        result = await self._s.execute(stmt)
        return list(result.scalars().all())
