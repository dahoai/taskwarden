"""Agents as folders: `<agents>/<id>/AGENT.md` and `<agents>/<id>/skills/<name>/SKILL.md`.

An agent is its core role (AGENT.md) plus one skill per job, each with its own words, tools and
evals. The app turns a folder into its own agent definition; this module only reads and checks
files. A bad folder fails when it is loaded, never at the first task that reads it. Whether the
tool names exist is the app's check: only it knows its tools.

`business_types` in AGENT.md is a list, null (every type), or the name of a group the app
expands; `load_agent(folder, groups=...)` names the groups it accepts.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .triggers import parse as parse_trigger

_AGENT_KEYS = {"id", "name", "title", "aliases", "business_types", "tools", "skills", "agenda",
               "skills_on_demand", "triggers"}
_SKILL_KEYS = {"name", "description", "tools", "requires", "evals"}
_AGENDA_KEYS = {"cadence", "weekday", "goal", "skills"}

USE_SKILL = "use_skill"
SKILLS_HINT = ("Your skills: each is how you do one job. Before you do that job, load it with "
               "use_skill; it gives you its instructions and its tools. Load the ones the task "
               "needs, all in one response.")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    tools: tuple[str, ...]
    text: str
    evals: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentFolder:
    id: str
    name: str
    title: str
    core: str
    tools: tuple[str, ...]          # the core's own; see all_tools
    skills: tuple[Skill, ...]
    aliases: tuple[str, ...] = ()
    # None = every type; a list; or a named group the roster expands ("field_work").
    business_types: Optional[tuple[str, ...] | str] = None
    agenda: Optional[dict] = None   # {cadence, weekday, goal, skills: preloaded}
    # Step 2: the prompt lists the skills and use_skill loads one. Off: every skill is in
    # the role (step 1). Switched on per agent, only when its evals hold.
    on_demand: bool = False
    # What wakes this agent besides the owner (taskwarden.triggers): checked across the whole
    # team by triggers.of_folders, since a `done` chain spans agents.
    triggers: tuple = ()

    @property
    def role(self) -> str:
        """Step 1: every skill is always loaded, the core first, then the skills in order."""
        return " ".join([self.core, *(s.text for s in self.skills)])

    @property
    def all_tools(self) -> tuple[str, ...]:
        return self.tools_for({s.name for s in self.skills})

    def tools_for(self, loaded: set[str]) -> tuple[str, ...]:
        """The core's tools, then each loaded skill's, in skill order, once each."""
        seen: dict[str, None] = dict.fromkeys(self.tools)
        for skill in self.skills:
            if skill.name in loaded:
                seen.update(dict.fromkeys(skill.tools))
        return tuple(seen)

    def prompt(self, loaded: set[str]) -> str:
        """The role an on-demand agent starts with: its core, the skills already loaded, and
        one line for each of the others."""
        rest = [s for s in self.skills if s.name not in loaded]
        parts = [self.core, *(s.text for s in self.skills if s.name in loaded)]
        if rest:
            parts.append(SKILLS_HINT + " " + " ".join(f"{s.name}: {s.description}"
                                                         for s in rest))
        return " ".join(parts)

    def skill(self, name: str) -> Optional[Skill]:
        return next((s for s in self.skills if s.name == name), None)

    def use_skill_schema(self, loaded: set[str]) -> Optional[dict]:
        names = [s.name for s in self.skills if s.name not in loaded]
        if not (self.on_demand and names):
            return None
        return {"type": "function", "function": {
            "name": USE_SKILL,
            "description": "Load one of your skills: its instructions and its tools.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "enum": names}}, "required": ["name"]}}}


def _read(path: Path, keys: set[str]) -> tuple[dict, str]:
    """(frontmatter, body as one line of text). The body is wrapped for reading; the words
    are what counts, so line breaks become spaces."""
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise ValueError(f"{path}: starts with a --- frontmatter block")
    head, _, body = raw[4:].partition("\n---\n")
    meta = yaml.safe_load(head) or {}
    if not isinstance(meta, dict):
        raise ValueError(f"{path}: frontmatter is a mapping")
    if unknown := set(meta) - keys:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}")
    text = " ".join(body.split())
    if not text:
        raise ValueError(f"{path}: has no instructions")
    return meta, text


def _names(path: Path, meta: dict, key: str) -> tuple[str, ...]:
    value = meta.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"{path}: {key} is a list of names")
    return tuple(value)


def load_skill(path: Path) -> Skill:
    meta, text = _read(path, _SKILL_KEYS)
    if meta.get("name") != path.parent.name:
        raise ValueError(f"{path}: name must be {path.parent.name!r}")
    if not (meta.get("description") or "").strip():
        raise ValueError(f"{path}: needs a description")
    return Skill(name=meta["name"], description=" ".join(meta["description"].split()),
                 tools=_names(path, meta, "tools"), text=text, evals=_names(path, meta, "evals"))


def load_agent(folder: Path, groups: tuple[str, ...] = ()) -> AgentFolder:
    path = folder / "AGENT.md"
    meta, core = _read(path, _AGENT_KEYS)
    if meta.get("id") != folder.name:
        raise ValueError(f"{path}: id must be {folder.name!r}")
    for key in ("name", "title"):
        if not (meta.get(key) or "").strip():
            raise ValueError(f"{path}: needs a {key}")
    skills = tuple(load_skill(folder / "skills" / name / "SKILL.md")
                   for name in _names(path, meta, "skills"))
    agenda = meta.get("agenda")
    if agenda is not None:
        if not isinstance(agenda, dict) or set(agenda) - _AGENDA_KEYS:
            raise ValueError(f"{path}: agenda has cadence, goal and optionally weekday and skills")
        cadence, weekday = agenda.get("cadence"), agenda.get("weekday", 0)
        if cadence not in ("daily", "weekly"):
            raise ValueError(f"{path}: agenda cadence is daily or weekly")
        if "weekday" in agenda and cadence != "weekly":
            raise ValueError(f"{path}: agenda weekday is for weekly schedules")
        if not isinstance(weekday, int) or isinstance(weekday, bool) or not 0 <= weekday <= 6:
            raise ValueError(f"{path}: agenda weekday is 0 (Monday) to 6")
        if not isinstance(agenda.get("goal"), str) or not agenda["goal"].strip():
            raise ValueError(f"{path}: agenda needs a goal")
        skills_list = agenda.get("skills") or []
        if not isinstance(skills_list, list) or not all(isinstance(s, str) for s in skills_list):
            raise ValueError(f"{path}: agenda skills is a list of names")
        if unknown := set(skills_list) - {s.name for s in skills}:
            raise ValueError(f"{path}: agenda preloads unknown skills {sorted(unknown)}")
    raw_triggers = meta.get("triggers") or []
    if not isinstance(raw_triggers, list):
        raise ValueError(f"{path}: triggers is a list")
    try:
        triggers = tuple(parse_trigger(meta["id"], t) for t in raw_triggers)
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from None
    types = meta.get("business_types")
    if isinstance(types, str):
        if types not in groups:
            raise ValueError(f"{path}: business_types is a list, null or one of {groups}")
    elif isinstance(types, list):
        if not all(isinstance(t, str) and t for t in types):
            raise ValueError(f"{path}: business_types is a list of names")
    elif types is not None:
        raise ValueError(f"{path}: business_types is a list, null or one of {groups}")
    on_demand = meta.get("skills_on_demand", False)
    if not isinstance(on_demand, bool):
        raise ValueError(f"{path}: skills_on_demand is true or false")
    return AgentFolder(
        id=meta["id"], name=meta["name"], title=meta["title"], core=core,
        tools=_names(path, meta, "tools"), skills=skills,
        aliases=_names(path, meta, "aliases"),
        business_types=tuple(types) if isinstance(types, list) else types,
        agenda={**agenda, "goal": " ".join(agenda["goal"].split())} if agenda else None,
        on_demand=on_demand, triggers=triggers)
