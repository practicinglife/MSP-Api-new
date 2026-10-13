"""Playbooks: YAML templates in app/org_playbooks/ that combine commands into ONE change request.

Format::

    name: standardize-client
    title: Standardize a client everywhere
    description: ...
    params:
      client: {type: string, description: Client id or code}
    steps:
      - command: apply_standard_name
        params: {client: "{{client}}"}
      - command: fix_psa_endpoint_ids
        params: {client: "{{client}}"}
        optional: true        # skip (with a warning) if this command has nothing to do

Registered as command ``playbook:<name>``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import structlog
import yaml

from app.services.commands import REGISTRY, Command, CommandError, Plan, get_command, register, validate_params

log = structlog.get_logger(__name__)

PLAYBOOK_DIR = Path(__file__).resolve().parent.parent.parent / "org_playbooks"
_RISK_ORDER = {"create": 0, "update": 1, "varies": 1, "delete": 2}


def _render(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, str):
        m = re.fullmatch(r"\{\{\s*(\w+)\s*\}\}", value)
        if m:
            return params.get(m.group(1))
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda mm: str(params.get(mm.group(1), "")), value)
    if isinstance(value, dict):
        return {k: _render(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, params) for v in value]
    return value


def _make(spec: dict[str, Any]) -> Command:
    async def plan(params: dict[str, Any]) -> Plan:
        steps: list[dict[str, Any]] = []
        warnings: list[str] = []
        risk, client_id = "create", None
        for item in spec.get("steps", []):
            cmd = get_command(item["command"])
            sub = {k: v for k, v in _render(item.get("params", {}), params).items() if v not in (None, "")}
            try:
                p = await cmd.plan(validate_params(cmd, sub))
            except CommandError as exc:
                if item.get("optional"):
                    warnings.append(f"{cmd.title}: {exc}")
                    continue
                raise
            steps += p.steps
            warnings += p.warnings
            client_id = client_id or p.client_id
            if _RISK_ORDER.get(p.risk, 1) > _RISK_ORDER.get(risk, 0):
                risk = p.risk
        if not steps:
            raise CommandError("Playbook has nothing to do. " + " ".join(warnings))
        return Plan(_render(spec.get("title", spec["name"]), params), steps, risk=risk,
                    client_id=client_id, warnings=warnings)

    raw = spec.get("params") or {}
    required = [k for k, v in raw.items() if v.get("required", True)]
    props = {k: {kk: vv for kk, vv in v.items() if kk != "required"} for k, v in raw.items()}
    platforms = sorted({p for s in spec.get("steps", [])
                        for p in (REGISTRY[s["command"]].platforms if s["command"] in REGISTRY else [])})
    return Command(f"playbook:{spec['name']}", spec.get("title", spec["name"]), spec.get("description", ""),
                   spec.get("risk", "varies"), props, required, plan, platforms)


def load_playbooks() -> None:
    if not PLAYBOOK_DIR.is_dir():
        return
    for f in sorted(PLAYBOOK_DIR.glob("*.yaml")):
        try:
            register(_make(yaml.safe_load(f.read_text(encoding="utf-8"))))
        except Exception as exc:  # noqa: BLE001
            log.error("playbook.load_failed", file=f.name, error=str(exc))


load_playbooks()
