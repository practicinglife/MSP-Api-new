---
name: msp-library
description: The org workflow library — find and reuse saved recipes, playbooks, commands and skills before building anything multi-step, and save new ones (formats + rules) so nobody rebuilds the same thing twice. Use whenever a request takes several steps, or the user says "save this", "make this a workflow/skill/recipe", "remember how to do this".
---

# Workflow library — build once, reuse everywhere

## 1. Always look first
For any request needing more than one or two tool calls: `find_workflow("<request in plain words>")`.

| Match kind | Use it with | Notes |
|---|---|---|
| recipe | `run_recipe(name, params)` | read-only, immediate; follow its `how_to_present` |
| playbook / command | `propose_change(name, params)` (`describe_command` for params) | a change: an Editor submits, an Approver approves |
| skill | `get_skill(name)` and follow it | how we do things |

Browse everything: `find_workflow()` with no query, or the page `/library` on the MSP Tools server.

## 2. Offer to save what you had to work out
If nothing matched and the answer took several tool calls or a judgment call that others will need again,
finish the request, then ask: **"Save this to the library so the team gets it next time?"** Don't save one-offs.

Pick the kind:
- **Recipe** — the same READ steps every time (a report, a status check). Most common.
- **Playbook** — several existing change commands that always go together. Can't add new vendor calls; if the
  request needs a vendor ability no command has, say a new command is needed (developer work) instead.
- **Skill** — knowledge/judgment: when to do what, rules, gotchas.

Save with `save_workflow(kind, name, content)` → returns an approval link (an Editor submits, an Approver approves).
It's validated before proposing; fix and retry if refused. To change an existing one, save the full new
content under the same name (the preview shows the diff). `delete: true` removes it.

## Formats
**Recipe** (`name` kebab-case; steps may use only read tools — `find_workflow(kind='command')` doesn't list
them; they are the msp read tools such as get_client, client_devices, list_clients, search_tickets,
naming_report, snapshot_diff):
```yaml
name: client-health
title: "Health check: {{client}}"
description: When to use it — the words people say.
params:
  client: {type: string, description: Client id or code}
steps:
  - tool: get_client
    args: {client: "{{client}}"}
    as: client                       # name for later steps: {{steps.client.psa_company_id}}
    pick: [client_code, compliance]  # optional: keep only these paths; * maps lists (clients.*.client_code)
  - tool: search_tickets
    args: {conditions: "closedFlag=false and company/id={{steps.client.psa_company_id}}"}
    as: open_tickets
    when: steps.client.psa_company_id   # optional: skip unless truthy
    optional: true                      # optional: a failure is reported, not fatal
present: |
  How to summarise the result for the user.
```
**Playbook:**
```yaml
name: standardize-client
title: Standardize {{client}}
description: …
params:
  client: {type: string, description: Client id or code}
steps:
  - command: apply_standard_name
    params: {client: "{{client}}"}
  - command: fix_psa_endpoint_ids
    params: {client: "{{client}}"}
    optional: true     # skipped with a note when it has nothing to do
```
**Skill:** Markdown starting with frontmatter `name:` (= the name) and `description:` (what it's for and the
phrases that should trigger it), then concise instructions.

## Good practice
- Names describe the outcome: `client-health`, `offboard-device`, `msp-offboarding`.
- Descriptions carry the words people actually say — that's how `find_workflow` finds them.
- Keep recipes small (≤ 6 steps) and use `pick` so results stay readable.
