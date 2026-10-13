"""save_workflow: add, update or remove a recipe, playbook or skill in the org library — as a change request.

Saving goes through the same gates as any vendor change (Editor submits, an Approver approves), and the approval is
bound to the exact text. Content is validated before it can even be proposed:

* playbook — YAML; every step is an existing (non-playbook) command, so a playbook can never add a new kind of
  vendor call, and every run of it is still its own approved change.
* recipe   — YAML; every step is an org MCP **read** tool.
* skill    — Markdown with ``name`` / ``description`` frontmatter.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

import yaml

from app.services.commands import REGISTRY, Command, CommandError, Plan, register
from app.services.recipes import NAME_RE, RECIPE_DIR, validate_recipe

APP_DIR = Path(__file__).resolve().parent.parent.parent
MAX_BYTES = 60_000


def target_path(kind: str, name: str) -> Path:
    if kind == "playbook":
        return APP_DIR / "org_playbooks" / f"{name}.yaml"
    if kind == "recipe":
        return RECIPE_DIR / f"{name}.yaml"
    return APP_DIR / "org_skills" / name / "SKILL.md"


def validate(kind: str, name: str, content: str) -> list[str]:
    if len(content.encode("utf-8")) > MAX_BYTES:
        return [f"content is over {MAX_BYTES // 1000} KB"]
    if kind == "skill":
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.+)$", content, re.S)
        if not m:
            return ["skill must start with frontmatter: ---\\nname: …\\ndescription: …\\n--- then the body"]
        meta = dict(re.findall(r"^(\w+):\s*(.+)$", m.group(1), re.M))
        problems = []
        if meta.get("name") != name:
            problems.append(f"frontmatter name must be {name!r}")
        if not meta.get("description"):
            problems.append("frontmatter description is required (it decides when the skill is used)")
        return problems
    try:
        spec = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return [f"YAML doesn't parse: {exc}"]
    if kind == "recipe":
        from app.api.org_mcp import recipe_tools
        return validate_recipe(spec, recipe_tools(), name)
    # playbook
    if not isinstance(spec, dict):
        return ["playbook must be a YAML mapping"]
    problems = []
    if spec.get("name") != name:
        problems.append(f"name in the file must be {name!r}")
    if not spec.get("description"):
        problems.append("description is required")
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        return problems + ["steps must be a non-empty list"]
    from app.services.commands import list_commands
    list_commands()  # make sure every module is registered
    for i, st in enumerate(steps, 1):
        cmd = (st or {}).get("command") if isinstance(st, dict) else None
        if not cmd or cmd not in REGISTRY:
            problems.append(f"step {i}: unknown command {cmd!r} (see list_commands)")
        elif cmd.startswith("playbook:"):
            problems.append(f"step {i}: playbooks can't call other playbooks — list their commands instead")
    if not problems:
        from app.services.commands.playbooks import _make
        try:
            _make(spec)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"playbook doesn't build: {exc}")
    return problems


async def plan_save_workflow(p: dict[str, Any]) -> Plan:
    kind, name = p["kind"], p["name"].strip()
    if not NAME_RE.match(name):
        raise CommandError("name must be kebab-case (a-z, 0-9, -), 2–49 chars")
    path = target_path(kind, name)
    current = path.read_text(encoding="utf-8") if path.exists() else None
    label = f"{kind} '{name}'"

    if p.get("delete"):
        if current is None:
            raise CommandError(f"There is no {label} to remove")
        return Plan(f"Remove {label} from the library",
                    [{"kind": "local", "action": "library_write", "description": f"Delete {label}",
                      "args": {"kind": kind, "name": name, "content": None}, "before": current,
                      "before_ref": {"action": "library_read", "args": {"kind": kind, "name": name}}}],
                    risk="delete", warnings=["Anyone relying on it will no longer find it."])

    content = (p.get("content") or "").replace("\r\n", "\n").strip() + "\n"
    if not content.strip():
        raise CommandError("content is required")
    problems = validate(kind, name, content)
    if problems:
        raise CommandError("Not saved — fix first: " + "; ".join(problems))
    if current == content:
        raise CommandError(f"{label} already has exactly this content")

    warnings = []
    if current is not None:
        diff = "".join(difflib.unified_diff(current.splitlines(True), content.splitlines(True),
                                            "current", "proposed", n=1))
        warnings.append("Changes vs current:\n" + (diff[:4000] + ("\n…" if len(diff) > 4000 else "")))
    if kind == "playbook":
        warnings.append("Saving only adds the template — every run is still its own change for Editor + Approver.")
    step = {"kind": "local", "action": "library_write",
            "description": f"{'Update' if current is not None else 'Add'} {label} — live for everyone right after",
            "args": {"kind": kind, "name": name, "content": content}, "before": current,
            # stale check: if someone else changes this file before approval, nothing is overwritten
            "before_ref": {"action": "library_read", "args": {"kind": kind, "name": name}}}
    return Plan(f"{'Update' if current is not None else 'Add'} {label} in the library", [step],
                risk="update" if current is not None else "create", warnings=warnings)


register(Command(
    "save_workflow", "Save to the workflow library",
    "Add, update or remove a recipe (read workflow), playbook (multi-step change template) or skill (how-to) "
    "in the org library so everyone reuses it. Validated before proposing; live right after an Approver approves.",
    "update",
    {"kind": {"type": "string", "enum": ["recipe", "playbook", "skill"], "description": "What to save"},
     "name": {"type": "string", "description": "kebab-case name, e.g. 'client-health'"},
     "content": {"type": "string", "maxLength": MAX_BYTES,
                 "description": "Full file: YAML for recipe/playbook, Markdown with frontmatter for skill"},
     "delete": {"type": "boolean", "description": "Remove it instead of saving"}},
    ["kind", "name"], plan_save_workflow, [],
))
