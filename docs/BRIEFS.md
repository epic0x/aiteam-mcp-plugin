# Brief templates

Every request the assistant sends to the team is a brief: one label, a short title, then four parts.

- **Labels:** `New feature request`, `Improvement`, `Bug found`.
- **Parts:** Why, The goal, What needs to be done, What I expect back.
- **The experts decide how.** A brief says what and why, never the stack or the method, unless the owner set it.
- **Asking an expert:** one question by name, plus what the assistant will assume until they answer. It keeps going.
- **End of a conversation:** every request and decision becomes a brief, a board task or a follow-up.

## Where they live

Defaults ship with the connector in `connector/aiteam_connector/briefs/`:

| File | What it holds |
| --- | --- |
| `core.yaml` | labels, the four parts, how to ask an expert, the recap rule, the signature |
| `engineering.yaml` | user story + acceptance criteria; bug report (steps, expected, actual, severity) |
| `design.yaml` | design brief: audience, constraints, references, success |
| `marketing.yaml` | creative brief: objective, audience, key message, channels, mandatories, budget, KPI |
| `video.yaml` | video brief: purpose, length and format, script, tone, assets, deliverables, review rounds |
| `ops.yaml` | change request / incident: system, impact, risk and rollback, window, cost |
| `research.yaml` | research brief: question, decision, scope, sources, format |
| `sales.yaml` | customer handoff: customer, context, next step, limits |
| `general.yaml` | SMART goal + RACI for anything else |

Each team file names the standard it follows, with a link.

## Changing them

No code change needed. Put YAML files in your briefs folder:

- `AITEAM_BRIEFS_DIR` if set, else `<HERMES_HOME>/briefs`.
- Same file name as a bundled one = replaces it (for example your own `engineering.yaml`).
- New file name = a new team (for example `legal.yaml` with `id: legal`).
- Files are read on every call: no restart.
- A file that does not parse, or breaks the shape, is skipped and listed under `problems` in the tool result. The
  bundled file stays in use. `core.yaml` must keep the three labels, in order.

Team file shape:

```yaml
id: legal                 # same as the file name
name: Legal
use_for: [contract, nda, terms]
standards:
  - name: Our contract checklist
    url: https://example.com/checklist
fields:
  all:                    # every label
    - key: Counterparty
      ask: Who signs.
  new-feature: []
  improvement: []
  bug:
    - key: Clause
      ask: Which clause is wrong and why.
review: Nothing is signed without the owner.
```

## The tool

`brief_templates` (read-only, free):

- no arguments: the core rules and the list of teams;
- `team`: that team's template and whether it is bundled or custom;
- `team` + `label` (`new-feature`, `improvement`, `bug`): a skeleton to fill in.
