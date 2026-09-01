# SPDX-License-Identifier: Apache-2.0
# ============================================================
# SKILLS ROUTER — /skills  (Postgres-native)
# ============================================================

import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel

from core.logger import logger
from auth.dependencies import get_current_user
from core.security_validation import validate_skill_request

router = APIRouter(tags=["skills"])


# ============================================================
# HELPERS
# ============================================================

def _row_to_dict(r) -> dict:
    return {
        "name":        r.name,
        "description": r.description or "",
        "code":        r.code or "",
        "tools":       r.tools or [],
        "tags":        r.tags or [],
        "examples":    [],
        "author":      r.created_by or "platform",
        "version":     "1.0.0",
        "status":      r.status or "PRODUCTION",
        "enabled":     r.is_production,
        "created_by":  r.created_by or "platform",
        "approved_by": r.approved_by or "",
        "visibility":  r.visibility or "public",
        "department":  r.department or "",
        "created_at":  r.created_at.isoformat() if r.created_at else None,
    }


# ============================================================
# PYDANTIC
# ============================================================

class SkillCreate(BaseModel):
    name: str
    description: str
    tools: List[str] = []
    tags: List[str] = []
    examples: List[str] = []
    author: str = "platform"
    version: str = "1.0.0"
    status: str = "DRAFT"
    created_by: str = ""
    code: str = ""


class SkillRun(BaseModel):
    message: str
    session_id: Optional[str] = None


# ============================================================
# ENDPOINTS
# ============================================================

@router.get("/skills")
def list_skills(current_user: dict = Depends(get_current_user)):
    from db.database import SessionLocal
    from db.models import SkillRecord
    from sqlalchemy import or_, and_
    from auth.rbac import is_admin
    uid  = current_user.get("sub")
    dept = current_user.get("department", "")
    db = SessionLocal()
    try:
        if is_admin(current_user):
            rows = db.query(SkillRecord).order_by(SkillRecord.name).all()
        else:
            rows = db.query(SkillRecord).filter(
                or_(
                    SkillRecord.created_by == uid,
                    SkillRecord.created_by.is_(None),           # legacy: no creator → visible to all
                    SkillRecord.visibility.is_(None),            # legacy: no visibility → visible to all
                    and_(SkillRecord.visibility == "public",  SkillRecord.status.in_(["APPROVED", "PRODUCTION"])),
                    and_(SkillRecord.visibility == "private", SkillRecord.department == dept),
                )
            ).order_by(SkillRecord.name).all()
        return {"skills": [_row_to_dict(r) for r in rows]}
    finally:
        db.close()

@router.get("/skills/proposals")
def list_skill_proposals(
        status: Optional[str] = None,
        current_user: dict = Depends(get_current_user),
):
    """Self-improving skill loop — audit trail of auto-synthesized skill proposals.

    Visible to approvers (admin OR can_approve / ad_level<=3). Non-approvers in a
    department see only their department's proposals; admins/approvers see all.
    The destructive approve/reject/promote actions are enforced separately by the
    governance router.
    """
    from auth.rbac import is_admin
    from store.skill_proposal_store import list_proposals

    _is_admin  = is_admin(current_user)
    _can_appr  = bool(current_user.get("can_approve")) or _is_admin
    if not _can_appr:
        # Scope non-approvers to their own department's proposals.
        dept = current_user.get("department") or ""
        if not dept:
            raise HTTPException(status_code=403, detail="Approver access required")
        proposals = list_proposals(status=status, department=dept)
    else:
        proposals = list_proposals(status=status)

    # Enrich SKILL_CREATED proposals with the live governance status of the
    # skill they produced, so the UI can render accurate approve/promote actions.
    _named = {p["skill_name"] for p in proposals if p.get("skill_name")}
    if _named:
        db = SessionLocal()
        try:
            rows = db.query(SkillRecord.name, SkillRecord.status).filter(
                SkillRecord.name.in_(_named)
            ).all()
            _status_map = {n: s for n, s in rows}
        finally:
            db.close()
        for p in proposals:
            if p.get("skill_name"):
                p["skill_status"] = _status_map.get(p["skill_name"])
    return {"proposals": proposals}

