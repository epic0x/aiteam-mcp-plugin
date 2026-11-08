"""Brief templates: how the coordinator writes a request for each kind of team, kept as editable YAML, not code.

Bundled defaults live next to this file in ``briefs/*.yaml``: ``core.yaml`` (labels, the four parts every brief
carries, how to ask an expert, the end-of-conversation recap) plus one file per team type (engineering, design,
marketing, video, ops, research, sales, general).

Owners change them without touching code: a folder named by env ``AITEAM_BRIEFS_DIR`` (else ``<HERMES_HOME>/briefs``)
is read on every call. A file there with a bundled name replaces that file; a new name adds a team. A file that does
not parse or breaks the shape below is skipped and reported in the result, so one bad edit never hides the rest.

Tool ``brief_templates`` (read-only, free): no arguments = the core rules + the list of teams; ``team`` = that team's
template; ``team`` + ``label`` = a ready skeleton to fill in.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from typing import Any, Optional

import mcp.types as types
import yaml

BUNDLED = pathlib.Path(__file__).resolve().parent / "briefs"
LABEL_IDS = ("new-feature", "improvement", "bug")
ID = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
MAX_FILE = 64 * 1024


def override_dir() -> Optional[pathlib.Path]:
    d = os.environ.get("AITEAM_BRIEFS_DIR", "").strip()
    if d:
        return pathlib.Path(d)
    home = os.environ.get("HERMES_HOME", "").strip()
    return pathlib.Path(home) / "briefs" if home else None


def _str(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _fields_ok(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(f, dict) and _str(f.get("key")) and _str(f.get("ask")) for f in v)


def check_core(d: Any) -> list[str]:
    if not isinstance(d, dict):
        return ["not a mapping"]
    err = []
    labels = d.get("labels")
    if not isinstance(labels, list) or [x.get("id") for x in labels if isinstance(x, dict)] != list(LABEL_IDS) \
            or not all(_str(x.get("title")) for x in labels):
        err.append(f"labels: need exactly {', '.join(LABEL_IDS)} in that order, each with a title")
    parts = d.get("parts")
    if not isinstance(parts, list) or not all(isinstance(p, dict) and _str(p.get("key")) and _str(p.get("heading"))
                                              and _str(p.get("ask")) for p in parts) or len(parts) < 1:
        err.append("parts: a list of {key, heading, ask}")
    if not isinstance(d.get("ask_expert"), dict) or not _str(d["ask_expert"].get("format")):
        err.append("ask_expert.format: required")
    return err


def check_team(d: Any, stem: str) -> list[str]:
    if not isinstance(d, dict):
        return ["not a mapping"]
    err = []
    if d.get("id") != stem or not ID.match(str(d.get("id"))):
        err.append(f"id: must be {stem!r} (the file name), lowercase letters, digits and dashes")
    if not _str(d.get("name")):
        err.append("name: required")
    if not isinstance(d.get("use_for"), list) or not all(_str(x) for x in d["use_for"]):
        err.append("use_for: a list of words")
    st = d.get("standards")
    if not isinstance(st, list) or not st or not all(isinstance(s, dict) and _str(s.get("name")) for s in st):
        err.append("standards: a list of {name, url}")
    f = d.get("fields")
    if not isinstance(f, dict) or not all(k in ("all",) + LABEL_IDS for k in f) or not all(_fields_ok(v) for v in f.values()):
        err.append(f"fields: keys all / {' / '.join(LABEL_IDS)}, each a list of {{key, ask}}")
    return err


def _read(p: pathlib.Path) -> tuple[Any, Optional[str]]:
    try:
        if p.stat().st_size > MAX_FILE:
            return None, f"larger than {MAX_FILE // 1024} KB"
        return yaml.safe_load(p.read_text(encoding="utf-8")), None
    except (OSError, UnicodeDecodeError) as e:
        return None, f"unreadable ({type(e).__name__})"
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        return None, "YAML error" + (f" at line {mark.line + 1}" if mark else "")


def load(extra: Optional[pathlib.Path] = None) -> dict:
    """{core, teams: {id: dict}, source: {name: bundled|custom}, problems: [str]}. Never raises on a bad custom file."""
    files = {p.name: p for p in sorted(BUNDLED.glob("*.yaml"))}
    source = {n: "bundled" for n in files}
    extra = extra if extra is not None else override_dir()
    if extra and extra.is_dir():
        for p in sorted(extra.glob("*.yaml")):
            files[p.name], source[p.name] = p, "custom"
    problems: list[str] = []
    core, teams = None, {}
    for name, p in files.items():
        d, why = _read(p)
        stem = p.stem
        errs = [why] if why else (check_core(d) if stem == "core" else check_team(d, stem))
        if errs and source[name] == "custom":
            problems.append(f"{name} (custom) skipped: " + "; ".join(errs))
            bp = BUNDLED / name
            if not bp.is_file():
                continue
            d, source[name] = _read(bp)[0], "bundled"
        elif errs:
            problems.append(f"{name} (bundled) is invalid: " + "; ".join(errs))
            continue
        if stem == "core":
            core = d
        else:
            teams[stem] = d
    return {"core": core, "teams": teams, "source": source, "problems": problems}


def skeleton(core: dict, team: dict, label: str) -> str:
    """A ready-to-fill brief: label + title line, the four parts, then the team's fields for that label."""
    title = next(x["title"] for x in core["labels"] if x["id"] == label)
    lines = [f"{title}: <short title>", ""]
    for part in core["parts"]:
        lines += [f"**{part['heading']}:** <{part['ask']}>"]
    fields = (team.get("fields") or {}).get("all", []) + (team.get("fields") or {}).get(label, [])
    if fields:
        lines += ["", "**Details:**"] + [f"- {f['key']}: <{f['ask']}>" for f in fields]
    lines += ["", "<@expert_id> <what you need from them>", core.get("signature") or "— <owner> from <app>"]
    return "\n".join(lines)


