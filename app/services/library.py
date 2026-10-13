"""The org workflow library: one searchable index over commands, playbooks, recipes and skills.

Claude calls ``find_workflow`` before building a multi-step answer, so something worked out once is reused
by everyone instead of being rebuilt per conversation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from app.services.recipes import RECIPE_DIR, load_recipes

SKILLS_DIR = Path(__file__).resolve().parent.parent / "org_skills"
PLAYBOOK_DIR = Path(__file__).resolve().parent.parent / "org_playbooks"
KINDS = ("command", "playbook", "recipe", "skill")


def _skills() -> list[dict[str, str]]:
    out = []
    for f in sorted(SKILLS_DIR.glob("*/SKILL.md")) if SKILLS_DIR.is_dir() else []:
        text = f.read_text(encoding="utf-8")
        meta = dict(re.findall(r"^(\w+):\s*(.+)$", text.split("---")[1], re.M)) if text.startswith("---") else {}
        out.append({"name": meta.get("name", f.parent.name), "description": meta.get("description", ""),
                    "mtime": f.stat().st_mtime})
    return out


def entries() -> list[dict[str, Any]]:
    from app.services.commands import list_commands

    items: list[dict[str, Any]] = []
    for c in list_commands():
        is_pb = c["name"].startswith("playbook:")
        f = PLAYBOOK_DIR / f"{c['name'].split(':', 1)[1]}.yaml" if is_pb else None
        items.append({
            "kind": "playbook" if is_pb else "command", "name": c["name"], "title": c["title"],
            "description": c["description"], "risk": c["risk"],
            "how": f"propose_change(command='{c['name']}', params=…) — describe_command for the parameters; "
                   "runs only after an Editor submits, an Approver approves",
            **({"mtime": f.stat().st_mtime} if f and f.exists() else {}),
        })
    for name, spec in load_recipes().items():
        f = RECIPE_DIR / f"{name}.yaml"
        items.append({
            "kind": "recipe", "name": name, "title": str(spec.get("title", name)),
            "description": spec.get("description", ""),
            "params": {k: (v or {}).get("description", "") for k, v in (spec.get("params") or {}).items()},
            "how": f"run_recipe(name='{name}', params=…) — read-only, runs immediately",
            **({"mtime": f.stat().st_mtime} if f.exists() else {}),
        })
    for s in _skills():
        items.append({"kind": "skill", "name": s["name"], "title": s["name"], "description": s["description"],
                      "how": f"get_skill(name='{s['name']}') and follow it", "mtime": s["mtime"]})
    return items


_STOP = set("a an the of for to and or is are be my our with in on at whats which who how do does did it this "
             "that me show give list all any".split())


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "e", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def _words(s: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in _STOP and len(w) > 2}


def _score(query: str, e: dict[str, Any]) -> float:
    """Keyword overlap (name/title count double) blended with fuzzy match; runnable workflows win ties."""
    qw = _words(query)
    if not qw:
        return 0.0
    nt = _words(re.sub(r"[-_:]", " ", e["name"]) + " " + re.sub(r"\{\{.*?\}\}", "", e["title"]))
    de = _words(e["description"])
    keyword = (2 * len(qw & nt) + len(qw & de)) / (2 * len(qw)) * 100
    fuzzy = fuzz.token_set_ratio(query.lower(), f"{e['name']} {e['title']} {e['description']}".lower())
    bonus = 6 if e["kind"] in ("recipe", "playbook") else 3 if e["kind"] == "command" else 0
    return 0.7 * keyword + 0.3 * fuzzy + bonus


def search(query: str | None = None, kind: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
    items = [e for e in entries() if not kind or e["kind"] == kind]
    if not query:
        return items
    scored = sorted(((_score(query, e), e) for e in items), key=lambda t: -t[0])
    return [{**e, "score": round(s)} for s, e in scored[:limit] if s >= 25]