@router.post("/skills")
def create_skill(body: SkillCreate, current_user: dict = Depends(get_current_user)):
    # Validate and sanitize all inputs
    is_valid, field_errors, sanitized = validate_skill_request(body)
    if not is_valid:
        error_messages = []
        for field, errors in field_errors.items():
            for e in errors:
                error_messages.append(f"{field}: {e}")
        raise HTTPException(status_code=400, detail="; ".join(error_messages))

    from db.database import SessionLocal
    from db.models import SkillRecord
    db = SessionLocal()
    try:
        existing = db.query(SkillRecord).filter(SkillRecord.name == sanitized["name"]).first()
        if existing:
            raise HTTPException(status_code=409, detail=f"Skill '{sanitized['name']}' already exists")
        _uid     = str(current_user.get("sub") or current_user.get("id") or "")
        _display = current_user.get("name") or current_user.get("email") or _uid
        _dept    = current_user.get("department") or ""
        rec = SkillRecord(
            name=sanitized["name"],
            description=sanitized["description"],
            code=body.code or f"# Skill: {sanitized['name']}\ndef run(input: str) -> dict:\n    return {{'output': input}}",
            tools=body.tools,
            tags=sanitized["tags"],
            status="DRAFT",
            created_by=_display or sanitized["author"] or "platform",
            department=_dept,
            is_production=False,
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        logger.info(f"Skill created: {sanitized['name']} (DRAFT)")
        return {"success": True, "skill": _row_to_dict(rec)}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.put("/skills/{name}")
def update_skill(name: str, body: SkillCreate, current_user: dict = Depends(get_current_user)):
    # Validate and sanitize all inputs
    is_valid, field_errors, sanitized = validate_skill_request(body)
    if not is_valid:
        error_messages = []
        for field, errors in field_errors.items():
            for e in errors:
                error_messages.append(f"{field}: {e}")
        raise HTTPException(status_code=400, detail="; ".join(error_messages))

    from db.database import SessionLocal
    from db.models import SkillRecord
    db = SessionLocal()
    try:
        r = db.query(SkillRecord).filter(SkillRecord.name == name).first()
        if not r:
            raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
        r.name        = sanitized["name"]
        r.description = sanitized["description"]
        r.tools       = body.tools
        r.tags        = sanitized["tags"]
        r.version     = sanitized["version"]
        if body.code:
            r.code = body.code   # code field intentionally not sanitized — Python code
        db.commit()
        db.refresh(r)
        return {"success": True, "skill": _row_to_dict(r)}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.delete("/skills/{name}")
def delete_skill(name: str):
    from db.database import SessionLocal
    from db.models import SkillRecord
    db = SessionLocal()
    try:
        r = db.query(SkillRecord).filter(SkillRecord.name == name).first()
        if not r:
            raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
        db.delete(r)
        db.commit()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# ============================================================
# AINXT PLATFORM SKILLS — embedded, no external calls
# Seeded once at startup if not already present.
# ============================================================

_PLATFORM_SKILLS = [
    {
        "name": "code_review",
        "description": "Expert code review: identifies bugs, security vulnerabilities, performance issues, and style violations. Provides actionable, prioritised feedback with severity levels (CRITICAL / HIGH / MEDIUM / LOW).",
        "tags": ["review", "quality", "security", "ainxt"],
        "tools": [],
        "code": '''"""
Code Review Skill — AiNxt Platform
Reviews any code snippet or file for correctness, security, and maintainability.

System prompt injected at execution time:
"""
SYSTEM_PROMPT = """
You are an expert code reviewer with deep expertise in security, performance, and software engineering best practices.

When reviewing code, produce a structured report:

## Code Review Report

### Summary
<1–2 sentence overall assessment>

### Findings

| # | Severity | Category | Line(s) | Issue | Recommendation |
|---|----------|----------|---------|-------|----------------|
| 1 | CRITICAL  | Security | 42      | SQL injection via unsanitised input | Use parameterised queries |
…

Severity levels:
- CRITICAL: Security vulnerability or data loss risk — must fix before merge
- HIGH: Correctness bug or major performance issue — fix before merge
- MEDIUM: Code smell, missing error handling — fix in follow-up
- LOW: Style, naming, minor improvements — optional

### Security Checklist
- [ ] Input validation present
- [ ] No hardcoded secrets
- [ ] Auth/authz enforced
- [ ] SQL/command injection safe
- [ ] Error messages don't leak internals

### Performance Notes
<any O(n²) loops, N+1 queries, memory issues>

### Positive Observations
<what is done well>

### Overall Verdict
APPROVE | REQUEST_CHANGES | REJECT
"""

def run(input: str, model_router=None) -> dict:
    if model_router is None:
        from models.model_router import model_router
    prompt = SYSTEM_PROMPT + "\\n\\nCode to review:\\n\\n```\\n" + input + "\\n```"
    output = model_router.generate(prompt, model_hint="complex")
    return {"output": output, "skill": "code_review"}
''',
    },
    {
        "name": "debugging",
        "description": "Systematic debugging assistant: analyses error messages, stack traces, and code to identify root causes. Provides a step-by-step diagnosis and concrete fix.",
        "tags": ["debug", "troubleshoot", "errors", "ainxt"],
        "tools": [],
        "code": '''"""
Debugging Skill — AiNxt Platform
Diagnoses errors, exceptions, and unexpected behaviour in any codebase.
"""
SYSTEM_PROMPT = """
You are an expert debugging engineer. When given an error, stack trace, or unexpected behaviour, follow this systematic process:

## Debugging Report

### Error Classification
Type: <RuntimeError | LogicError | ConfigurationError | NetworkError | DataError | …>
Severity: <P0-Critical | P1-High | P2-Medium | P3-Low>

### Root Cause Analysis
<Explain exactly what is failing and why. Be precise about the line, function, or condition that triggers the error.>

### Evidence Trail
1. <First clue from the stack trace / logs>
2. <Second clue>
3. …

### Fix
```<language>
# Corrected code
```

### Why This Fix Works
<Explain the fix clearly>

### Prevention
<How to prevent this class of bug in future: defensive coding, tests, linting rules>

### Related Issues to Check
<Any other parts of the codebase that might have the same bug>
"""

def run(input: str, model_router=None) -> dict:
    if model_router is None:
        from models.model_router import model_router
    prompt = SYSTEM_PROMPT + "\\n\\nProblem to debug:\\n\\n" + input
    output = model_router.generate(prompt, model_hint="complex")
    return {"output": output, "skill": "debugging"}
''',
    },
    {
        "name": "documentation",
        "description": "Generates comprehensive, accurate documentation for code: docstrings, API docs, README sections, and architecture descriptions. Follows Google/NumPy/JSDoc style.",
        "tags": ["docs", "docstring", "api", "readme", "ainxt"],
        "tools": [],
        "code": '''"""
Documentation Skill — AiNxt Platform
Generates docstrings, API documentation, and README content from code.
"""
SYSTEM_PROMPT = """
You are a technical writer and documentation expert. Generate clear, accurate, complete documentation for the provided code.

Output format depends on what is provided:

### For a function / method:
Produce a docstring in Google style:
```python
def example(param: str) -> dict:
    \"\"\"One-sentence summary.

    Longer description if needed.

    Args:
        param: Description of param.

    Returns:
        dict: Description of return value.
            key (type): Description.

    Raises:
        ValueError: When param is empty.

    Example:
        >>> example("hello")
        {"result": "hello"}
    \"\"\"
```

### For a class:
Include class docstring + docstrings for all public methods.

### For an API endpoint:
Produce OpenAPI-style documentation:
- Method and path
- Summary and description
- Request body schema (with types and constraints)
- Response schemas (200, 4xx, 5xx)
- Example request / response

### For a module / file:
Produce a module-level docstring describing:
- Purpose
- Key exports
- Usage example

Always be accurate — only document what the code actually does.
"""

def run(input: str, model_router=None) -> dict:
    if model_router is None:
        from models.model_router import model_router
    prompt = SYSTEM_PROMPT + "\\n\\nCode to document:\\n\\n```\\n" + input + "\\n```"
    output = model_router.generate(prompt, model_hint="complex")
    return {"output": output, "skill": "documentation"}
''',
    },
    {
        "name": "architecture_analysis",
        "description": "Analyses software architecture: evaluates design patterns, coupling, cohesion, scalability, and security posture. Produces an ADR-style report with actionable recommendations.",
        "tags": ["architecture", "design", "adr", "patterns", "ainxt"],
        "tools": [],
        "code": '''"""
Architecture Analysis Skill — AiNxt Platform
Deep analysis of software architecture, design patterns, and system design.
"""
SYSTEM_PROMPT = """
You are a senior software architect with expertise in distributed systems, microservices, cloud-native patterns, and enterprise architecture.

Analyse the provided architecture description, diagram, or codebase and produce a structured Architecture Review Report.

## Architecture Review Report

### Executive Summary
<2–3 sentences on overall architectural health>

### Architecture Overview
- **Pattern**: <Monolith | Microservices | Event-driven | Serverless | Hexagonal | …>
- **Key Components**: <list with responsibilities>
- **Data Flow**: <how data moves through the system>
- **External Dependencies**: <third-party services, databases, queues>

### Strengths
1. <what is well-designed>
2. …

### Risks & Issues

| Priority | Component | Issue | Impact | Recommendation |
|----------|-----------|-------|--------|----------------|
| HIGH | Auth Service | Single point of failure | Outage risk | Add replica + health check |
…

### Scalability Assessment
- **Horizontal scaling**: <possible | constrained | blocked> — reason
- **Bottlenecks**: <identify DB, queue, or service bottlenecks>
- **Estimated load capacity**: <rough estimate if inferable>

### Security Posture
- Auth/Authz: <assessment>
- Network: <assessment>
- Data encryption: <at rest | in transit>
- Secrets management: <assessment>

### Recommendations (Prioritised)
1. [CRITICAL] <action> — <why>
2. [HIGH] <action> — <why>
…

### Architecture Decision Records (ADRs)
Suggest 1–3 ADRs for key decisions that should be documented.
"""

def run(input: str, model_router=None) -> dict:
    if model_router is None:
        from models.model_router import model_router
    prompt = SYSTEM_PROMPT + "\\n\\nArchitecture to analyse:\\n\\n" + input
    output = model_router.generate(prompt, model_hint="complex")
    return {"output": output, "skill": "architecture_analysis"}
''',
    },
]


def seed_platform_skills():
    """
    Idempotent seed: insert AiNxt platform skills if they don't exist.
    Called once at gateway startup.
    """
    from db.database import SessionLocal
    from db.models import SkillRecord
    db = SessionLocal()
    try:
        for skill_def in _PLATFORM_SKILLS:
            existing = db.query(SkillRecord).filter(
                SkillRecord.name == skill_def["name"]
            ).first()
            if existing:
                continue   # already seeded — skip
            rec = SkillRecord(
                name=skill_def["name"],
                description=skill_def["description"],
                code=skill_def["code"],
                tools=skill_def["tools"],
                tags=skill_def["tags"],
                status="PRODUCTION",
                is_production=True,
                created_by="ainxt-platform",
                approved_by="system",
            )
            db.add(rec)
            logger.info(f"Seeded AiNxt platform skill: {skill_def['name']}")
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning(f"seed_platform_skills: {exc}")
    finally:
        db.close()


class SkillGenerate(BaseModel):
    description: str          # plain-English description of what the skill should do
    name:        str          # desired skill name (snake_case)
    department:  str = ""
    visibility:  str = "public"
    auto_save:   bool = True  # save as PRODUCTION immediately


@router.post("/skills/generate")
def generate_skill(body: SkillGenerate, current_user: dict = Depends(get_current_user)):
    """
    Natural-language skill creation.
    Describe what the skill should do → LLM writes the Python code → saved as PRODUCTION.
    No Python knowledge required from the user.
    """
    from services.skill_synthesis import synthesize_skill

    _uid  = str(current_user.get("sub") or current_user.get("id") or "")
    _dept = current_user.get("department") or body.department or ""
    _skill_type = body.skill_type if body.skill_type in ("execution", "behavioral") else "execution"

    try:
        _syn = synthesize_skill(body.name, body.description, _skill_type, _dept)
        code = _syn["code"]
        if not body.auto_save:
            return {"success": True, "code": code, "saved": False}

        from db.database import SessionLocal
        from db.models import SkillRecord
        db = SessionLocal()
        try:
            existing = db.query(SkillRecord).filter(SkillRecord.name == body.name).first()
            if existing:
                existing.code        = code
                existing.description = body.description
                existing.status      = "PRODUCTION"
                existing.is_production = True
                db.commit()
                db.refresh(existing)
                return {"success": True, "code": code, "saved": True, "skill": _row_to_dict(existing)}
            rec = SkillRecord(
                name=body.name,
                description=body.description,
                code=code,
                tools=[],
                tags=["nl-generated", _dept] if _dept else ["nl-generated"],
                status="PRODUCTION",
                created_by=_uid or "platform",
                department=_dept,
                is_production=True,
                visibility=body.visibility,
            )
            db.add(rec)
            db.commit()
            db.refresh(rec)
            logger.info(f"NL skill generated + saved: {body.name}")
            return {"success": True, "code": code, "saved": True, "skill": _row_to_dict(rec)}
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            db.close()

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Skill generation failed: {e}")


@router.post("/skills/{name}/test")
def test_skill(name: str, body: SkillRun):
    from db.database import SessionLocal
    from db.models import SkillRecord
    db = SessionLocal()
    try:
        r = db.query(SkillRecord).filter(SkillRecord.name == name).first()
        if not r:
            raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
        if r.status not in ("PRODUCTION", "APPROVED"):
            raise HTTPException(
                status_code=403,
                detail=f"Skill '{name}' is in '{r.status}' state. Only PRODUCTION or APPROVED skills can be tested."
            )
    finally:
        db.close()

    try:
        from models.model_router import model_router
        answer = model_router.generate(body.message)
        return {"success": True, "answer": answer, "skill": name}
    except Exception as e:
        return {"success": False, "error": str(e)}