def brief_templates_tool() -> types.Tool:
    return types.Tool(
        name="brief_templates", title="Read the brief templates",
        description="How to write a request to the team, from the owner's editable templates. No arguments: the "
                    "labels (New feature request, Improvement, Bug found), the four parts every brief carries (why, "
                    "goal, what needs to be done, what I expect back), how to ask an expert and the list of teams. "
                    "With team: that team's fields and standard. With team and label: a ready skeleton to fill in. "
                    "Call it before you send a request to anyone.",
        inputSchema={"type": "object", "additionalProperties": False, "properties": {
            "team": {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,40}$",
                     "description": "Team type id from the list, e.g. engineering, marketing, video."},
            "label": {"type": "string", "enum": list(LABEL_IDS),
                      "description": "new-feature, improvement or bug. Needs team."}}},
        annotations=types.ToolAnnotations(title="Read the brief templates", readOnlyHint=True, destructiveHint=False,
                                          idempotentHint=True, openWorldHint=False))


def _result(data: dict, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=1))],
                                structuredContent=data, isError=is_error)


def call(args: dict, extra: Optional[pathlib.Path] = None) -> types.CallToolResult:
    args = {k: v for k, v in (args or {}).items() if k != "son"}
    team_id, label = args.get("team"), args.get("label")
    if label is not None and label not in LABEL_IDS:
        return _result({"error": f"label must be one of {', '.join(LABEL_IDS)}"}, True)
    if label and not team_id:
        return _result({"error": "label needs team (use team general when no other fits)"}, True)
    t = load(extra)
    if t["core"] is None:
        return _result({"error": "core.yaml is missing or invalid", "problems": t["problems"]}, True)
    out: dict = {}
    if t["problems"]:
        out["problems"] = t["problems"]
    if not team_id:
        out.update(core=t["core"], teams=[{"id": k, "name": v.get("name"), "use_for": v.get("use_for")}
                                          for k, v in sorted(t["teams"].items())],
                   edit="Templates are YAML files. Override any of them, or add a team, in the briefs folder "
                        "(env AITEAM_BRIEFS_DIR, else <HERMES_HOME>/briefs).")
        return _result(out)
    team = t["teams"].get(team_id)
    if team is None:
        return _result({"error": f"unknown team {team_id!r}", "teams": sorted(t["teams"])}, True)
    out["team"] = team
    out["source"] = t["source"].get(f"{team_id}.yaml")
    if label:
        out["skeleton"] = skeleton(t["core"], team, label)
    return _result(out)
