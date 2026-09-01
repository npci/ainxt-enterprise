# SPDX-License-Identifier: Apache-2.0
"""
workers/graph_edges.py — WS-6: tree-sitter (AST) graph-edge extraction.

See docs/planning/SDLC_AGENTIC_LOOP_RFD.md §4 WS-6.

WHY
---
`code_graph` import/extends/implements/call edges are extracted by per-language
REGEX in workers/index_worker.py. Regex misses and mis-attributes edges —
especially call edges and multi-line / aliased imports. WS-6 adds an AST path
that resolves those edges from the parsed tree instead.

DESIGN — AST node-type traversal, NOT `.scm` text queries
---------------------------------------------------------
We deliberately walk node TYPES (the same mechanism `_ts_chunk` / `_DEFINITION_NODES`
already use) rather than tree-sitter `Query`/`.scm` strings. Node `.type` / `.children`
/ `.start_byte` are stable across every tree-sitter binding version, whereas the
Query API changed shape between versions (old `language.query(...)`/list-of-tuples
vs new `Query(lang, scm)` + `QueryCursor.captures(...)`/dict). Avoiding queries keeps
this working identically on the server's `tree_sitter_languages` and on a dev box.

CONTRACT
--------
* Edge JSONB shape is UNCHANGED: `[{type, target_name, target_file}]`, target_file
  resolved downstream exactly as for the regex path. No migration, no
  graph_resolver / 03_tables.sql change.
* Flag-gated: `SDLC_GRAPH_EDGE_MODE` default `regex`. `treesitter` is opt-in and
  requires a re-index to populate (the slice consumer is unaffected).
* Per-file / per-language fallback: any missing grammar, parse failure, or empty
  capture for a file falls back to the regex extractor for that file (the AST path
  can only ADD coverage, never strand a language). The caller (index_worker) owns
  the fallback; this module returns None to signal "use regex".
* Fully unit-testable on Windows: the extractor takes an injected `parser`, so a
  test passes a real tree-sitter parser (any binding) and asserts the edge sets.
"""

from __future__ import annotations

import os
from typing import Optional


def graph_edge_mode() -> str:
    """`regex` (default) | `treesitter`. Read at call time."""
    m = os.getenv("SDLC_GRAPH_EDGE_MODE", "regex").strip().lower()
    return m if m in ("regex", "treesitter") else "regex"


# ── per-language AST node types ──────────────────────────────────────────────

_IMPORT_NODES = {
    "python":     {"import_statement", "import_from_statement"},
    "java":       {"import_declaration"},
    "javascript": {"import_statement"},
    "typescript": {"import_statement"},
    "tsx":        {"import_statement"},
    "go":         {"import_declaration", "import_spec"},
}

_CLASS_NODES = {
    "python":     {"class_definition"},
    "java":       {"class_declaration", "interface_declaration", "enum_declaration"},
    "javascript": {"class_declaration"},
    "typescript": {"class_declaration", "interface_declaration"},
    "tsx":        {"class_declaration"},
}

_CALL_NODES = {
    "python":     {"call"},
    "java":       {"object_creation_expression"},
    "javascript": {"new_expression"},
    "typescript": {"new_expression"},
    "tsx":        {"new_expression"},
    "go":         {"composite_literal", "call_expression"},
}

# Languages with at least imports + (classes or calls) support.
_TS_EDGE_LANGS = set(_IMPORT_NODES) | set(_CLASS_NODES) | set(_CALL_NODES)

_IDENT_TYPES = {"identifier", "type_identifier", "dotted_name", "scoped_identifier",
                "scoped_type_identifier", "package_identifier", "field_identifier"}


def _default_parser(language: str):
    """Resolve a tree-sitter parser the same way index_worker does in production
    (tree_sitter_languages), with a core+language-pack fallback for dev boxes.
    Returns None if no grammar is available (→ caller falls back to regex)."""
    try:
        from tree_sitter_languages import get_parser  # production
        return get_parser(language)
    except Exception:
        pass
    try:  # modern dev stack
        import tree_sitter as _ts
        from tree_sitter_language_pack import get_language
        return _ts.Parser(get_language(language))
    except Exception:
        return None


