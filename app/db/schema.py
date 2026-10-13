"""SQLAlchemy 2.0 async ORM table definitions."""

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.auth.crypto import EncryptedJSON


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""

    pass


class SyncRun(Base):
    """Record of a single reconciliation run for audit and visibility."""

    __tablename__ = "sync_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    company_match_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    device_match_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warnings: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]


class ConnectorEvent(Base):
    """Per-connector event recorded during a sync run."""

    __tablename__ = "connector_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    connector: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    company_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    device_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CompanyOverride(Base):
    """Manual override linking two company platform IDs."""

    __tablename__ = "company_overrides"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform_a: Mapped[str] = mapped_column(String(64), nullable=False)
    platform_id_a: Mapped[str] = mapped_column(String(256), nullable=False)
    platform_b: Mapped[str] = mapped_column(String(64), nullable=False)
    platform_id_b: Mapped[str] = mapped_column(String(256), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class DeviceOverride(Base):
    """Manual override linking two device platform IDs."""

    __tablename__ = "device_overrides"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform_a: Mapped[str] = mapped_column(String(64), nullable=False)
    platform_id_a: Mapped[str] = mapped_column(String(256), nullable=False)
    platform_b: Mapped[str] = mapped_column(String(64), nullable=False)
    platform_id_b: Mapped[str] = mapped_column(String(256), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CompanyMatch(Base):
    """Persisted result of a company reconciliation match."""

    __tablename__ = "company_matches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    platform_a: Mapped[str] = mapped_column(String(64), nullable=False)
    platform_id_a: Mapped[str] = mapped_column(String(256), nullable=False)
    name_a: Mapped[str] = mapped_column(String(512), nullable=False)
    platform_b: Mapped[str | None] = mapped_column(String(64), nullable=True)
    platform_id_b: Mapped[str | None] = mapped_column(String(256), nullable=True)
    name_b: Mapped[str | None] = mapped_column(String(512), nullable=True)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class DeviceMatch(Base):
    """Persisted result of a device reconciliation match."""

    __tablename__ = "device_matches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    platform_a: Mapped[str] = mapped_column(String(64), nullable=False)
    platform_id_a: Mapped[str] = mapped_column(String(256), nullable=False)
    hostname_a: Mapped[str] = mapped_column(String(512), nullable=False)
    platform_b: Mapped[str | None] = mapped_column(String(64), nullable=True)
    platform_id_b: Mapped[str | None] = mapped_column(String(256), nullable=True)
    hostname_b: Mapped[str | None] = mapped_column(String(512), nullable=True)
    score: Mapped[int] = mapped_column(nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reasons: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ConnectorConfig(Base):
    """Persisted connector configuration — credentials stored AES-GCM encrypted (MSP_MASTER_KEY)."""

    __tablename__ = "connector_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    credentials: Mapped[dict] = mapped_column(EncryptedJSON, nullable=False)  # type: ignore[type-arg]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class CompanyAlias(Base):
    """Manual alias mapping a normalised company name to a canonical name.

    Used to merge companies that differ due to truncation or naming
    variations across platforms (e.g. "acme plumbing and heatin"
    → "acme plumbing and heating").
    """

    __tablename__ = "company_aliases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    alias_name: Mapped[str] = mapped_column(String(512), nullable=False, unique=True, index=True)
    canonical_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "manual" (a person linked them) or "auto" (company_linker found the evidence; note says what it was)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual", server_default="manual")
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CompanyMatchDecision(Base):
    """A person's answer about a company name, so it is never suggested again.

    ``decision``: ``different`` — ``name`` is not the same client as ``target``;
    ``not_managed`` — ``name`` is a contact-only / one-off company, not expected in every tool.
    """

    __tablename__ = "company_match_decisions"
    __table_args__ = (UniqueConstraint("name", "target", "decision", name="uq_company_match_decision"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    target: Mapped[str] = mapped_column(String(512), nullable=False, default="", server_default="")
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(256), nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ReconciledDevice(Base):
    """A single reconciled device record produced by cross-platform matching.

    Each row represents one physical device identified by hostname + endpoint_id.
    The ``platforms`` JSON list records which platforms reported the device
    (e.g. ``["asio", "connectwise_psa", "screenconnect"]``).
    ``match_type`` is "full" (all 3), "partial" (2 platforms), or "unmatched" (1).
    """

    __tablename__ = "reconciled_devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    site_name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    hostname: Mapped[str] = mapped_column(String(512), nullable=False)
    endpoint_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    mac_address: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    serial_number: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    platforms: Mapped[dict] = mapped_column(JSON, nullable=False)  # type: ignore[type-arg]
    match_type: Mapped[str] = mapped_column(String(32), nullable=False, default="unmatched")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ApiSpec(Base):
    """Stored OpenAPI / Swagger specification for a platform API."""

    __tablename__ = "api_specs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    swagger_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    spec_json: Mapped[dict] = mapped_column(JSON, nullable=False)  # type: ignore[type-arg]
    endpoint_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ApiEndpoint(Base):
    """Individual endpoint extracted from an OpenAPI spec."""

    __tablename__ = "api_endpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[dict] = mapped_column(JSON, nullable=False, default=list)  # type: ignore[type-arg]
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False, default=list)  # type: ignore[type-arg]
    request_body_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    response_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ApiResponseCache(Base):
    """Cached API response for a specific endpoint call — long TTL for static data."""

    __tablename__ = "api_response_cache"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    endpoint_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    request_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    status_code: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    response_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    response_headers: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    cached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlatformDevice(Base):
    """Persisted device record from a platform scan.

    Each row represents one device as reported by a single platform.
    ``endpoint_id`` is the golden key for ASIO ↔ PSA matching;
    hostname is used as fallback for ScreenConnect / SentinelOne.
    The table is bulk-upserted on every scan; ``last_seen_at`` tracks freshness.
    """

    __tablename__ = "platform_devices"
    __table_args__ = (
        UniqueConstraint("platform", "platform_id", name="uq_platform_device"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    platform_id: Mapped[str] = mapped_column(String(256), nullable=False)
    endpoint_id: Mapped[str] = mapped_column(String(256), nullable=False, default="", index=True)
    hostname: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    hostname_norm: Mapped[str] = mapped_column(String(512), nullable=False, default="", index=True)
    company_raw: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    company_canonical: Mapped[str] = mapped_column(String(512), nullable=False, default="", index=True)
    company_platform_id: Mapped[str] = mapped_column(String(256), nullable=False, default="", index=True)
    # Registry client (via confirmed client_platform_links) — set by reconciliation; None = unlinked company
    client_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    site_name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    serial_number: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    mac_addresses: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CompanyMapping(Base):
    """Resolved mapping from a platform-specific company name to canonical name.

    Built from normalisation + alias resolution + manual overrides.
    One row per unique (platform, raw_name).  Used as the lookup table
    so reconciliation reads from DB instead of recomputing every time.
    """

    __tablename__ = "company_mappings"
    __table_args__ = (
        UniqueConstraint("platform", "raw_name", name="uq_company_mapping"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    raw_name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    alias_source: Mapped[str] = mapped_column(String(64), nullable=False, default="auto")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class DeviceAvStatus(Base):
    """Per-device antivirus detection result from any source.

    Multiple sources can contribute AV status for the same device:
    - ``screenconnect_command``: PowerShell detection script run via SC
    - ``sentinelone_apps``: S1 installed-applications API cross-reference
    - ``eset_protect``: ESET PROTECT Cloud API (future)
    - ``psa_agreement``: Inferred from PSA agreement AV assignment
    - ``manual``: Manually reported / CSV upload

    Each row is one (hostname, av_vendor) pair.  A device with ESET + Defender
    would have two rows.  ``detection_source`` tracks provenance.
    """

    __tablename__ = "device_av_status"
    __table_args__ = (
        UniqueConstraint(
            "hostname_norm", "av_vendor", "detection_source",
            name="uq_device_av_vendor_source",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(512), nullable=False)
    hostname_norm: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    endpoint_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    company_canonical: Mapped[str] = mapped_column(String(512), nullable=False, default="", index=True)
    av_vendor: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    av_product: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    av_version: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    av_status: Mapped[str] = mapped_column(String(64), nullable=False, default="installed")
    av_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    av_up_to_date: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    detection_source: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

# -- Breakdown cache: pre-computed enriched reconciliation output ----


class BreakdownCompany(Base):
    """Per-company summary row from the last completed reconciliation.

    Populated by ``run_fast_reconciliation()`` after every reconcile run.
    ``GET /api/companies/reconciled`` reads directly from this table so
    it never recomputes or calls live APIs.
    """

    __tablename__ = "breakdown_companies"

    company_name: Mapped[str] = mapped_column(String(512), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)  # type: ignore[type-arg]
    # Top-level fields duplicated for cheap SQL filtering/ordering
    matched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mismatched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    assigned_av: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    psa_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class BreakdownSnapshot(Base):
    """Point-in-time summary of reconciliation counts for change tracking.

    One row per reconcile run.  ``changes`` captures the diff vs the
    previous snapshot: companies/devices added, removed, or changed.
    ``company_names`` is the sorted list of canonical company names at
    snapshot time, used for diff computation.
    """

    __tablename__ = "breakdown_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    company_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mismatched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_unique: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    company_names: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # sorted list  # type: ignore[type-arg]
    changes: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]


class AvAssignmentCache(Base):
    """PSA AV-assignment per company, refreshed on each full/smart reconcile.

    Stores the result of ``psa.get_company_av_map()`` so that
    ``run_fast_reconciliation()`` can enrich the breakdown without live
    API calls.
    """

    __tablename__ = "av_assignment_cache"

    company_name: Mapped[str] = mapped_column(String(512), primary_key=True)
    assigned_av: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AsioLastContactImport(Base):
    """CSV-provided Asio last-contact timestamps.

    Used as a manual fallback when Asio device/asset API payloads do not expose
    a last-online field for cleanup decisions.
    """

    __tablename__ = "asio_last_contact_imports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    endpoint_id: Mapped[str] = mapped_column(String(256), nullable=False, default="", index=True)
    platform_id: Mapped[str] = mapped_column(String(256), nullable=False, default="", index=True)
    hostname: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    hostname_norm: Mapped[str] = mapped_column(String(512), nullable=False, default="", index=True)
    company_name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    site_name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    last_contact: Mapped[str] = mapped_column(String(128), nullable=False)
    source_filename: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# -- Layer 2: Delta refresh fingerprints ----------------------------


class PlatformFingerprint(Base):
    """Per-company per-platform device fingerprint for delta detection.

    Stores a count + hash of sorted device IDs so the next scan can
    determine *cheaply* whether anything changed for that company on
    that platform. If the fingerprint matches, the platform's data is
    skipped entirely. If it differs, only the new/changed devices need
    detail lookups.
    """

    __tablename__ = "platform_fingerprints"
    __table_args__ = (
        UniqueConstraint("platform", "company_canonical", name="uq_fingerprint_platform_company"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    company_canonical: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    device_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    devices_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# -- Layer 3: Agent request memory & workflow learning --------------


class AgentWorkflow(Base):
    """A reusable, named execution plan that an agent (or human) created.

    Workflows map an *intent* (e.g. `company_report`) to an ordered list
    of steps (proxy/catalog endpoint calls) that fulfil it. Workflows are
    versioned: editing creates a new row with `version+1` and deactivates
    the prior row. The `auto_generated` flag distinguishes plans that the
    LLM produced (Stage 2 learning) from hand-authored ones.
    """

    __tablename__ = "agent_workflows"
    __table_args__ = (
        UniqueConstraint("intent", "version", name="uq_workflow_intent_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    intent: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    auto_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    steps: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # type: ignore[type-arg]
    parameters_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentRequest(Base):
    """Audit log of every natural-language request received by the agent API."""

    __tablename__ = "agent_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, default="anonymous")
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # type: ignore[type-arg]
    workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    stage: Mapped[str] = mapped_column(String(16), nullable=False, default="stage1")
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")


class WorkflowExecution(Base):
    """A single execution instance of a workflow (one entry per run)."""

    __tablename__ = "workflow_executions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # type: ignore[type-arg]
    step_results: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # type: ignore[type-arg]
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class DailySnapshot(Base):
    """Daily per-company device breakdown snapshot — mirrors the UI breakdown page.

    One row per (snapshot_date, company_canonical).
    Taken automatically at the end of each reconciliation run.
    Stores matched/unmatched counts + per-platform counts so you can
    answer: "What changed between Thursday and today?"
    """

    __tablename__ = "daily_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_date", "company_canonical", name="uq_snapshot_company_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    snapshot_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    company_canonical: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # Breakdown counts — same as what the breakdown page shows
    total_devices: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unmatched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Per-platform counts
    asio_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    psa_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sc_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    s1_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# ── Client master (client naming standard) ───────────────────────────────────────────


class Client(Base):
    """One managed customer. ``id`` is the permanent primary key every tool links to.

    Names are attributes, not keys — renaming a client never breaks its links.
    """

    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_code: Mapped[str | None] = mapped_column(String(8), nullable=True, unique=True)
    standard_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    legal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    psa_company_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    av_vendor: Mapped[str | None] = mapped_column(String(32), nullable=True)  # sentinelone | eset
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    naming_status: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed")  # proposed | approved
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PlatformCompany(Base):
    """Local copy of every company/site/CP1 value seen in each platform.

    Refreshed on demand by ``POST /api/clients/sync``; read from DB all day.
    ``platform_company_id`` is the platform's own ID (ScreenConnect has none, so the
    exact CustomProperty1 text is used).
    """

    __tablename__ = "platform_companies"
    __table_args__ = (
        UniqueConstraint("platform", "platform_company_id", name="uq_platform_company"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    platform_company_id: Mapped[str] = mapped_column(String(512), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    name_norm: Mapped[str] = mapped_column(String(512), nullable=False, default="", index=True)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ApiKey(Base):
    """Connection key for external access (Claude connectors, Copilot, etc.). Only a hash is stored."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)  # first chars, for identification
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Authorization. NULL (keys created before scopes existed) = read-only everywhere.
    roles: Mapped[list | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    scopes: Mapped[list | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]  # "<platform|*>:<read|write>"
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class OAuthClient(Base):
    """OAuth client registered by Claude via Dynamic Client Registration (public client, PKCE)."""

    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    redirect_uris: Mapped[list | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class OAuthToken(Base):
    """Authorization codes, access tokens and refresh tokens (hashed), tied to the API key that approved them."""

    __tablename__ = "oauth_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # code | access | refresh
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    api_key_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # key id or "m365:<oid>"
    redirect_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_challenge: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Set when the token was issued through Microsoft 365 sign-in (api_key_id is then "m365:<oid>")
    user_upn: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_roles: Mapped[list | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    # RFC 8707 audience: the MCP server URL this token is valid for (NULL on pre-binding tokens = /msp/mcp).
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuthFlow(Base):
    """In-flight Microsoft 365 sign-in (state/nonce/PKCE verifier). Short-lived."""

    __tablename__ = "auth_flows"

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="session")  # session | oauth
    next_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_request: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserSession(Base):
    """Signed-in Microsoft 365 user (browser session for edits / approvals)."""

    __tablename__ = "user_sessions"

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    upn: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    oid: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    roles: Mapped[list | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    csrf: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LocalUser(Base):
    """Break-glass / no-IdP account: scrypt password + mandatory TOTP. Created with ``python -m app.cli``."""

    __tablename__ = "local_users"

    username: Mapped[str] = mapped_column(String(128), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    totp_secret: Mapped[str] = mapped_column(String(64), nullable=False)
    roles: Mapped[list | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_totp_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # replay protection
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlatformPolicy(Base):
    """Per-platform write mode: read_only | direct | approval (see app/auth/write_mode.py)."""

    __tablename__ = "platform_policies"

    platform: Mapped[str] = mapped_column(String(64), primary_key=True)
    write_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="read_only")
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class AuditLog(Base):
    """Who did what: every write, every vendor call path and every MCP tool call."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    principal: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    auth_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # session | api_key | oauth | internal | none
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # ui | api | mcp | internal
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    action: Mapped[str | None] = mapped_column(String(128), nullable=True)  # e.g. MCP tool name
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]


class ChangeRequest(Base):
    """A proposed vendor write. Executes only after an Editor submits it and an Approver approves it."""

    __tablename__ = "change_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    command: Mapped[str] = mapped_column(String(128), nullable=False)
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    client_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    risk: Mapped[str] = mapped_column(String(16), nullable=False, default="update")  # create|update|delete
    steps: Mapped[list | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed", index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="api")  # mcp|dashboard|playbook|api
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    submitted_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    results: Mapped[list | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    warnings: Mapped[list | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ChangeEvent(Base):
    """Audit trail for change requests — one row per gate/action."""

    __tablename__ = "change_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    change_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ClientPlatformLink(Base):
    """Links a client to one platform company by that platform's ID.

    status: confirmed (used for matching) | suggested (needs review) | rejected (never re-suggest).
    """

    __tablename__ = "client_platform_links"
    __table_args__ = (
        UniqueConstraint("platform", "platform_company_id", "client_id", name="uq_client_platform_link"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    platform_company_id: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="suggested")
    link_method: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AsioEndpointAv(Base):
    """What antivirus Asio detects on each endpoint — written by the AV sweep (one row per Asio endpoint).

    A row with ``vendors == []`` means Asio was asked and reported no antivirus (vs. no row = not checked yet).
    """

    __tablename__ = "asio_endpoint_av"

    endpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    company_platform_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    hostname: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    host_key: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    products: Mapped[list | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    vendors: Mapped[list | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    modified_at: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    agent_installed_at: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    last_contact: Mapped[str] = mapped_column(String(40), nullable=False, default="")  # Asio heartbeat
    online: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    os_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    is_server: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Detected server roles that should NOT run antivirus: hyperv | sql | dc
    roles: Mapped[list | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    swept_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AlertCompanyAlias(Base):
    """A customer/site name from an alert email (Avast customer, Instant On site) confirmed to be a PSA company."""

    __tablename__ = "alert_company_aliases"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)  # "<source>|<normalised name>"
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    psa_company_id: Mapped[int] = mapped_column(Integer, nullable=False)
    company_name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    set_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MachineRoleOverride(Base):
    """Manual correction of a machine's no-antivirus server role (wins over detection).

    role: hyperv | sql | dc | none ("none" = not an excluded server, even if detected as one).
    """

    __tablename__ = "machine_role_overrides"

    endpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    hostname: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    set_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    note: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AvReviewItem(Base):
    """A machine flagged "No Antivirus - Needs Review" (Asio custom field) and what happened to it.

    status: flagged → deploy_sent → fixed (AV seen, flag cleared) | not_fixed (still missing after 2 sweeps).
    Only machines listed here, under an approved change, are ever unflagged automatically.
    """

    __tablename__ = "av_review_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    endpoint_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    client_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    hostname: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    expected_av: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="flagged", index=True)
    change_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    checks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    flagged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ── Any-API connections (Phase 2) ─────────────────────────────────────────────────────────────────────


class Connection(Base):
    """One connected API or data source, of any kind (see ``app/connections/``).

    ``slug`` is the stable handle used in URLs, scopes (``<slug>:write``) and write modes.
    ``secrets`` holds credentials (AES-GCM encrypted); ``settings`` holds everything non-secret.
    """

    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # openapi | postman | curl | soap | graphql | odata | sql | mongodb | mcp
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    auth_type: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    secrets: Mapped[dict | None] = mapped_column(EncryptedJSON, nullable=True)  # type: ignore[type-arg]
    settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    source: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]  # where the definition came from
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    operation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Operation(Base):
    """One callable thing in a connection: REST endpoint, SOAP operation, GraphQL field, OData entity-set
    action, SQL/Mongo tool or a bridged MCP tool. ``binding`` says how to execute it (protocol-specific)."""

    __tablename__ = "operations"
    __table_args__ = (UniqueConstraint("connection_id", "key", name="uq_operation_connection_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(255), nullable=False)       # stable id within the connection
    name: Mapped[str] = mapped_column(String(128), nullable=False)      # tool-safe name, e.g. list_pets
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    method: Mapped[str] = mapped_column(String(16), nullable=False, default="")  # HTTP verb / QUERY / MUTATION / CALL
    target: Mapped[str] = mapped_column(Text, nullable=False, default="")        # path, op name, field, entity set …
    input_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    output_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    binding: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deprecated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")  # flattened for search (Phase 3 indexes it)


class ConnectionSync(Base):
    """A read operation of a connection, called on a schedule so its results are kept locally
    (``connection_records``) for fast lookup and search without calling the API every time."""

    __tablename__ = "connection_syncs"
    __table_args__ = (UniqueConstraint("connection_id", "operation", name="uq_connection_sync_operation"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)          # operation name
    args: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    records_path: Mapped[str] = mapped_column(String(255), nullable=False, default="")  # "" = find the list
    key_field: Mapped[str] = mapped_column(String(255), nullable=False, default="")     # "" = id / key / hash
    paging: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]  # None = detect
    max_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="never")  # never|running|ok|partial|error
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class ConnectionRecord(Base):
    """One record kept from a sync (a ticket, device, customer …). ``removed_at`` is set when a full sync no
    longer returned it; it is kept for history and cleared if it comes back."""

    __tablename__ = "connection_records"
    __table_args__ = (UniqueConstraint("sync_id", "record_key", name="uq_connection_record_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    sync_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    record_key: Mapped[str] = mapped_column(String(255), nullable=False)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)