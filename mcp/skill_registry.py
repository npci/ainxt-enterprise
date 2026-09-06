# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt MCP SKILL REGISTRY  (Phase 10)
# Registry for higher-level composed skills.
#
# A "skill" is a named, reusable capability composed of:
#   - an ordered list of tool names (tool_registry keys), OR
#   - a workflow definition (workflows/engine.py Workflow), OR
#   - a direct callable (for simple wrapping)
#
# Skills are the building blocks that agent_builder.py will
# use when engineers create new agents on the platform.
#
# Usage:
#   skill_registry.register(SkillDefinition(...))
#   skill_registry.get("fix_bug")
#   skill_registry.discover(tag="engineering")
# ============================================================

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.logger import logger


# ============================================================
# SKILL DEFINITION
# ============================================================

@dataclass
class SkillDefinition:
    """
    Metadata for a reusable platform skill.

    Fields:
        name          Unique skill name (snake_case).
        description   What the skill does.
        tools         Ordered list of tool names from tool_registry
                      that this skill calls in sequence.
        workflow_name If set, the skill executes via the WorkflowEngine
                      using this workflow name.
        fn            Optional direct callable (highest priority when set).
        tags          Discovery tags.
        input_schema  JSON schema for expected input.
        version       Semver string.
        author        Who registered the skill.
        enabled       Disabled skills are hidden from discovery.
        examples      Example inputs (for documentation / agent planning).
    """
    name:          str
    description:   str
    tools:         List[str]        = field(default_factory=list)
    workflow_name: Optional[str]    = None
    fn:            Optional[Callable] = None
    tags:          List[str]        = field(default_factory=list)
    input_schema:  Dict[str, Any]   = field(default_factory=dict)
    version:       str              = "1.0.0"
    author:        str              = "platform"
    enabled:       bool             = True
    examples:      List[str]        = field(default_factory=list)


# ============================================================
# SKILL REGISTRY
# ============================================================

class SkillRegistry:
    """
    In-memory registry for platform skills.
    """

    def __init__(self):
        self._skills: Dict[str, SkillDefinition] = {}
        logger.info("SkillRegistry initialised")

    # --------------------------------------------------------
    # REGISTER
    # --------------------------------------------------------

    def register(self, skill: SkillDefinition) -> None:
        if skill.name in self._skills:
            logger.warning(
                f"SkillRegistry: overwriting existing skill {skill.name!r}"
            )
        self._skills[skill.name] = skill
        logger.info(
            f"SkillRegistry: registered {skill.name!r} "
            f"tags={skill.tags} tools={skill.tools}"
        )

    def unregister(self, name: str) -> bool:
        if name in self._skills:
            del self._skills[name]
            logger.info(f"SkillRegistry: unregistered {name!r}")
            return True
        return False

    def enable(self, name: str) -> None:
        self._get_or_raise(name).enabled = True

    def disable(self, name: str) -> None:
        self._get_or_raise(name).enabled = False

    # --------------------------------------------------------
    # QUERY
    # --------------------------------------------------------

    def get(self, name: str) -> Optional[SkillDefinition]:
        return self._skills.get(name)

    def list_all(self, enabled_only: bool = True) -> List[SkillDefinition]:
        skills = list(self._skills.values())
        if enabled_only:
            skills = [s for s in skills if s.enabled]
        return skills

    def discover(
        self,
        tag: Optional[str] = None,
        query: Optional[str] = None,
    ) -> List[SkillDefinition]:
        """Find skills by tag and/or keyword in name/description."""
        results = self.list_all()

        if tag:
            results = [s for s in results if tag.lower() in s.tags]

        if query:
            q = query.lower()
            results = [
                s for s in results
                if q in s.name.lower() or q in s.description.lower()
            ]

        return results

    def names(self) -> List[str]:
        return list(self._skills.keys())

    # --------------------------------------------------------
    # HELPERS
    # --------------------------------------------------------

    def _get_or_raise(self, name: str) -> SkillDefinition:
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError(f"Skill {name!r} not found")
        return skill

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: str) -> bool:
        return name in self._skills


# ============================================================
# SINGLETON
# ============================================================

skill_registry = SkillRegistry()