def _iter(node):
    """DFS over all nodes."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _txt(node, sb: bytes) -> str:
    try:
        return sb[node.start_byte:node.end_byte].decode("utf-8", "replace")
    except Exception:
        return ""


def _last_seg(s: str) -> str:
    s = (s or "").strip().strip('"').strip("'")
    s = s.split(" as ")[0].split("<")[0].strip()
    for sep in (".", "::", "/"):
        if sep in s:
            s = s.split(sep)[-1]
    return s.strip()


def _field(node, name: str):
    try:
        return node.child_by_field_name(name)
    except Exception:
        return None


def _first_ident(node, sb: bytes) -> str:
    """Name of a definition node: prefer the 'name' field, else first identifier child."""
    nm = _field(node, "name")
    if nm is not None:
        return _txt(nm, sb).strip()
    for c in node.children:
        if c.type in ("identifier", "type_identifier"):
            return _txt(c, sb).strip()
    return ""


_IMPORT_LEAF_TYPES = {"dotted_name", "scoped_identifier", "scoped_type_identifier",
                      "interpreted_string_literal", "package_identifier"}


def _import_names(imp_node, sb: bytes) -> list[str]:
    """Collect imported simple names from an import node (AST — handles multi-line
    and aliased imports the regex path mangles). Dotted/scoped names are treated as
    leaves (last segment only) so intermediate path segments aren't captured."""
    names = []
    stack = list(imp_node.children)
    while stack:
        n = stack.pop()
        if n.type in _IMPORT_LEAF_TYPES:
            nm = _last_seg(_txt(n, sb))          # leaf: last segment, no descent
            if nm:
                names.append(nm)
        elif n.type in ("identifier", "type_identifier"):
            nm = _last_seg(_txt(n, sb))
            if nm:
                names.append(nm)
        else:
            stack.extend(n.children)
    return [n for n in names if n not in ("*", "_", "import", "from", "as")]


def _inherit_edges(class_node, language: str, sb: bytes) -> list[tuple]:
    """(rel_type, target) for a class node's super types."""
    out = []
    lang = language.lower()

    def _collect(container, rel):
        if container is None:
            return
        for d in _iter(container):
            if d.type in ("type_identifier", "identifier", "scoped_type_identifier"):
                t = _last_seg(_txt(d, sb))
                if t:
                    out.append((rel, t))

    if lang == "python":
        _collect(_field(class_node, "superclasses"), "extends")
    elif lang == "java":
        _collect(_field(class_node, "superclass"), "extends")
        _collect(_field(class_node, "interfaces"), "implements")
    elif lang in ("javascript", "typescript", "tsx"):
        # class_heritage holds `extends X` / `implements Y`
        for c in class_node.children:
            if c.type == "class_heritage":
                _collect(c, "extends")
    return out


def _callee_name(call_node, language: str, sb: bytes) -> str:
    lang = language.lower()
    if lang == "python":
        fn = _field(call_node, "function")
        if fn is None:
            return ""
        if fn.type == "identifier":
            return _txt(fn, sb).strip()
        if fn.type == "attribute":
            attr = _field(fn, "attribute")
            return _txt(attr, sb).strip() if attr is not None else ""
        return ""
    if lang == "java":  # object_creation_expression
        return _last_seg(_txt(_field(call_node, "type") or call_node, sb))
    if lang in ("javascript", "typescript", "tsx"):  # new_expression
        ctor = _field(call_node, "constructor")
        if ctor is not None:
            return _last_seg(_txt(ctor, sb))
        for c in call_node.children:
            if c.type in ("identifier", "member_expression"):
                return _last_seg(_txt(c, sb))
        return ""
    if lang == "go":
        target = _field(call_node, "type") or _field(call_node, "function")
        return _last_seg(_txt(target, sb)) if target is not None else ""
    return ""


def extract_file_edges_treesitter(source: str, file_path: str, language: str,
                                  parser=None) -> Optional[dict]:
    """Extract import / inherit / call edges from a file via AST.

    Returns None to signal "fall back to regex" (unsupported language, no grammar,
    parse failure). Otherwise returns:
      {"imports": [name,...],
       "classes": {class_name: {"inherits": [(rel_type, target),...], "calls": [name,...]}},
       "_source": "treesitter"}
    Call targets are filtered to capitalized identifiers (constructor/instantiation
    convention) to match the existing `calls` edge semantics.
    """
    lang = (language or "").lower()
    if lang not in _TS_EDGE_LANGS:
        return None
    p = parser or _default_parser(lang)
    if p is None:
        return None
    try:
        sb = source.encode("utf-8", "replace")
        tree = p.parse(sb)
        root = tree.root_node
    except Exception:
        return None

    try:
        # ── imports (file-wide) ──
        imports: list[str] = []
        seen_imp: set[str] = set()
        imp_types = _IMPORT_NODES.get(lang, set())
        for n in _iter(root):
            if n.type in imp_types:
                for nm in _import_names(n, sb):
                    if nm not in seen_imp:
                        seen_imp.add(nm)
                        imports.append(nm)

        # ── per-class inherit + call edges ──
        classes: dict[str, dict] = {}
        class_types = _CLASS_NODES.get(lang, set())
        call_types = _CALL_NODES.get(lang, set())
        for n in _iter(root):
            if n.type not in class_types:
                continue
            cname = _first_ident(n, sb)
            if not cname:
                continue
            inherits = _inherit_edges(n, lang, sb)
            calls: list[str] = []
            seen_calls: set[str] = set()
            for d in _iter(n):
                if d.type in call_types:
                    tgt = _callee_name(d, lang, sb)
                    if (tgt and len(tgt) >= 2 and tgt[0].isupper()
                            and tgt != cname and tgt not in seen_imp
                            and tgt not in seen_calls):
                        seen_calls.add(tgt)
                        calls.append(tgt)
            classes[cname] = {"inherits": inherits, "calls": calls}

        # ── file-wide data flow + control flow edges (P4) ──
        data_flow = extract_data_flow_edges(root, lang, sb)
        control_flow = extract_control_flow_edges(root, lang, sb)

        return {
            "imports": imports,
            "classes": classes,
            "data_flow": data_flow,
            "control_flow": control_flow,
            "_source": "treesitter",
        }
    except Exception:
        return None


