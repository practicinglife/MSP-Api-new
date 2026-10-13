"""Baseline: every table as of the first migrated release.

New databases are built from here. Databases created before migrations existed are repaired to this shape and
stamped with this revision (see app/db/migrations.py), so this file never runs against them.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('agent_requests',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('user_id', sa.String(length=128), nullable=False),
    sa.Column('raw_text', sa.Text(), nullable=False),
    sa.Column('intent', sa.String(length=128), nullable=False),
    sa.Column('parameters', sa.JSON(), nullable=False),
    sa.Column('workflow_id', sa.String(length=36), nullable=True),
    sa.Column('stage', sa.String(length=16), nullable=False),
    sa.Column('outcome', sa.String(length=32), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('result_summary', sa.Text(), nullable=False),
    sa.Column('error', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_agent_requests_intent'), 'agent_requests', ['intent'], unique=False)
    op.create_index(op.f('ix_agent_requests_received_at'), 'agent_requests', ['received_at'], unique=False)
    op.create_index(op.f('ix_agent_requests_workflow_id'), 'agent_requests', ['workflow_id'], unique=False)
    op.create_table('agent_workflows',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('intent', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=256), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('auto_generated', sa.Boolean(), nullable=False),
    sa.Column('steps', sa.JSON(), nullable=False),
    sa.Column('parameters_schema', sa.JSON(), nullable=True),
    sa.Column('success_count', sa.Integer(), nullable=False),
    sa.Column('failure_count', sa.Integer(), nullable=False),
    sa.Column('avg_duration_ms', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('intent', 'version', name='uq_workflow_intent_version')
    )
    op.create_index(op.f('ix_agent_workflows_active'), 'agent_workflows', ['active'], unique=False)
    op.create_index(op.f('ix_agent_workflows_intent'), 'agent_workflows', ['intent'], unique=False)
    op.create_table('alert_company_aliases',
    sa.Column('key', sa.String(length=300), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=256), nullable=False),
    sa.Column('psa_company_id', sa.Integer(), nullable=False),
    sa.Column('company_name', sa.String(length=256), nullable=False),
    sa.Column('set_by', sa.String(length=255), nullable=False),
    sa.Column('set_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('api_endpoints',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('platform', sa.String(length=64), nullable=False),
    sa.Column('path', sa.String(length=1024), nullable=False),
    sa.Column('method', sa.String(length=16), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('tags', sa.JSON(), nullable=False),
    sa.Column('parameters', sa.JSON(), nullable=False),
    sa.Column('request_body_schema', sa.JSON(), nullable=True),
    sa.Column('response_schema', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_api_endpoints_platform'), 'api_endpoints', ['platform'], unique=False)
    op.create_table('api_keys',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('prefix', sa.String(length=16), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('revoked', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('roles', sa.JSON(), nullable=True),
    sa.Column('scopes', sa.JSON(), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.String(length=255), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key_hash')
    )
    op.create_table('api_response_cache',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('platform', sa.String(length=64), nullable=False),
    sa.Column('endpoint_path', sa.String(length=1024), nullable=False),
    sa.Column('method', sa.String(length=16), nullable=False),
    sa.Column('request_params', sa.JSON(), nullable=True),
    sa.Column('status_code', sa.Integer(), nullable=False),
    sa.Column('response_body', sa.JSON(), nullable=True),
    sa.Column('response_headers', sa.JSON(), nullable=True),
    sa.Column('cached_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_api_response_cache_platform'), 'api_response_cache', ['platform'], unique=False)
    op.create_table('api_specs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('platform', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=256), nullable=False),
    sa.Column('version', sa.String(length=64), nullable=False),
    sa.Column('swagger_url', sa.String(length=1024), nullable=False),
    sa.Column('spec_json', sa.JSON(), nullable=False),
    sa.Column('endpoint_count', sa.Integer(), nullable=False),
    sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_api_specs_platform'), 'api_specs', ['platform'], unique=True)
    op.create_table('asio_endpoint_av',
    sa.Column('endpoint_id', sa.String(length=64), nullable=False),
    sa.Column('client_id', sa.String(length=36), nullable=True),
    sa.Column('company_platform_id', sa.String(length=64), nullable=False),
    sa.Column('hostname', sa.String(length=256), nullable=False),
    sa.Column('host_key', sa.String(length=32), nullable=False),
    sa.Column('products', sa.JSON(), nullable=True),
    sa.Column('vendors', sa.JSON(), nullable=True),
    sa.Column('modified_at', sa.String(length=40), nullable=False),
    sa.Column('agent_installed_at', sa.String(length=40), nullable=False),
    sa.Column('last_contact', sa.String(length=40), nullable=False),
    sa.Column('online', sa.Boolean(), nullable=True),
    sa.Column('os_name', sa.String(length=128), nullable=False),
    sa.Column('is_server', sa.Boolean(), nullable=True),
    sa.Column('roles', sa.JSON(), nullable=True),
    sa.Column('swept_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('endpoint_id')
    )
    op.create_index(op.f('ix_asio_endpoint_av_client_id'), 'asio_endpoint_av', ['client_id'], unique=False)
    op.create_index(op.f('ix_asio_endpoint_av_host_key'), 'asio_endpoint_av', ['host_key'], unique=False)
    op.create_table('asio_last_contact_imports',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('endpoint_id', sa.String(length=256), nullable=False),
    sa.Column('platform_id', sa.String(length=256), nullable=False),
    sa.Column('hostname', sa.String(length=512), nullable=False),
    sa.Column('hostname_norm', sa.String(length=512), nullable=False),
    sa.Column('company_name', sa.String(length=512), nullable=False),
    sa.Column('site_name', sa.String(length=512), nullable=False),
    sa.Column('last_contact', sa.String(length=128), nullable=False),
    sa.Column('source_filename', sa.String(length=256), nullable=False),
    sa.Column('imported_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_asio_last_contact_imports_endpoint_id'), 'asio_last_contact_imports', ['endpoint_id'], unique=False)
    op.create_index(op.f('ix_asio_last_contact_imports_hostname_norm'), 'asio_last_contact_imports', ['hostname_norm'], unique=False)
    op.create_index(op.f('ix_asio_last_contact_imports_platform_id'), 'asio_last_contact_imports', ['platform_id'], unique=False)
    op.create_table('audit_log',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('principal', sa.String(length=255), nullable=False),
    sa.Column('auth_kind', sa.String(length=16), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('method', sa.String(length=8), nullable=False),
    sa.Column('path', sa.String(length=512), nullable=False),
    sa.Column('action', sa.String(length=128), nullable=True),
    sa.Column('status', sa.Integer(), nullable=False),
    sa.Column('ip', sa.String(length=64), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_log_at'), 'audit_log', ['at'], unique=False)
    op.create_index(op.f('ix_audit_log_principal'), 'audit_log', ['principal'], unique=False)
    op.create_table('auth_flows',
    sa.Column('state', sa.String(length=64), nullable=False),
    sa.Column('nonce', sa.String(length=64), nullable=False),
    sa.Column('code_verifier', sa.String(length=128), nullable=False),
    sa.Column('purpose', sa.String(length=32), nullable=False),
    sa.Column('next_url', sa.Text(), nullable=True),
    sa.Column('oauth_request', sa.JSON(), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('state')
    )
    op.create_table('av_assignment_cache',
    sa.Column('company_name', sa.String(length=512), nullable=False),
    sa.Column('assigned_av', sa.String(length=32), nullable=False),
    sa.Column('refreshed_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('company_name')
    )
    op.create_table('av_review_items',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('endpoint_id', sa.String(length=64), nullable=False),
    sa.Column('client_id', sa.String(length=36), nullable=True),
    sa.Column('hostname', sa.String(length=256), nullable=False),
    sa.Column('expected_av', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('change_id', sa.String(length=36), nullable=True),
    sa.Column('checks', sa.Integer(), nullable=False),
    sa.Column('note', sa.String(length=512), nullable=False),
    sa.Column('flagged_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cleared_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_av_review_items_change_id'), 'av_review_items', ['change_id'], unique=False)
    op.create_index(op.f('ix_av_review_items_client_id'), 'av_review_items', ['client_id'], unique=False)
    op.create_index(op.f('ix_av_review_items_endpoint_id'), 'av_review_items', ['endpoint_id'], unique=False)
    op.create_index(op.f('ix_av_review_items_status'), 'av_review_items', ['status'], unique=False)
    op.create_table('breakdown_companies',
    sa.Column('company_name', sa.String(length=512), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('matched_count', sa.Integer(), nullable=False),
    sa.Column('mismatched_count', sa.Integer(), nullable=False),
    sa.Column('assigned_av', sa.String(length=32), nullable=False),
    sa.Column('psa_present', sa.Boolean(), nullable=False),
    sa.Column('computed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=True),
    sa.PrimaryKeyConstraint('company_name')
    )
    op.create_table('breakdown_snapshots',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('snapshot_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('company_count', sa.Integer(), nullable=False),
    sa.Column('matched_count', sa.Integer(), nullable=False),
    sa.Column('mismatched_count', sa.Integer(), nullable=False),
    sa.Column('total_unique', sa.Integer(), nullable=False),
    sa.Column('company_names', sa.JSON(), nullable=True),
    sa.Column('changes', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_breakdown_snapshots_run_id'), 'breakdown_snapshots', ['run_id'], unique=False)
    op.create_table('change_events',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('change_id', sa.String(length=36), nullable=False),
    sa.Column('event', sa.String(length=32), nullable=False),
    sa.Column('actor', sa.String(length=255), nullable=False),
    sa.Column('details', sa.JSON(), nullable=True),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_change_events_change_id'), 'change_events', ['change_id'], unique=False)
    op.create_table('change_requests',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('number', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('command', sa.String(length=128), nullable=False),
    sa.Column('params', sa.JSON(), nullable=True),
    sa.Column('client_id', sa.String(length=36), nullable=True),
    sa.Column('risk', sa.String(length=16), nullable=False),
    sa.Column('steps', sa.JSON(), nullable=True),
    sa.Column('payload_hash', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('requested_by', sa.String(length=255), nullable=False),
    sa.Column('submitted_by', sa.String(length=255), nullable=True),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('approved_by', sa.String(length=255), nullable=True),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('rejected_reason', sa.Text(), nullable=True),
    sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('results', sa.JSON(), nullable=True),
    sa.Column('warnings', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_change_requests_client_id'), 'change_requests', ['client_id'], unique=False)
    op.create_index(op.f('ix_change_requests_number'), 'change_requests', ['number'], unique=False)
    op.create_index(op.f('ix_change_requests_status'), 'change_requests', ['status'], unique=False)
    op.create_table('client_platform_links',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('client_id', sa.String(length=36), nullable=False),
    sa.Column('platform', sa.String(length=64), nullable=False),
    sa.Column('platform_company_id', sa.String(length=512), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('link_method', sa.String(length=32), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('platform', 'platform_company_id', 'client_id', name='uq_client_platform_link')
    )
    op.create_index(op.f('ix_client_platform_links_client_id'), 'client_platform_links', ['client_id'], unique=False)
    op.create_index(op.f('ix_client_platform_links_platform'), 'client_platform_links', ['platform'], unique=False)
    op.create_index(op.f('ix_client_platform_links_platform_company_id'), 'client_platform_links', ['platform_company_id'], unique=False)
    op.create_table('clients',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('client_code', sa.String(length=8), nullable=True),
    sa.Column('standard_name', sa.String(length=255), nullable=False),
    sa.Column('legal_name', sa.String(length=255), nullable=True),
    sa.Column('psa_company_id', sa.String(length=64), nullable=True),
    sa.Column('av_vendor', sa.String(length=32), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('naming_status', sa.String(length=32), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_code'),
    sa.UniqueConstraint('psa_company_id')
    )
    op.create_index(op.f('ix_clients_standard_name'), 'clients', ['standard_name'], unique=False)
    op.create_table('company_aliases',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('alias_name', sa.String(length=512), nullable=False),
    sa.Column('canonical_name', sa.String(length=512), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_company_aliases_alias_name'), 'company_aliases', ['alias_name'], unique=True)
    op.create_index(op.f('ix_company_aliases_canonical_name'), 'company_aliases', ['canonical_name'], unique=False)
    op.create_table('company_mappings',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('platform', sa.String(length=64), nullable=False),
    sa.Column('raw_name', sa.String(length=512), nullable=False),
    sa.Column('normalized_name', sa.String(length=512), nullable=False),
    sa.Column('canonical_name', sa.String(length=512), nullable=False),
    sa.Column('alias_source', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('platform', 'raw_name', name='uq_company_mapping')
    )
    op.create_index(op.f('ix_company_mappings_canonical_name'), 'company_mappings', ['canonical_name'], unique=False)
    op.create_index(op.f('ix_company_mappings_platform'), 'company_mappings', ['platform'], unique=False)
    op.create_table('company_matches',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=True),
    sa.Column('platform_a', sa.String(length=64), nullable=False),
    sa.Column('platform_id_a', sa.String(length=256), nullable=False),
    sa.Column('name_a', sa.String(length=512), nullable=False),
    sa.Column('platform_b', sa.String(length=64), nullable=True),
    sa.Column('platform_id_b', sa.String(length=256), nullable=True),
    sa.Column('name_b', sa.String(length=512), nullable=True),
    sa.Column('score', sa.Float(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_company_matches_run_id'), 'company_matches', ['run_id'], unique=False)
    op.create_table('company_overrides',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('platform_a', sa.String(length=64), nullable=False),
    sa.Column('platform_id_a', sa.String(length=256), nullable=False),
    sa.Column('platform_b', sa.String(length=64), nullable=False),
    sa.Column('platform_id_b', sa.String(length=256), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('connection_records',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('connection_id', sa.String(length=36), nullable=False),
    sa.Column('sync_id', sa.String(length=36), nullable=False),
    sa.Column('record_key', sa.String(length=255), nullable=False),
    sa.Column('data', sa.JSON(), nullable=True),
    sa.Column('hash', sa.String(length=64), nullable=False),
    sa.Column('first_seen', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
    sa.Column('changed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('removed_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('sync_id', 'record_key', name='uq_connection_record_key')
    )
    op.create_index(op.f('ix_connection_records_connection_id'), 'connection_records', ['connection_id'], unique=False)
    op.create_index(op.f('ix_connection_records_sync_id'), 'connection_records', ['sync_id'], unique=False)
    op.create_table('connection_syncs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('connection_id', sa.String(length=36), nullable=False),
    sa.Column('operation', sa.String(length=128), nullable=False),
    sa.Column('args', sa.JSON(), nullable=True),
    sa.Column('interval_minutes', sa.Integer(), nullable=False),
    sa.Column('records_path', sa.String(length=255), nullable=False),
    sa.Column('key_field', sa.String(length=255), nullable=False),
    sa.Column('paging', sa.JSON(), nullable=True),
    sa.Column('max_pages', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=False),
    sa.Column('last_summary', sa.JSON(), nullable=True),
    sa.Column('record_count', sa.Integer(), nullable=False),
    sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_ok_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('connection_id', 'operation', name='uq_connection_sync_operation')
    )
    op.create_index(op.f('ix_connection_syncs_connection_id'), 'connection_syncs', ['connection_id'], unique=False)
    op.create_table('connections',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('base_url', sa.Text(), nullable=False),
    sa.Column('auth_type', sa.String(length=32), nullable=False),
    sa.Column('secrets', sa.JSON(), nullable=True),
    sa.Column('settings', sa.JSON(), nullable=True),
    sa.Column('source', sa.JSON(), nullable=True),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('operation_count', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('imported_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_connections_slug'), 'connections', ['slug'], unique=True)
    op.create_table('connector_configs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('platform', sa.String(length=64), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('credentials', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('platform')
    )
    op.create_table('connector_events',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('connector', sa.String(length=64), nullable=False),
    sa.Column('event_type', sa.String(length=32), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('company_count', sa.Integer(), nullable=False),
    sa.Column('device_count', sa.Integer(), nullable=False),
    sa.Column('latency_ms', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_connector_events_run_id'), 'connector_events', ['run_id'], unique=False)
    op.create_table('daily_snapshots',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('snapshot_date', sa.String(length=10), nullable=False),
    sa.Column('company_canonical', sa.String(length=255), nullable=False),
    sa.Column('total_devices', sa.Integer(), nullable=False),
    sa.Column('matched_count', sa.Integer(), nullable=False),
    sa.Column('unmatched_count', sa.Integer(), nullable=False),
    sa.Column('asio_count', sa.Integer(), nullable=False),
    sa.Column('psa_count', sa.Integer(), nullable=False),
    sa.Column('sc_count', sa.Integer(), nullable=False),
    sa.Column('s1_count', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('taken_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('snapshot_date', 'company_canonical', name='uq_snapshot_company_date')
    )
    op.create_index(op.f('ix_daily_snapshots_company_canonical'), 'daily_snapshots', ['company_canonical'], unique=False)
    op.create_index(op.f('ix_daily_snapshots_snapshot_date'), 'daily_snapshots', ['snapshot_date'], unique=False)
    op.create_table('device_av_status',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('hostname', sa.String(length=512), nullable=False),
    sa.Column('hostname_norm', sa.String(length=512), nullable=False),
    sa.Column('endpoint_id', sa.String(length=256), nullable=False),
    sa.Column('company_canonical', sa.String(length=512), nullable=False),
    sa.Column('av_vendor', sa.String(length=128), nullable=False),
    sa.Column('av_product', sa.String(length=256), nullable=False),
    sa.Column('av_version', sa.String(length=128), nullable=False),
    sa.Column('av_status', sa.String(length=64), nullable=False),
    sa.Column('av_enabled', sa.Boolean(), nullable=True),
    sa.Column('av_up_to_date', sa.Boolean(), nullable=True),
    sa.Column('detection_source', sa.String(length=64), nullable=False),
    sa.Column('detected_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('hostname_norm', 'av_vendor', 'detection_source', name='uq_device_av_vendor_source')
    )
    op.create_index(op.f('ix_device_av_status_av_vendor'), 'device_av_status', ['av_vendor'], unique=False)
    op.create_index(op.f('ix_device_av_status_company_canonical'), 'device_av_status', ['company_canonical'], unique=False)
    op.create_index(op.f('ix_device_av_status_hostname_norm'), 'device_av_status', ['hostname_norm'], unique=False)
    op.create_table('device_matches',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=True),
    sa.Column('platform_a', sa.String(length=64), nullable=False),
    sa.Column('platform_id_a', sa.String(length=256), nullable=False),
    sa.Column('hostname_a', sa.String(length=512), nullable=False),
    sa.Column('platform_b', sa.String(length=64), nullable=True),
    sa.Column('platform_id_b', sa.String(length=256), nullable=True),
    sa.Column('hostname_b', sa.String(length=512), nullable=True),
    sa.Column('score', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('reasons', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_device_matches_run_id'), 'device_matches', ['run_id'], unique=False)
    op.create_table('device_overrides',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('platform_a', sa.String(length=64), nullable=False),
    sa.Column('platform_id_a', sa.String(length=256), nullable=False),
    sa.Column('platform_b', sa.String(length=64), nullable=False),
    sa.Column('platform_id_b', sa.String(length=256), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('local_users',
    sa.Column('username', sa.String(length=128), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('totp_secret', sa.String(length=64), nullable=False),
    sa.Column('roles', sa.JSON(), nullable=True),
    sa.Column('disabled', sa.Boolean(), nullable=False),
    sa.Column('failed_count', sa.Integer(), nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_totp_step', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('username')
    )
    op.create_table('machine_role_overrides',
    sa.Column('endpoint_id', sa.String(length=64), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('hostname', sa.String(length=256), nullable=False),
    sa.Column('set_by', sa.String(length=255), nullable=False),
    sa.Column('note', sa.String(length=512), nullable=False),
    sa.Column('set_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('endpoint_id')
    )
    op.create_table('oauth_clients',
    sa.Column('client_id', sa.String(length=64), nullable=False),
    sa.Column('client_name', sa.String(length=255), nullable=False),
    sa.Column('redirect_uris', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('client_id')
    )
    op.create_table('oauth_tokens',
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('client_id', sa.String(length=64), nullable=False),
    sa.Column('api_key_id', sa.String(length=64), nullable=False),
    sa.Column('redirect_uri', sa.Text(), nullable=True),
    sa.Column('code_challenge', sa.String(length=128), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used', sa.Boolean(), nullable=False),
    sa.Column('user_upn', sa.String(length=255), nullable=True),
    sa.Column('user_roles', sa.JSON(), nullable=True),
    sa.Column('resource', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('token_hash')
    )
    op.create_index(op.f('ix_oauth_tokens_api_key_id'), 'oauth_tokens', ['api_key_id'], unique=False)
    op.create_index(op.f('ix_oauth_tokens_kind'), 'oauth_tokens', ['kind'], unique=False)
    op.create_table('operations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('connection_id', sa.String(length=36), nullable=False),
    sa.Column('key', sa.String(length=255), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('title', sa.String(length=512), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('tags', sa.JSON(), nullable=True),
    sa.Column('method', sa.String(length=16), nullable=False),
    sa.Column('target', sa.Text(), nullable=False),
    sa.Column('input_schema', sa.JSON(), nullable=True),
    sa.Column('output_schema', sa.JSON(), nullable=True),
    sa.Column('binding', sa.JSON(), nullable=True),
    sa.Column('read_only', sa.Boolean(), nullable=False),
    sa.Column('pinned', sa.Boolean(), nullable=False),
    sa.Column('deprecated', sa.Boolean(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('search_text', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('connection_id', 'key', name='uq_operation_connection_key')
    )
    op.create_index(op.f('ix_operations_connection_id'), 'operations', ['connection_id'], unique=False)
    op.create_table('platform_companies',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('platform', sa.String(length=64), nullable=False),
    sa.Column('platform_company_id', sa.String(length=512), nullable=False),
    sa.Column('name', sa.String(length=512), nullable=False),
    sa.Column('name_norm', sa.String(length=512), nullable=False),
    sa.Column('extra', sa.JSON(), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('platform', 'platform_company_id', name='uq_platform_company')
    )
    op.create_index(op.f('ix_platform_companies_name_norm'), 'platform_companies', ['name_norm'], unique=False)
    op.create_index(op.f('ix_platform_companies_platform'), 'platform_companies', ['platform'], unique=False)
    op.create_table('platform_devices',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('platform', sa.String(length=64), nullable=False),
    sa.Column('platform_id', sa.String(length=256), nullable=False),
    sa.Column('endpoint_id', sa.String(length=256), nullable=False),
    sa.Column('hostname', sa.String(length=512), nullable=False),
    sa.Column('hostname_norm', sa.String(length=512), nullable=False),
    sa.Column('company_raw', sa.String(length=512), nullable=False),
    sa.Column('company_canonical', sa.String(length=512), nullable=False),
    sa.Column('company_platform_id', sa.String(length=256), nullable=False),
    sa.Column('client_id', sa.String(length=36), nullable=True),
    sa.Column('site_name', sa.String(length=512), nullable=False),
    sa.Column('serial_number', sa.String(length=256), nullable=False),
    sa.Column('mac_addresses', sa.Text(), nullable=False),
    sa.Column('raw_json', sa.JSON(), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('platform', 'platform_id', name='uq_platform_device')
    )
    op.create_index(op.f('ix_platform_devices_client_id'), 'platform_devices', ['client_id'], unique=False)
    op.create_index(op.f('ix_platform_devices_company_canonical'), 'platform_devices', ['company_canonical'], unique=False)
    op.create_index(op.f('ix_platform_devices_company_platform_id'), 'platform_devices', ['company_platform_id'], unique=False)
    op.create_index(op.f('ix_platform_devices_endpoint_id'), 'platform_devices', ['endpoint_id'], unique=False)
    op.create_index(op.f('ix_platform_devices_hostname_norm'), 'platform_devices', ['hostname_norm'], unique=False)
    op.create_index(op.f('ix_platform_devices_platform'), 'platform_devices', ['platform'], unique=False)
    op.create_table('platform_fingerprints',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('platform', sa.String(length=64), nullable=False),
    sa.Column('company_canonical', sa.String(length=512), nullable=False),
    sa.Column('device_count', sa.Integer(), nullable=False),
    sa.Column('devices_hash', sa.String(length=64), nullable=False),
    sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_changed_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('platform', 'company_canonical', name='uq_fingerprint_platform_company')
    )
    op.create_index(op.f('ix_platform_fingerprints_company_canonical'), 'platform_fingerprints', ['company_canonical'], unique=False)
    op.create_index(op.f('ix_platform_fingerprints_platform'), 'platform_fingerprints', ['platform'], unique=False)
    op.create_table('platform_policies',
    sa.Column('platform', sa.String(length=64), nullable=False),
    sa.Column('write_mode', sa.String(length=16), nullable=False),
    sa.Column('updated_by', sa.String(length=255), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('platform')
    )
    op.create_table('reconciled_devices',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('company_name', sa.String(length=512), nullable=False),
    sa.Column('site_name', sa.String(length=512), nullable=False),
    sa.Column('hostname', sa.String(length=512), nullable=False),
    sa.Column('endpoint_id', sa.String(length=128), nullable=False),
    sa.Column('mac_address', sa.String(length=256), nullable=False),
    sa.Column('serial_number', sa.String(length=256), nullable=False),
    sa.Column('platforms', sa.JSON(), nullable=False),
    sa.Column('match_type', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_reconciled_devices_company_name'), 'reconciled_devices', ['company_name'], unique=False)
    op.create_index(op.f('ix_reconciled_devices_endpoint_id'), 'reconciled_devices', ['endpoint_id'], unique=False)
    op.create_table('sync_runs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('company_match_count', sa.Integer(), nullable=False),
    sa.Column('device_match_count', sa.Integer(), nullable=False),
    sa.Column('error_count', sa.Integer(), nullable=False),
    sa.Column('errors', sa.JSON(), nullable=True),
    sa.Column('warning_count', sa.Integer(), nullable=False),
    sa.Column('warnings', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('user_sessions',
    sa.Column('id_hash', sa.String(length=64), nullable=False),
    sa.Column('upn', sa.String(length=255), nullable=False),
    sa.Column('oid', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('roles', sa.JSON(), nullable=True),
    sa.Column('csrf', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id_hash')
    )
    op.create_index(op.f('ix_user_sessions_upn'), 'user_sessions', ['upn'], unique=False)
    op.create_table('workflow_executions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('workflow_id', sa.String(length=36), nullable=False),
    sa.Column('request_id', sa.String(length=36), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('parameters', sa.JSON(), nullable=False),
    sa.Column('step_results', sa.JSON(), nullable=False),
    sa.Column('outcome', sa.String(length=32), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=False),
    sa.Column('dry_run', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_workflow_executions_request_id'), 'workflow_executions', ['request_id'], unique=False)
    op.create_index(op.f('ix_workflow_executions_started_at'), 'workflow_executions', ['started_at'], unique=False)
    op.create_index(op.f('ix_workflow_executions_workflow_id'), 'workflow_executions', ['workflow_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_workflow_executions_workflow_id'), table_name='workflow_executions')
    op.drop_index(op.f('ix_workflow_executions_started_at'), table_name='workflow_executions')
    op.drop_index(op.f('ix_workflow_executions_request_id'), table_name='workflow_executions')
    op.drop_table('workflow_executions')
    op.drop_index(op.f('ix_user_sessions_upn'), table_name='user_sessions')
    op.drop_table('user_sessions')
    op.drop_table('sync_runs')
    op.drop_index(op.f('ix_reconciled_devices_endpoint_id'), table_name='reconciled_devices')
    op.drop_index(op.f('ix_reconciled_devices_company_name'), table_name='reconciled_devices')
    op.drop_table('reconciled_devices')
    op.drop_table('platform_policies')
    op.drop_index(op.f('ix_platform_fingerprints_platform'), table_name='platform_fingerprints')
    op.drop_index(op.f('ix_platform_fingerprints_company_canonical'), table_name='platform_fingerprints')
    op.drop_table('platform_fingerprints')
    op.drop_index(op.f('ix_platform_devices_platform'), table_name='platform_devices')
    op.drop_index(op.f('ix_platform_devices_hostname_norm'), table_name='platform_devices')
    op.drop_index(op.f('ix_platform_devices_endpoint_id'), table_name='platform_devices')
    op.drop_index(op.f('ix_platform_devices_company_platform_id'), table_name='platform_devices')
    op.drop_index(op.f('ix_platform_devices_company_canonical'), table_name='platform_devices')
    op.drop_index(op.f('ix_platform_devices_client_id'), table_name='platform_devices')
    op.drop_table('platform_devices')
    op.drop_index(op.f('ix_platform_companies_platform'), table_name='platform_companies')
    op.drop_index(op.f('ix_platform_companies_name_norm'), table_name='platform_companies')
    op.drop_table('platform_companies')
    op.drop_index(op.f('ix_operations_connection_id'), table_name='operations')
    op.drop_table('operations')
    op.drop_index(op.f('ix_oauth_tokens_kind'), table_name='oauth_tokens')
    op.drop_index(op.f('ix_oauth_tokens_api_key_id'), table_name='oauth_tokens')
    op.drop_table('oauth_tokens')
    op.drop_table('oauth_clients')
    op.drop_table('machine_role_overrides')
    op.drop_table('local_users')
    op.drop_table('device_overrides')
    op.drop_index(op.f('ix_device_matches_run_id'), table_name='device_matches')
    op.drop_table('device_matches')
    op.drop_index(op.f('ix_device_av_status_hostname_norm'), table_name='device_av_status')
    op.drop_index(op.f('ix_device_av_status_company_canonical'), table_name='device_av_status')
    op.drop_index(op.f('ix_device_av_status_av_vendor'), table_name='device_av_status')
    op.drop_table('device_av_status')
    op.drop_index(op.f('ix_daily_snapshots_snapshot_date'), table_name='daily_snapshots')
    op.drop_index(op.f('ix_daily_snapshots_company_canonical'), table_name='daily_snapshots')
    op.drop_table('daily_snapshots')
    op.drop_index(op.f('ix_connector_events_run_id'), table_name='connector_events')
    op.drop_table('connector_events')
    op.drop_table('connector_configs')
    op.drop_index(op.f('ix_connections_slug'), table_name='connections')
    op.drop_table('connections')
    op.drop_index(op.f('ix_connection_syncs_connection_id'), table_name='connection_syncs')
    op.drop_table('connection_syncs')
    op.drop_index(op.f('ix_connection_records_sync_id'), table_name='connection_records')
    op.drop_index(op.f('ix_connection_records_connection_id'), table_name='connection_records')
    op.drop_table('connection_records')
    op.drop_table('company_overrides')
    op.drop_index(op.f('ix_company_matches_run_id'), table_name='company_matches')
    op.drop_table('company_matches')
    op.drop_index(op.f('ix_company_mappings_platform'), table_name='company_mappings')
    op.drop_index(op.f('ix_company_mappings_canonical_name'), table_name='company_mappings')
    op.drop_table('company_mappings')
    op.drop_index(op.f('ix_company_aliases_canonical_name'), table_name='company_aliases')
    op.drop_index(op.f('ix_company_aliases_alias_name'), table_name='company_aliases')
    op.drop_table('company_aliases')
    op.drop_index(op.f('ix_clients_standard_name'), table_name='clients')
    op.drop_table('clients')
    op.drop_index(op.f('ix_client_platform_links_platform_company_id'), table_name='client_platform_links')
    op.drop_index(op.f('ix_client_platform_links_platform'), table_name='client_platform_links')
    op.drop_index(op.f('ix_client_platform_links_client_id'), table_name='client_platform_links')
    op.drop_table('client_platform_links')
    op.drop_index(op.f('ix_change_requests_status'), table_name='change_requests')
    op.drop_index(op.f('ix_change_requests_number'), table_name='change_requests')
    op.drop_index(op.f('ix_change_requests_client_id'), table_name='change_requests')
    op.drop_table('change_requests')
    op.drop_index(op.f('ix_change_events_change_id'), table_name='change_events')
    op.drop_table('change_events')
    op.drop_index(op.f('ix_breakdown_snapshots_run_id'), table_name='breakdown_snapshots')
    op.drop_table('breakdown_snapshots')
    op.drop_table('breakdown_companies')
    op.drop_index(op.f('ix_av_review_items_status'), table_name='av_review_items')
    op.drop_index(op.f('ix_av_review_items_endpoint_id'), table_name='av_review_items')
    op.drop_index(op.f('ix_av_review_items_client_id'), table_name='av_review_items')
    op.drop_index(op.f('ix_av_review_items_change_id'), table_name='av_review_items')
    op.drop_table('av_review_items')
    op.drop_table('av_assignment_cache')
    op.drop_table('auth_flows')
    op.drop_index(op.f('ix_audit_log_principal'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_at'), table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_index(op.f('ix_asio_last_contact_imports_platform_id'), table_name='asio_last_contact_imports')
    op.drop_index(op.f('ix_asio_last_contact_imports_hostname_norm'), table_name='asio_last_contact_imports')
    op.drop_index(op.f('ix_asio_last_contact_imports_endpoint_id'), table_name='asio_last_contact_imports')
    op.drop_table('asio_last_contact_imports')
    op.drop_index(op.f('ix_asio_endpoint_av_host_key'), table_name='asio_endpoint_av')
    op.drop_index(op.f('ix_asio_endpoint_av_client_id'), table_name='asio_endpoint_av')
    op.drop_table('asio_endpoint_av')
    op.drop_index(op.f('ix_api_specs_platform'), table_name='api_specs')
    op.drop_table('api_specs')
    op.drop_index(op.f('ix_api_response_cache_platform'), table_name='api_response_cache')
    op.drop_table('api_response_cache')
    op.drop_table('api_keys')
    op.drop_index(op.f('ix_api_endpoints_platform'), table_name='api_endpoints')
    op.drop_table('api_endpoints')
    op.drop_table('alert_company_aliases')
    op.drop_index(op.f('ix_agent_workflows_intent'), table_name='agent_workflows')
    op.drop_index(op.f('ix_agent_workflows_active'), table_name='agent_workflows')
    op.drop_table('agent_workflows')
    op.drop_index(op.f('ix_agent_requests_workflow_id'), table_name='agent_requests')
    op.drop_index(op.f('ix_agent_requests_received_at'), table_name='agent_requests')
    op.drop_index(op.f('ix_agent_requests_intent'), table_name='agent_requests')
    op.drop_table('agent_requests')