def edges_to_relations(file_edges: dict, class_name: str) -> list[dict]:
    """Convert the structured extractor output into the `code_graph` relations JSONB
    list `[{type, target_name, target_file}]` for one class — same shape the regex
    path produces. File-wide imports + this class's inherit/call edges."""
    rels: list[dict] = []
    for nm in file_edges.get("imports", []):
        rels.append({"type": "imports", "target_name": nm, "target_file": ""})
    cls = (file_edges.get("classes") or {}).get(class_name) or {}
    for rel_type, target in cls.get("inherits", []):
        rels.append({"type": rel_type, "target_name": target, "target_file": ""})
    for nm in cls.get("calls", []):
        rels.append({"type": "calls", "target_name": nm, "target_file": ""})
    # P4: include data flow and control flow edges when present
    for nm in file_edges.get("data_flow", []):
        rels.append({"type": "data_flow", "target_name": nm, "target_file": ""})
    for nm in file_edges.get("control_flow", []):
        rels.append({"type": "control_flow", "target_name": nm, "target_file": ""})
    return rels


# ============================================================
# P4 — DATA FLOW & CONTROL FLOW EDGE EXTRACTION
# ============================================================
# These are additive — same JSONB shape {type, target_name, target_file}.
# Appended to the existing import/inherit/call edges in extract_file_edges_treesitter.
# Scope: top-level assignments (data flow) and branch/loop targets (control flow).
# Deliberately shallow — full SSA/DFG is compiler territory.

_DATA_FLOW_NODES: dict[str, set[str]] = {
    "python":     {"assignment", "augmented_assignment", "named_expression"},
    "java":       {"variable_declarator", "assignment_expression", "field_declaration"},
    "javascript": {"variable_declarator", "assignment_expression"},
    "typescript": {"variable_declarator", "assignment_expression"},
    "go":         {"short_var_decl", "var_spec", "assignment_statement"},
    "kotlin":     {"property_declaration", "variable_declaration"},
    "scala":      {"val_definition", "var_definition", "assignment"},
    "rust":       {"let_declaration", "assignment_expression"},
}

_CONTROL_FLOW_NODES: dict[str, set[str]] = {
    "python":     {"if_statement", "for_statement", "while_statement",
                   "try_statement", "with_statement", "match_statement"},
    "java":       {"if_statement", "for_statement", "while_statement",
                   "try_statement", "switch_expression"},
    "javascript": {"if_statement", "for_statement", "while_statement",
                   "try_statement", "switch_statement"},
    "typescript": {"if_statement", "for_statement", "while_statement",
                   "try_statement", "switch_statement"},
    "go":         {"if_statement", "for_statement", "select_statement",
                   "switch_statement", "defer_statement"},
    "kotlin":     {"if_expression", "for_statement", "while_statement",
                   "try_expression", "when_expression"},
    "scala":      {"if_expression", "for_expression", "while_expression",
                   "try_expression", "match_expression"},
    "rust":       {"if_expression", "for_expression", "while_expression",
                   "match_expression", "loop_expression"},
}


def extract_data_flow_edges(node, language: str, sb: bytes) -> list[str]:
    """
    Extract top-level assigned variable/field names from *node* subtree.
    Returns a list of identifier strings (target_name for data_flow edges).
    Shallow: only direct children of the node, not recursive.
    """
    lang = (language or "").lower()
    df_types = _DATA_FLOW_NODES.get(lang, set())
    if not df_types:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for child in _iter(node):
        if child.type in df_types:
            ident = _first_ident(child, sb)
            if ident and ident not in seen and len(ident) >= 2:
                seen.add(ident)
                names.append(ident)
    return names[:30]  # cap to avoid noise


def extract_control_flow_edges(node, language: str, sb: bytes) -> list[str]:
    """
    Extract control-flow branch targets (condition variable names) from *node* subtree.
    Returns a list of identifier strings (target_name for control_flow edges).
    """
    lang = (language or "").lower()
    cf_types = _CONTROL_FLOW_NODES.get(lang, set())
    if not cf_types:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for child in _iter(node):
        if child.type in cf_types:
            # Extract the first identifier in the condition (the variable being tested)
            ident = _first_ident(child, sb)
            if ident and ident not in seen and len(ident) >= 2:
                seen.add(ident)
                names.append(ident)
    return names[:20]  # cap to avoid noise
