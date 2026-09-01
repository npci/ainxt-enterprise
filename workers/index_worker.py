# SPDX-License-Identifier: Apache-2.0
# ============================================================
# INDEX WORKER — rq job that indexes a codebase into pgvector.
#
# Flow:
#   1. Distributed lock (Redis SET NX) — prevents duplicate runs
#   2. Status → "running" in repo_index_status
#   3. Collect files (local path or git clone)
#   4. AST-aware chunking via tree-sitter for ALL supported languages
#      (Java, Python, JS/TS, Go, Kotlin, Scala, C/C++, C#, Ruby,
#       Rust, PHP, Swift, Groovy, Bash, SQL stored procs).
#      Line-based fallback for config/data files (YAML, JSON, XML, MD).
#   5. Filter already-indexed chunks by content hash (incremental)
#   6. Call embed svc for embeddings in batches of 200
#   7. Bulk upsert to document_embeddings via batch INSERT
#   8. Status → "done" / "failed"
#   9. Release distributed lock
#
# Scales to 100+ parallel jobs because:
#   - Each repo has its own distributed lock (no cross-repo contention)
#   - Embed svc batches requests across all workers (1 Ollama call per 64 texts)
#   - pgvector writes use batch INSERT (500 rows/call) not individual INSERTs
#   - HNSW index is left live for incremental updates (small file diffs)
#     Full re-index: caller should set payload["drop_index"]=True
# ============================================================

import ast
import ctypes as _ctypes
import gc as _gc
import hashlib
import json
import os
import re as _re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator

import httpx
import itertools
from core.config import (
    EMBED_SVC_URL as _EMBED_SVC_URL,
    RDB_QUEUE, RDB_REGISTRY, RDB_EMBED,
)
from core.kv import get_kv

from core.logger import logger

# DB=5 — queue/locks, DB=3 — index status, DB=7 — enrichment cache.
# Backends selected per-DB via REDIS_CLIENT_CONFIG_DB{5,3,7}.
_R        = get_kv(RDB_QUEUE,    decode_responses=True)
_R_STATUS = get_kv(RDB_REGISTRY, decode_responses=True)
_R_ENRICH = get_kv(RDB_EMBED,    decode_responses=True)

# ── Embed svc URL pool (round-robin across instances) ──────────────────────
# Set EMBED_SVC_URLS=http://host1:8001,http://host2:8001 for multi-server setup.
# Falls back to EMBED_SVC_URL (single instance) when EMBED_SVC_URLS not set.
_raw_embed_urls   = os.getenv("EMBED_SVC_URLS", _EMBED_SVC_URL).split(",")
_EMBED_SVC_POOL   = [u.strip().rstrip("/") for u in _raw_embed_urls if u.strip()]
_embed_cycle      = itertools.cycle(range(len(_EMBED_SVC_POOL)))
_embed_cycle_lock = threading.Lock()

def _next_embed_url() -> str:
    with _embed_cycle_lock:
        return _EMBED_SVC_POOL[next(_embed_cycle)]

# ── tree-sitter universal parser ────────────────────────────────────────────
# tree-sitter-languages bundles pre-compiled grammars for 200+ languages.
# Falls back to line-based chunking if a language grammar is unavailable.
try:
    from tree_sitter_languages import get_parser as _ts_get_parser
    # Probe that the parser actually works — tree-sitter 0.20.x raises TypeError
    # when tree_sitter_languages internally calls Parser(language_obj) because the
    # old API only supports Parser() + parser.set_language(lang).
    _ts_get_parser("python")
    _TS_AVAILABLE = True
except ImportError:
    # tree-sitter-languages publishes nothing for Python 3.12+.  Fall back to
    # tree-sitter-language-pack, which graph_edges.py already uses, so a modern
    # interpreter keeps AST chunking instead of dropping to line-based.
    try:
        import tree_sitter as _TSCore
        from tree_sitter_language_pack import get_language as _tslp_get_language

        def _ts_get_parser(lang: str):   # noqa: F811  (intentional redefinition)
            return _TSCore.Parser(_tslp_get_language(lang))

        _ts_get_parser("python")
        _TS_AVAILABLE = True
        logger.info("index_worker: tree-sitter via tree-sitter-language-pack")
    except Exception as _ts_pack_err:
        _TS_AVAILABLE = False
        logger.warning(
            "index_worker: no tree-sitter grammar source available (%s) — "
            "line-based chunking only", _ts_pack_err,
        )
except Exception as _ts_probe_err:
    # tree-sitter version mismatch — redefine _ts_get_parser using the legacy API
    try:
        from tree_sitter_languages import get_language as _ts_get_language
        from tree_sitter import Parser as _TSParser
        def _ts_get_parser(lang: str):   # noqa: F811  (intentional redefinition)
            _p = _TSParser()
            _p.set_language(_ts_get_language(lang))
            return _p
        _TS_AVAILABLE = True
        logger.info("index_worker: tree-sitter using legacy Parser().set_language() API (0.20.x compat)")
    except Exception as _ts_fallback_err:
        _TS_AVAILABLE = False
        logger.warning(f"index_worker: tree-sitter unavailable ({_ts_fallback_err}) — line-based chunking only")

# Extension → tree-sitter language name
_LANG_MAP: dict[str, str] = {
    # JVM
    ".java":   "java",
    ".kt":     "kotlin",
    ".kts":    "kotlin",      # Kotlin Script (Gradle Kotlin DSL)
    ".scala":  "scala",
    ".groovy": "groovy",
    ".gradle": "groovy",      # Gradle build files (Groovy DSL)
    # Web / scripting
    ".py":     "python",
    ".js":     "javascript",
    ".ts":     "typescript",
    ".tsx":    "tsx",
    ".jsx":    "javascript",
    ".rb":     "ruby",
    ".php":    "php",
    # Systems
    ".go":     "go",
    ".rs":     "rust",
    ".c":      "c",
    ".h":      "c",
    ".cpp":    "cpp",
    ".cc":     "cpp",
    ".cxx":    "cpp",
    ".hpp":    "cpp",
    ".cs":     "c_sharp",
    ".swift":  "swift",
    # Shell / scripting
    ".sh":     "bash",
    ".bash":   "bash",
    # Data / config — tree-sitter available but no definition nodes (line chunking)
    ".sql":    "sql",
    ".yaml":   "yaml",
    ".yml":    "yaml",
    ".xml":    "xml",
    ".json":   "json",
    ".md":     "markdown",
}

# Node types that represent named, indexable code definitions per language.
# These are the boundaries at which we split chunks AND extract symbols.
_DEFINITION_NODES: dict[str, set[str]] = {
    "python":     {"function_definition", "async_function_def", "class_definition"},
    "java":       {"class_declaration", "interface_declaration", "enum_declaration",
                   "method_declaration", "constructor_declaration", "annotation_type_declaration"},
    "javascript": {"function_declaration", "generator_function_declaration", "class_declaration",
                   "method_definition", "arrow_function"},
    "typescript": {"function_declaration", "generator_function_declaration", "class_declaration",
                   "method_definition", "interface_declaration", "type_alias_declaration",
                   "enum_declaration", "abstract_class_declaration"},
    "tsx":        {"function_declaration", "class_declaration", "method_definition",
                   "interface_declaration", "type_alias_declaration"},
    "go":         {"function_declaration", "method_declaration", "type_declaration",
                   "type_spec"},
    "kotlin":     {"function_declaration", "class_declaration", "object_declaration",
                   "companion_object", "interface_declaration"},
    "scala":      {"function_definition", "class_definition", "object_definition",
                   "trait_definition", "val_definition", "var_definition"},
    "c":          {"function_definition", "struct_specifier", "union_specifier",
                   "enum_specifier", "declaration"},
    "cpp":        {"function_definition", "class_specifier", "struct_specifier",
                   "namespace_definition", "template_declaration"},
    "c_sharp":    {"class_declaration", "interface_declaration", "method_declaration",
                   "constructor_declaration", "struct_declaration", "enum_declaration"},
    "ruby":       {"method", "singleton_method", "class", "module", "do_block"},
    "rust":       {"function_item", "impl_item", "struct_item", "trait_item",
                   "enum_item", "mod_item", "type_item"},
    "php":        {"function_definition", "class_declaration", "interface_declaration",
                   "method_declaration"},
    "swift":      {"function_declaration", "class_declaration", "struct_declaration",
                   "protocol_declaration", "enum_declaration", "extension_declaration"},
    "bash":       {"function_definition"},
    "groovy":     {"class_declaration", "interface_declaration", "enum_declaration",
                   "method_declaration", "constructor_declaration"},
    "sql":        {"create_function_statement", "create_procedure_statement",
                   "create_trigger_statement", "create_view_statement"},
}

# Maps tree-sitter node type → our canonical symbol_type
_SYMBOL_TYPE_MAP: dict[str, str] = {
    "class_declaration": "class", "class_definition": "class", "class_specifier": "class",
    "class": "class", "abstract_class_declaration": "class",
    "interface_declaration": "interface", "interface_definition": "interface",
    "function_declaration": "function", "function_definition": "function",
    "async_function_def": "function", "generator_function_declaration": "function",
    "method_declaration": "method", "method_definition": "method",
    "constructor_declaration": "constructor",
    "enum_declaration": "enum", "enum_definition": "enum", "enum_item": "enum",
    "struct_specifier": "struct", "struct_declaration": "struct", "struct_item": "struct",
    "trait_definition": "trait", "trait_item": "trait",
    "type_declaration": "type", "type_alias_declaration": "type", "type_item": "type",
    "object_definition": "object", "object_declaration": "object",
    "annotation_type_declaration": "annotation",
    "function_item": "function",
    "impl_item": "impl",
    "mod_item": "module",
    "module": "module",
    "function": "function",
    "method": "method",
    "singleton_method": "method",
    # SQL stored-proc/function node types
    "create_function_statement": "function",
    "create_procedure_statement": "function",
    "create_trigger_statement": "trigger",
    "create_view_statement": "view",
}

# ── Code Knowledge Graph: node types worth creating graph nodes for ──────────
# Class / interface / module level only — methods are already in code_symbols.
# The resolver does file-path routing, so method granularity is unnecessary.
_GRAPH_NODE_TYPES = frozenset({
    "class", "interface", "enum", "struct", "trait",
    "object", "module", "type", "annotation",
})

# ── Import statement regex patterns per language ─────────────────────────────
# We extract the last segment (simple name) of each import so the graph can
# match node names without needing to resolve fully-qualified paths.
_IMPORT_RE: dict[str, list] = {
    "python": [
        _re.compile(r"^from\s+[\w.]+\s+import\s+([\w,\s*]+)", _re.MULTILINE),
        _re.compile(r"^import\s+([\w.]+(?:\s*,\s*[\w.]+)*)", _re.MULTILINE),
    ],
    "java": [
        _re.compile(r"^import\s+(?:static\s+)?(?:[\w.]+\.)(\w+)\s*;", _re.MULTILINE),
    ],
    "kotlin": [
        _re.compile(r"^import\s+(?:[\w.]+\.)(\w+)", _re.MULTILINE),
    ],
    "javascript": [
        _re.compile(r"import\s+(?:\{([^}]+)\}|(\w+))\s+from\s+['\"]", _re.MULTILINE),
        _re.compile(r"require\(['\"](?:.*/)?([\w_-]+)['\"]", _re.MULTILINE),
    ],
    "typescript": [
        _re.compile(r"import\s+(?:type\s+)?(?:\{([^}]+)\}|(\w+))\s+from\s+['\"]", _re.MULTILINE),
    ],
    "tsx": [
        _re.compile(r"import\s+(?:type\s+)?(?:\{([^}]+)\}|(\w+))\s+from\s+['\"]", _re.MULTILINE),
    ],
    "go": [
        _re.compile(r'"[\w./]*/(\w+)"', _re.MULTILINE),
    ],
    "scala": [
        _re.compile(r"^import\s+(?:[\w.]+\.)(\w+)", _re.MULTILINE),
    ],
    "c_sharp": [
        _re.compile(r"^using\s+(?:static\s+)?(?:[\w.]+\.)?(\w+)\s*;", _re.MULTILINE),
    ],
    "rust": [
        _re.compile(r"\buse\s+(?:[\w:]+::)?(\w+)(?:\s*;|\s*\{|\s+as\s)", _re.MULTILINE),
    ],
    "php": [
        _re.compile(r"\buse\s+(?:[\w\\]+\\)?(\w+)(?:\s+as\s+\w+)?\s*;", _re.MULTILINE),
    ],
    "swift": [
        _re.compile(r"^import\s+(\w+)", _re.MULTILINE),
    ],
    "ruby": [
        _re.compile(r"require[_relative]*\s+['\"](?:.*/)?([\w_-]+)['\"]", _re.MULTILINE),
    ],
    "cpp": [
        _re.compile(r"#include\s+[<\"](?:.*/)?([\w.]+)[>\"]"),
    ],
    "c": [
        _re.compile(r"#include\s+[<\"](?:.*/)?([\w.]+)[>\"]"),
    ],
    "groovy": [
        _re.compile(r"^import\s+(?:[\w.]+\.)(\w+)", _re.MULTILINE),
    ],
}

# ── Inheritance / supertype patterns per language ─────────────────────────────
# Applied to the class declaration header (first ~5 lines of each definition).
_EXTENDS_RE: dict[str, list] = {
    "python":     [(_re.compile(r"class\s+\w+\(([^)]+)\)"),                              "extends")],
    "java":       [(_re.compile(r"\bextends\s+([\w<>]+)"),                               "extends"),
                   (_re.compile(r"\bimplements\s+([\w<>,\s]+?)(?=\s*\{|\s+extends)"),    "implements")],
    "kotlin":     [(_re.compile(r":\s*([\w<>]+)(?:\([^)]*\))?"),                         "extends")],
    "typescript": [(_re.compile(r"\bextends\s+([\w<>,\s]+?)(?=\s*[\{,]|\s+implements)"), "extends"),
                   (_re.compile(r"\bimplements\s+([\w<>,\s]+?)(?=\s*\{)"),               "implements")],
    "javascript": [(_re.compile(r"\bextends\s+(\w+)"),                                   "extends")],
    "tsx":        [(_re.compile(r"\bextends\s+([\w<>,\s]+?)(?=\s*[\{,]|\s+implements)"), "extends")],
    "scala":      [(_re.compile(r"\bextends\s+([\w\[\],\s]+?)(?=\s+with|\s*\{|$)"),      "extends"),
                   (_re.compile(r"\bwith\s+([\w\[\],\s]+?)(?=\s+with|\s*\{|$)"),         "implements")],
    "c_sharp":    [(_re.compile(r":\s*([\w,\s]+?)(?=\s*\{|\s+where)"),                   "extends")],
    "rust":       [(_re.compile(r"\bfor\s+(\w+)"),                                       "implements")],
    "php":        [(_re.compile(r"\bextends\s+(\w+)"),                                   "extends"),
                   (_re.compile(r"\bimplements\s+([\w,\s]+?)(?=\s*\{)"),                 "implements")],
    "swift":      [(_re.compile(r":\s*([\w,\s]+?)(?=\s*\{)"),                            "extends")],
    "ruby":       [(_re.compile(r"<\s*(\w+)"),                                           "extends")],
    "cpp":        [(_re.compile(r":\s*(?:public|private|protected)\s+(\w+)"),             "extends")],
    "groovy":     [(_re.compile(r"\bextends\s+(\w+)"),                                   "extends"),
                   (_re.compile(r"\bimplements\s+([\w,\s]+?)(?=\s*\{)"),                 "implements")],
}

# ── Call / instantiation patterns per language ────────────────────────────────
# Captures "what classes does this class construct or call statically?"
# We use `new X()` (explicit allocation) and CamelCase-receiver static calls
# as the reliable signal.  Only CamelCase targets are kept to filter out
# primitive function calls (print(), len(), etc.).
_CALL_RE: dict[str, list] = {
    # Languages with explicit `new` keyword → unambiguous
    "java":       [_re.compile(r"\bnew\s+([A-Z]\w+)\s*[(<]")],
    "kotlin":     [_re.compile(r"\bnew\s+([A-Z]\w+)\s*[(<]"),
                   _re.compile(r"\b([A-Z]\w+)\s*\(")],   # Kotlin constructor: ClassName(...)
    "scala":      [_re.compile(r"\bnew\s+([A-Z]\w+)"),
                   _re.compile(r"\b([A-Z]\w+)\s*\(")],
    "c_sharp":    [_re.compile(r"\bnew\s+([A-Z]\w+)\s*[(<{]")],
    "cpp":        [_re.compile(r"\bnew\s+([A-Z]\w+)\s*[(<]")],
    "typescript": [_re.compile(r"\bnew\s+([A-Z]\w+)\s*[(<]")],
    "javascript": [_re.compile(r"\bnew\s+([A-Z]\w+)\s*[(<]")],
    "tsx":        [_re.compile(r"\bnew\s+([A-Z]\w+)\s*[(<]")],
    "php":        [_re.compile(r"\bnew\s+([A-Z]\w+)\s*[(<]")],
    "groovy":     [_re.compile(r"\bnew\s+([A-Z]\w+)\s*[(<]")],
    "swift":      [_re.compile(r"\bnew\s+([A-Z]\w+)\s*[(<]"),
                   _re.compile(r"\b([A-Z]\w+)\s*[(<{]")],
    # Python: CamelCase call = constructor (PEP 8 convention)
    "python":     [_re.compile(r"=\s*([A-Z]\w+)\s*\("),
                   _re.compile(r"\b([A-Z]\w+)\s*\(")],
    # Go: struct literal &StructName{} or StructName{} — capitalised = exported
    "go":         [_re.compile(r"&([A-Z]\w+)\s*\{"),
                   _re.compile(r"\b([A-Z]\w+)\s*\{")],
    # Rust: TypeName::new() and TypeName { } struct literals
    "rust":       [_re.compile(r"\b([A-Z]\w+)\s*::\s*\w+\s*[({]"),
                   _re.compile(r"\b([A-Z]\w+)\s*\{")],
    # Ruby: ClassName.new
    "ruby":       [_re.compile(r"\b([A-Z]\w+)\.new\b")],
}


def _extract_graph_nodes(source: str, file_path: str, language: str, symbols: list[dict]) -> list[dict]:
    """
    Build code_graph node records for class/interface/module-level symbols.
    Relations extracted per node:
      - imports     → from import statements at the top of the file (file-wide, shared)
      - extends     → from class declaration header (node-specific)
      - implements  → from class declaration header (node-specific)
      - calls       → from instantiation / static-call patterns within the class body
                      (e.g. `new RetryHandler()`, `PaymentService.process()`)

    Returns a list of dicts ready for _bulk_upsert_graph().

    WS-6: when SDLC_GRAPH_EDGE_MODE=treesitter, edges come from AST extraction
    (workers/graph_edges) with per-file / per-class fallback to the regex path
    below. Default `regex` ⇒ unchanged. Edge JSONB shape is identical either way.
    """
    # ── WS-6: optional tree-sitter (AST) edge extraction (flag-gated) ─────────
    _ts_edges = None
    _edge_src = "regex"
    try:
        from workers.graph_edges import graph_edge_mode as _gem, extract_file_edges_treesitter as _tsx
        if _gem() == "treesitter":
            try:
                _parser = _ts_get_parser(language) if _TS_AVAILABLE else None
            except Exception:
                _parser = None
            _ts_edges = _tsx(source, file_path, language, parser=_parser)
            if _ts_edges is not None:
                _edge_src = "treesitter"
            else:
                logger.debug(
                    f"index_worker: treesitter edges unavailable for {file_path} "
                    f"({language}) — regex fallback for this file"
                )
    except Exception as _ts_edge_err:
        logger.debug(f"index_worker: WS-6 edge extraction skipped: {_ts_edge_err}")
        _ts_edges = None

    # ── File-level: extract import relations once for the whole file ──────────
    import_rels: list[dict] = []
    seen_imports: set[str] = set()
    if _ts_edges is not None:
        for name in _ts_edges.get("imports", []):
            if name and name not in seen_imports:
                seen_imports.add(name)
                import_rels.append({"type": "imports", "target_name": name, "target_file": ""})
    else:
        for pattern in _IMPORT_RE.get(language, []):
            for m in pattern.finditer(source):
                # Some patterns have multiple groups (e.g. named imports {A, B} vs default D)
                raw_parts = [g for g in m.groups() if g] or [m.group(0)]
                for raw in raw_parts:
                    for part in raw.split(","):
                        # Strip aliases ("X as Y"), generics, whitespace
                        name = part.strip().split(" as ")[0].split("<")[0].strip()
                        name = name.split(".")[-1].strip()   # last segment only
                        if name and name not in ("*", "_", "") and name not in seen_imports:
                            seen_imports.add(name)
                            import_rels.append({"type": "imports", "target_name": name, "target_file": ""})

    # ── Symbol-level: build a node for each class/interface/module ────────────
    lines = source.splitlines()
    nodes: list[dict] = []
    class_symbols = [s for s in symbols if s.get("symbol_type") in _GRAPH_NODE_TYPES]

    for sym in class_symbols:
        name       = sym["symbol_name"]
        node_id    = f"{file_path}::{name}"
        line_start = max(0, (sym.get("line_start") or 1) - 1)   # 0-indexed
        line_end   = sym.get("line_end") or len(lines)

        inherit_rels: list[dict] = []
        call_rels: list[dict] = []
        _ts_cls = (_ts_edges.get("classes") if _ts_edges else None) or {}
        if _ts_edges is not None and name in _ts_cls:
            # ── WS-6 AST edges for this class ──
            for rel_type, target in _ts_cls[name].get("inherits", []):
                if target and target != name:
                    inherit_rels.append({"type": rel_type, "target_name": target, "target_file": ""})
            seen_calls: set[str] = set()
            for target in _ts_cls[name].get("calls", []):
                if (target and len(target) >= 2 and target != name
                        and target not in seen_calls and target not in seen_imports):
                    seen_calls.add(target)
                    call_rels.append({"type": "calls", "target_name": target, "target_file": ""})
        else:
            # ── regex fallback (default path; also per-class fallback when AST
            #    found no edges for this specific class) ──
            # Inheritance from the declaration header (up to 5 lines)
            decl_lines = lines[line_start:line_start + 5]
            decl_text  = "\n".join(decl_lines)
            for pattern, rel_type in _EXTENDS_RE.get(language, []):
                m = pattern.search(decl_text)
                if m:
                    for target in m.group(1).split(","):
                        target = target.strip().split("<")[0].strip()  # strip generics
                        if target and target != name:
                            inherit_rels.append({"type": rel_type, "target_name": target, "target_file": ""})

            # Call graph: scan class body for instantiations / static calls.
            # Restricted to the class body lines so we only attribute calls to the
            # class that makes them, not the whole file.
            body_src = "\n".join(lines[line_start:line_end])
            seen_calls = set()
            for pattern in _CALL_RE.get(language, []):
                for m in pattern.finditer(body_src):
                    target = m.group(1).strip()
                    # Skip: self-reference, built-ins, single-char, already recorded
                    if (target == name or len(target) < 2
                            or target in seen_calls
                            or target in seen_imports):   # imports already captured
                        continue
                    seen_calls.add(target)
                    call_rels.append({"type": "calls", "target_name": target, "target_file": ""})

        nodes.append({
            "node_id":   node_id,
            "node_type": sym.get("symbol_type", "class"),
            "name":      name,
            "file_path": file_path,
            "language":  language,
            "relations": import_rels + inherit_rels + call_rels,
            "metadata":  {"parent_name": sym.get("parent_name", "")},
        })

    # ── Module-level fallback: pure-script files (no classes) ────────────────
    # Still creates a node so the file is discoverable via import relations.
    if not nodes and import_rels:
        module_name = os.path.splitext(os.path.basename(file_path))[0]
        nodes.append({
            "node_id":   f"{file_path}::__module__",
            "node_type": "module",
            "name":      module_name,
            "file_path": file_path,
            "language":  language,
            "relations": import_rels,
            "metadata":  {},
        })

    # ── WS-6 telemetry: per-file edge counts by type + source (regex|treesitter) ──
    if _edge_src == "treesitter":
        try:
            _n_imp = sum(1 for n in nodes for r in n["relations"] if r["type"] == "imports")
            _n_inh = sum(1 for n in nodes for r in n["relations"]
                         if r["type"] in ("extends", "implements"))
            _n_call = sum(1 for n in nodes for r in n["relations"] if r["type"] == "calls")
            logger.info(
                f"index_worker: graph edges [{file_path}] source={_edge_src} "
                f"lang={language} imports={_n_imp} inherits={_n_inh} calls={_n_call}"
            )
        except Exception:
            pass

    return nodes


def _bulk_upsert_graph(repo_name: str, nodes: list[dict]) -> None:
    """
    Upsert code_graph nodes.  ON CONFLICT (repo, node_id) updates relations + timestamp
    so re-indexing a changed file always reflects the current graph state.
    Processed in batches of 500.
    """
    if not nodes:
        return
    from db.database import SessionLocal
    from sqlalchemy import text as _sql

    sql = _sql("""
        INSERT INTO code_graph
            (repo, node_id, node_type, name, file_path, language, relations, metadata)
        VALUES
            (:repo, :node_id, :node_type, :name, :file_path, :language,
             CAST(:relations AS jsonb), CAST(:metadata AS jsonb))
        ON CONFLICT (repo, node_id) DO UPDATE SET
            relations  = EXCLUDED.relations,
            updated_at = NOW()
    """)

    rows = [
        {
            "repo":      repo_name,
            "node_id":   n["node_id"],
            "node_type": n["node_type"],
            "name":      n["name"],
            "file_path": n["file_path"],
            "language":  n["language"],
            "relations": json.dumps(n.get("relations", [])),
            "metadata":  json.dumps(n.get("metadata", {})),
        }
        for n in nodes
    ]
    logger.info(f"index_worker: graph count - {len(rows)}")
    try:
        db = SessionLocal()
        try:
            for i in range(0, len(rows), 500):
                db.execute(sql, rows[i:i + 500])
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"index_worker: graph upsert failed: {e}")

def _mirror_code_nodes_to_kg(repo_name: str, nodes: list[dict],
                             product_id: str = "", department: str = "") -> None:
    """
    Mirror code_graph nodes (just upserted by _bulk_upsert_graph) into the unified
    knowledge_graph_nodes / knowledge_graph_edges tables, attaching RBAC scoping.

    graph_id = "repo:<name>".  Edges are derived from each node's `relations`
    (imports/extends/implements/calls).  dst_node_id follows the "{file}::{name}"
    convention when the target file is known, else the bare target name (resolved
    by name during multi-hop traversal).  Best-effort + non-fatal — must never
    block the indexing run.  RBAC defaults to internal/PUBLIC/band-0 (overridable
    later by a product policy lookup).
    """
    if not nodes:
        return
    from db.database import SessionLocal
    from sqlalchemy import text as _sql

    graph_id = f"repo:{repo_name}"
    _pid = product_id or None          # UUID column — '' would fail the cast
    _dept = department or None

    # Resolve bare target names → the real "{file}::{name}" node_id so edges
    # actually connect to their target node during multi-hop traversal (the CTE
    # joins dst_node_id ↔ node_id exactly; a bare name like "ModelRouter" would
    # otherwise dangle and break call/import traversal).
    name_to_id: dict[str, str] = {}
    for n in nodes:
        name_to_id.setdefault(n["name"], n["node_id"])

    node_rows: list[dict] = []
    edge_rows: list[dict] = []
    for n in nodes:
        nid = n["node_id"]
        node_rows.append({
            "graph_id":   graph_id,
            "node_id":    nid,
            "node_type":  n["node_type"],
            "name":       n["name"],
            "source_ref": n.get("file_path"),
            "language":   n.get("language"),
            "product_id": _pid,
            "department": _dept,
        })
        for rel in (n.get("relations") or []):
            tgt_name = rel.get("target_name")
            if not tgt_name:
                continue
            tgt_file = rel.get("target_file")
            if tgt_file:
                dst = f"{tgt_file}::{tgt_name}"
            else:
                # Prefer a same-graph node with that name; else keep the bare
                # name as a leaf reference (external lib, undiscovered file).
                dst = name_to_id.get(tgt_name, tgt_name)
            edge_rows.append({
                "graph_id": graph_id,
                "src":      nid,
                "dst":      dst,
                "etype":    rel.get("type", "related_to"),
            })

    node_sql = _sql("""
                    INSERT INTO knowledge_graph_nodes
                    (graph_id, node_id, node_type, name, source_type, source_ref,
                     language, product_id, department)
                    VALUES
                        (:graph_id, :node_id, :node_type, :name, 'code', :source_ref,
                         :language, CAST(:product_id AS uuid), :department)
                        ON CONFLICT (graph_id, node_id) DO UPDATE SET
                        node_type  = EXCLUDED.node_type,
                                                               name       = EXCLUDED.name,
                                                               source_ref = EXCLUDED.source_ref,
                                                               language   = EXCLUDED.language,
                                                               product_id = EXCLUDED.product_id,
                                                               department = EXCLUDED.department,
                                                               updated_at = NOW()
                    """)
    edge_sql = _sql("""
                    INSERT INTO knowledge_graph_edges
                        (graph_id, src_node_id, dst_node_id, edge_type)
                    VALUES (:graph_id, :src, :dst, :etype)
                        ON CONFLICT (graph_id, src_node_id, dst_node_id, edge_type) DO NOTHING
                    """)
    try:
        db = SessionLocal()
        try:
            for i in range(0, len(node_rows), 500):
                db.execute(node_sql, node_rows[i:i + 500])
            for i in range(0, len(edge_rows), 500):
                db.execute(edge_sql, edge_rows[i:i + 500])
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"index_worker: KG mirror failed: {e}")

_ENRICH_MODEL = os.getenv("ENRICH_MODEL", "")   # set via ENRICH_MODEL in .env — no code default
# ── Constants ──────────────────────────────────────────────────
ENRICH_MODEL       = _ENRICH_MODEL
LOCK_TTL           = 43200        # 12 hours — 100k+ vector repos run 10+ hours
CHUNK_SIZE         = 512          # tokens (approximated as chars / 4)
CHUNK_OVERLAP      = 64
BATCH_EMBED        = 64           # match embed svc batch accumulator (64 texts / 50ms)
BATCH_INSERT       = 500          # rows per pgvector INSERT
EMBED_CONCURRENCY  = 4            # 8 concurrent embed threads (64 texts × 8 = 512 in-flight); more threads
                                  # keep the embed svc queue full with 2000-file waves
ENRICH_CONCURRENCY = 8            # 16 → 8: each thread holds 1500-char code + LLM response in RAM;
                                  # Ollama serialises internally so >8 threads don't improve throughput
ENRICH_MIN_CHARS   = 4            # skip enrichment for short boilerplate chunks
ENRICH_CONTENT_MAX = 1000         # max chars stored per (enriched) chunk — was 4000
ENRICH_CACHE_TTL   = 7 * 86400    # 7 days — descriptions valid across re-indexes
ENRICH_CACHE_PFX   = "enrich_desc:"  # key prefix in Redis db=7 (shared with embed SHA256 cache, no collision)
_EMBED_SPLIT_CHARS = 2000         # split chunks longer than this before embedding/insert
FILE_WAVE          = 2000         # files processed per wave — increased from 500; with 1 TB RAM
                                  # larger waves reduce DB round-trips (dedup IN, symbol upsert,
                                  # progress update) by 4× while peak RAM per wave stays under 1 GB
MAX_FILE_BYTES     = 512 * 1024   # skip files > 512 KB

# Extensions eligible for LLM-based NL enrichment
_ENRICH_CODE_EXTS = {
    ".py", ".java", ".js", ".ts", ".tsx", ".jsx",
    ".go", ".rs", ".cpp", ".cc", ".cxx", ".c", ".cs",
    ".kt", ".kts", ".scala", ".rb", ".php", ".swift",
    ".sh", ".bash", ".sql",
}

_ENRICH_PROMPT = """You are a code documentation assistant. Read the following code chunk and write a concise 1-3 sentence natural language description of what it does, its purpose, and any key concepts it implements. Be specific and technical. Output ONLY the description, no preamble.

```
{code}
```"""

SUPPORTED_EXT = {
    # JVM
    ".java", ".kt", ".kts", ".scala", ".groovy", ".gradle",
    # Web / scripting
    ".py", ".js", ".ts", ".tsx", ".jsx", ".rb", ".php",
    # Systems
    ".go", ".rs", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".cs", ".swift",
    # Shell
    ".sh", ".bash",
    # Data / config / docs
    ".sql", ".yaml", ".yml", ".json", ".xml", ".md", ".txt",
    # Misc
    ".dockerfile", ".toml", ".properties",
}


# ── Enrich embedding ──────────────────────────────────────────

def _enrich_chunk(chunk: dict) -> dict:
    """
    Prepend an LLM-generated NL description to a code chunk before embedding.
    This makes semantic search richer — queries like "how is retry handled?"
    match chunks whose code implements retry but never mentions the word.

    Returns the (possibly mutated) chunk. Never raises — falls back to original
    content if the LLM call fails.
    """
    content = chunk.get("content", "")
    if len(content.strip()) < ENRICH_MIN_CHARS:
        return chunk  # too short to be worth enriching

    ext = ("." + chunk.get("file_path", "").rsplit(".", 1)[-1].lower()
           if "." in chunk.get("file_path", "") else "")
    if ext not in _ENRICH_CODE_EXTS:
        return chunk

    try:
        from models.model_router import model_router as _mr
        code = content
        # Cache/content-dedup key, not a security control — sha256 avoids the
        # static-analysis false positive that MD5 triggers here.
        _ck  = ENRICH_CACHE_PFX + hashlib.sha256(code.encode()).digest().hex()

        # ── Cache hit: skip LLM call entirely ────────────────────
        try:
            cached_desc = _R_ENRICH.get(_ck)
        except Exception:
            cached_desc = None

        if cached_desc:
            enriched = f"{cached_desc}\n\n{content}"
            chunk = dict(chunk)
            chunk["content"]      = enriched[:ENRICH_CONTENT_MAX]
            chunk["content_hash"] = hashlib.sha256(enriched.encode()).digest().hex()
            return chunk

        # ── Cache miss: call LLM ──────────────────────────────────
        prompt      = _ENRICH_PROMPT.format(code=code)
        description = _mr.generate(prompt, model_hint=ENRICH_MODEL).strip()
        if description and len(description) > 10:
            enriched = f"{description}\n\n{content}"
            chunk = dict(chunk)
            chunk["content"]      = enriched[:ENRICH_CONTENT_MAX]
            chunk["content_hash"] = hashlib.sha256(enriched.encode()).digest().hex()
            try:
                _R_ENRICH.setex(_ck, ENRICH_CACHE_TTL, description)
            except Exception:
                pass  # cache write failure is non-fatal
    except Exception as _e:
        logger.debug(f"index_worker: enrich failed for {chunk.get('file_path')}: {_e}")

    return chunk



def _enrich_chunks(chunks: list[dict]) -> list[dict]:
    """
    Enrich all eligible chunks — one LLM call per chunk via _enrich_chunk().
    ENRICH_CONCURRENCY threads run in parallel so Ollama's queue stays full
    while each individual call waits for a response.

    Redis cache (db=7, key enrich_desc:{MD5}) means re-indexing the same repo
    skips all LLM calls — each _enrich_chunk() returns instantly on cache hit.
    """
    if not chunks:
        return chunks

    t0 = time.perf_counter()

    eligible = [
        i for i, c in enumerate(chunks)
        if len(c.get("content", "").strip()) >= ENRICH_MIN_CHARS
        and ("." + c.get("file_path", "").rsplit(".", 1)[-1].lower()
             if "." in c.get("file_path", "") else "") in _ENRICH_CODE_EXTS
    ]

    logger.info(f"index_worker: enrichment — {len(eligible)}/{len(chunks)} chunks eligible (code exts, ≥{ENRICH_MIN_CHARS} chars)")
    if not eligible:
        return chunks

    result = list(chunks)

    def _enrich_one(idx: int) -> tuple[int, dict]:
        original = result[idx]
        enriched = _enrich_chunk(original)
        return idx, enriched

    logger.info(f"index_worker: enrichment — {len(eligible)} chunks, concurrency={ENRICH_CONCURRENCY}")
    total_enriched = 0
    with ThreadPoolExecutor(max_workers=ENRICH_CONCURRENCY) as pool:
        futures = {pool.submit(_enrich_one, idx): idx for idx in eligible}
        for fut in futures:
            try:
                idx, enriched = fut.result(timeout=30)
                result[idx] = enriched
                if enriched["content"] != chunks[idx]["content"]:
                    total_enriched += 1
            except TimeoutError:
                fp = chunks[futures[fut]].get("file_path", "?")
                logger.warning(f"index_worker: enrich timeout for {fp} — using original")
            except Exception as _e:
                logger.warning(f"index_worker: enrich error: {_e}")

    elapsed = time.perf_counter() - t0
    logger.info(
        f"index_worker: enriched {total_enriched}/{len(chunks)} chunks with NL descriptions "
        f"in {elapsed:.2f}s"
    )
    return result


# ── Lock renewal ─────────────────────────────────────────────

def _start_lock_renewal(lock_key: str) -> threading.Event:
    """
    Starts a daemon thread that renews the Redis distributed lock every 30 min.
    Returns a stop Event — caller must set it when done.

    Without this, a 10+ hour job loses its lock after LOCK_TTL seconds and a
    second job for the same repo can start, causing double-indexing.
    """
    stop = threading.Event()

    def _renew():
        while not stop.wait(1800):  # renew every 30 min
            try:
                _R.expire(lock_key, LOCK_TTL)
            except Exception:
                pass

    t = threading.Thread(target=_renew, daemon=True, name=f"lock-renewal:{lock_key}")
    t.start()
    return stop


# ── Entry point ───────────────────────────────────────────────

def index_repo_job(payload: dict) -> dict:
    """
    RQ job: index a codebase into pgvector.

    payload keys:
      repo_name    str   — unique repo identifier (used as repo_filter)
      repo_path    str   — absolute local path OR GitLab HTTPS URL
      branch       str   — git branch to clone/checkout (empty = remote default)
      triggered_by str   — user who triggered the index
      drop_index   bool  — if True, delete existing chunks before indexing (full re-index)
      file_filter  list  — optional list of specific file paths to re-index (incremental)
      product_id   str   — product this repo belongs to (for vector metadata scoping)
      department   str   — department derived from product mapping (for vector metadata scoping)
    """
    repo_name    = payload["repo_name"]
    repo_path    = payload["repo_path"]
    branch       = payload.get("branch", "")
    triggered_by = payload.get("triggered_by", "system")
    drop_index   = payload.get("drop_index", False)
    file_filter  = payload.get("file_filter")   # list[str] or None
    product_id   = payload.get("product_id", "")
    department   = payload.get("department", "")

    # Restore trace context from payload (consistent logs across gateway → worker)
    from core.logger import set_request_id, set_chat_context, set_span_id
    set_request_id(payload.get("request_id", repo_name))
    set_chat_context(triggered_by, "index")
    set_span_id("index_worker")

    lock_key = f"index:lock:{repo_name}"

    # Capture git remote URL so /ide/repo/resolve can map workspace → repo_filter.
    # NEVER persist embedded credentials: repo_path arrives with the indexer's PAT
    # baked in (routers/index_router.inject_gitlab_token) so THIS clone can auth, but
    # git_url is a per-repo shared value read back by every user's SDLC clone. Strip
    # the token so each run re-injects its own (build_run_clone_url).
    _git_url: str = None
    if repo_path and (repo_path.startswith("https://") or repo_path.startswith("git@")):
        from core.platform_credentials import strip_gitlab_token as _strip_tok
        _git_url = _strip_tok(repo_path.split("?")[0].rstrip("/"))  # drop creds + query/slash

    # ── Distributed lock ────────────────────────────────────────
    # Store metadata in the lock value so enqueue_index_job (and humans reading Redis)
    # can see which request/user currently holds the lock.
    import json as _json
    lock_meta = _json.dumps({
        "request_id":   payload.get("request_id", ""),
        "triggered_by": triggered_by,
        "started_at":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "branch":       branch,
    })
    acquired = _R.set(lock_key, lock_meta, nx=True, ex=LOCK_TTL)
    if not acquired:
        try:
            held_by = _json.loads(_R.get(lock_key) or "{}")
        except Exception:
            held_by = {}
        logger.warning(
            f"index_worker: lock held for '{repo_name}' — skipping duplicate job. "
            f"Held by request_id={held_by.get('request_id', '?')} "
            f"triggered_by={held_by.get('triggered_by', '?')} "
            f"started_at={held_by.get('started_at', '?')}"
        )
        return {"status": "skipped", "reason": "already_indexing"}

    # Renew lock every 30 min — prevents expiry during 10+ hour indexing runs
    _lock_renewal_stop = _start_lock_renewal(lock_key)

    _update_status(repo_name, "running", triggered_by=triggered_by, git_url=_git_url, branch=branch or None)

    try:
        result = _do_index(repo_name, repo_path, drop_index, file_filter, product_id, department, branch)

        # ── Build sandbox image for SDLC execution ────────────────
        # Extract structured build metadata from the freshly indexed chunks.
        # Runs AFTER indexing so document_embeddings is fully populated.
        # Never blocks indexing — failures are logged and ignored.
        _local_path = result.get("local_path")
        try:
            from core.build_metadata_extractor import BuildMetadataExtractor
            _meta = BuildMetadataExtractor().extract_and_store(
                repo_slug=repo_name,
                local_path=_local_path,   # workspace fallback if not yet searchable
            )
            if _meta:
                logger.info(
                    f"index_worker: build metadata extracted for {repo_name} — "
                    f"tool={_meta.get('build_tool')} lang={_meta.get('language')} "
                    f"ver={_meta.get('language_version')} from={_meta.get('extracted_from')}"
                )
            else:
                logger.info(
                    f"index_worker: no build metadata detected for {repo_name} "
                    f"(UNKNOWN_BUILD_PATTERN — .sdlc.yml MR will be queued on first SDLC run)"
                )
        except Exception as _meta_err:
            logger.warning(f"index_worker: build metadata extraction failed for {repo_name}: {_meta_err}")

        _update_status(repo_name, "done",
                       total_chunks=result["total"],
                       indexed_chunks=result["indexed"],
                       branch=branch or None)
        _update_index_request(payload.get("request_id"), "done")

        # Sync status to Redis so index_router.py reads "ready" immediately
        try:
            _R_STATUS.set(f"index:repo:{repo_name}:status", "ready")
            _R_STATUS.set(f"index:repo:{repo_name}:indexed_at", str(time.time()))
        except Exception as _re:
            logger.warning(f"index_worker: redis status sync failed: {_re}")

        # Invalidate 24h retrieval cache for this repo so queries immediately
        # see the freshly indexed chunks instead of stale pre-reindex results.
        try:
            from core.config import RDB_CACHE
            from core.kv import get_kv as _get_kv
            _rc = _get_kv(RDB_CACHE, decode_responses=True)
            _prefix = "hybrid_retrieval:v2:"
            keys = _rc.keys(f"{_prefix}*")
            if keys:
                _rc.delete(*keys)
                logger.info(
                    f"index_worker: invalidated {len(keys)} retrieval cache entries after re-index of {repo_name}"
                )
        except Exception as _ce:
            logger.warning(f"index_worker: retrieval cache invalidation failed: {_ce}")

        # Kafka telemetry (fire-and-forget)
        try:
            from core.kafka_producer import produce, TOPIC_EMBEDDINGS, TOPIC_METRICS
            produce(TOPIC_EMBEDDINGS, {
                "repo":          repo_name,
                "total_chunks":  result["total"],
                "new_chunks":    result["indexed"],
                "triggered_by":  triggered_by,
            }, key=repo_name)
        except Exception:
            pass

        logger.info(f"index_worker: {repo_name} done — {result['indexed']} chunks indexed")
        return result

    except Exception as exc:
        logger.error(f"index_worker: {repo_name} failed — {exc}")
        _update_status(repo_name, "failed", error_msg=str(exc)[:1000])
        _update_index_request(payload.get("request_id"), "failed", error_msg=str(exc)[:1000])
        # Sync failure status to Redis so the status endpoint reflects reality.
        # Mirrors the success path (which writes "ready") — wrapped in try/except
        # so a Redis outage never masks the original exception.
        try:
            _R_STATUS.set(f"index:repo:{repo_name}:status", "failed")
            _R_STATUS.set(f"index:repo:{repo_name}:error", str(exc)[:500])
        except Exception as _re:
            logger.warning(f"index_worker: redis failure status sync failed: {_re}")
        raise

    finally:
        _lock_renewal_stop.set()
        _R.delete(lock_key)


# ── Core indexing ─────────────────────────────────────────────

def _do_index(
    repo_name:   str,
    repo_path:   str,
    drop_index:  bool,
    file_filter: list | None,
    product_id:  str = "",
    department:  str = "",
    branch:      str = "",
) -> dict:
    # ── Resolve local path (clone if HTTPS URL) ────────────────
    local_path = _resolve_path(repo_name, repo_path, branch)

    # ── Collect files ──────────────────────────────────────────
    if file_filter:
        files = [Path(local_path) / f for f in file_filter if (Path(local_path) / f).exists()]
    else:
        files = list(_collect_files(local_path))

    logger.info(f"index_worker: {repo_name} — {len(files)} files to process")

    if not files:
        return {"status": "done", "total": 0, "indexed": 0, "local_path": str(local_path)}

    total_waves = (len(files) + FILE_WAVE - 1) // FILE_WAVE

    # ── Wave checkpoint — resume from last completed wave if killed mid-run ──
    # Key: index:checkpoint:{repo_name}, TTL 48h.
    # Value: {"drop_done": true, "wave": N}  where N = number of waves completed.
    # On restart: if drop_done is true the chunk delete was already done; skip it
    # and resume from wave N.  Incremental dedup (_filter_new_chunks) handles any
    # partially-indexed wave automatically.
    _ckpt_key        = f"index:checkpoint:{repo_name}"
    _ckpt_start_wave = 0   # 0 = no checkpoint, start from the beginning

    if drop_index:
        existing_ckpt = None
        try:
            existing_ckpt = _R.get(_ckpt_key)
        except Exception:
            pass
        if existing_ckpt:
            try:
                _ckpt_data = json.loads(existing_ckpt)
                if _ckpt_data.get("drop_done"):
                    _ckpt_start_wave = int(_ckpt_data.get("wave", 0))
                    drop_index = False   # skip delete — already done in the previous run
                    logger.info(
                        f"index_worker: {repo_name} — checkpoint found, resuming from "
                        f"wave {_ckpt_start_wave + 1}/{total_waves} (drop already done)"
                    )
            except Exception as _ce:
                logger.warning(f"index_worker: {repo_name} — checkpoint parse failed ({_ce}), starting fresh")

    # ── Drop existing chunks if full re-index ─────────────────
    if drop_index:
        _delete_repo_chunks(repo_name)
        logger.info(f"index_worker: dropped existing chunks for {repo_name}")
        try:
            _R.setex(_ckpt_key, 172800, json.dumps({"drop_done": True, "wave": 0, "total_waves": total_waves}))
        except Exception:
            pass

    total_new = 0
    indexed   = 0

    def _process_batch(batch):
        # Expand chunks whose sanitized content exceeds _EMBED_SPLIT_CHARS into
        # sub-chunks.  Each sub-chunk gets its own UUID, content_hash, and a
        # derived chunk_index (base * 1000 + sub_index) to satisfy the unique
        # constraint on (repo, file_path, chunk_index).
        expanded: list[dict] = []
        for c in batch:
            text = _sanitize_for_embed(c["content"], c.get("file_path", ""))
            if len(text) <= _EMBED_SPLIT_CHARS:
                sc = dict(c)
                sc["content"] = text
                expanded.append(sc)
            else:
                base_idx = c.get("chunk_index", 0)
                for si, off in enumerate(range(0, len(text), _EMBED_SPLIT_CHARS)):
                    sc = dict(c)
                    sc["id"]           = str(uuid.uuid4())
                    sc["content"]      = text[off:off + _EMBED_SPLIT_CHARS]
                    sc["content_hash"] = hashlib.sha256(sc["content"].encode()).digest().hex()
                    sc["chunk_index"]  = base_idx * 1000 + si
                    expanded.append(sc)

        texts = [c["content"] for c in expanded]
        fps   = [c.get("file_path", "?") for c in expanded]
        lens  = [len(t) for t in texts]
        if len(expanded) != len(batch):
            logger.info(
                f"index_worker [embed→]: split {len(batch)} chunks → {len(expanded)} sub-chunks "
                f"(split_threshold={_EMBED_SPLIT_CHARS})"
            )
        logger.debug(
            f"index_worker [embed→]: batch={len(expanded)} "
            f"chars min={min(lens)} max={max(lens)} avg={sum(lens)//len(lens)} "
            f"files={fps[:3]}{'...' if len(fps) > 3 else ''}"
        )
        try:
            embeddings = _call_embed_svc(texts)
        except Exception as exc:
            logger.error(
                f"index_worker [embed✗]: batch FAILED — {type(exc).__name__}: {exc} | "
                f"files={fps} | chars={lens}"
            )
            raise
        if not embeddings or len(embeddings) != len(expanded):
            logger.error(
                f"index_worker [embed✗]: wrong embedding count {len(embeddings)} "
                f"for batch {len(expanded)} — skipping | files={fps}"
            )
            return 0
        logger.debug(f"index_worker [embed✓]: batch={len(expanded)} embedded OK")
        _bulk_upsert(repo_name, expanded, embeddings, product_id=product_id, department=department, branch=branch)
        return len(expanded)

    # ── Wave-based processing — bounds peak RAM to FILE_WAVE × avg_chunk_size ──
    # For 100k+ vector repos this prevents holding all chunks in memory at once.
    # Graph extraction is merged into each wave (no separate second full-file scan).
    _total_graph_nodes = 0
    for wave_num, wave_start in enumerate(range(0, len(files), FILE_WAVE), 1):

        # ── Checkpoint skip — fast-forward past already-completed waves ──────
        if wave_num - 1 < _ckpt_start_wave:
            logger.info(
                f"index_worker: {repo_name} wave {wave_num}/{total_waves} — "
                f"skipped (checkpoint resume)"
            )
            continue

        wave_t0 = time.perf_counter()
        wave_files   = files[wave_start:wave_start + FILE_WAVE]
        wave_chunks: list[dict] = []
        wave_symbols: list[dict] = []

        chunk_t0 = time.perf_counter()
        for fpath in wave_files:
            try:
                fc, fs = _chunk_file(fpath, local_path)
                wave_chunks.extend(fc)
                wave_symbols.extend(fs)
            except Exception as e:
                logger.debug(f"index_worker: skip {fpath}: {e}")
        logger.info(
            f"index_worker: {repo_name} wave {wave_num}/{total_waves} — "
            f"chunking {len(wave_files)} files took {time.perf_counter() - chunk_t0:.2f}s "
            f"({len(wave_chunks)} chunks, {len(wave_symbols)} symbols)"
        )

        # Dedup incremental
        if not drop_index and wave_chunks:
            dedup_t0 = time.perf_counter()
            wave_chunks = _filter_new_chunks(repo_name, wave_chunks)
            logger.info(
                f"index_worker: {repo_name} wave {wave_num}/{total_waves} — "
                f"dedup took {time.perf_counter() - dedup_t0:.2f}s "
                f"({len(wave_chunks)} new chunks)"
            )

        wave_new = len(wave_chunks)
        total_new += wave_new
        logger.info(
            f"index_worker: {repo_name} wave {wave_num}/{total_waves} — "
            f"{len(wave_files)} files, {wave_new} new chunks"
        )

        # Enrich + embed only if there are new chunks in this wave
        if wave_chunks:
            wave_chunks = _enrich_chunks(wave_chunks)
            embed_t0 = time.perf_counter()
            batches = [wave_chunks[i:i + BATCH_EMBED]
                       for i in range(0, len(wave_chunks), BATCH_EMBED)]
            with ThreadPoolExecutor(max_workers=EMBED_CONCURRENCY) as pool:
                for count in pool.map(_process_batch, batches):
                    indexed += count
            logger.info(
                f"index_worker: {repo_name} wave {wave_num}/{total_waves} — "
                f"embed+upsert took {time.perf_counter() - embed_t0:.2f}s "
                f"({len(wave_chunks)} chunks, {len(batches)} batches)"
            )
            _update_progress(repo_name, total_new, indexed)

        # Upsert symbols per wave so they're never held in full-repo RAM
        if wave_symbols:
            sym_t0 = time.perf_counter()
            for s in wave_symbols:
                s["repo"] = repo_name
            _bulk_upsert_symbols(repo_name, wave_symbols)
            logger.info(
                f"index_worker: {repo_name} wave {wave_num}/{total_waves} — "
                f"symbol upsert took {time.perf_counter() - sym_t0:.2f}s "
                f"({len(wave_symbols)} symbols)"
            )

        # ── Knowledge graph — merged into wave (eliminates separate second scan) ──
        # Failures must never block indexing; graph is best-effort.
        if _TS_AVAILABLE:
            graph_t0 = time.perf_counter()
            wave_graph_nodes: list[dict] = []
            _g_wave_count = 0
            for fpath in wave_files:
                try:
                    ext  = fpath.suffix.lower()
                    lang = _LANG_MAP.get(ext)
                    if not lang or lang not in _DEFINITION_NODES:
                        continue
                    rel_path = str(fpath.relative_to(local_path))
                    text     = fpath.read_text(encoding="utf-8", errors="replace")
                    _, file_symbols = _ts_chunk(text, rel_path, lang)
                    nodes = _extract_graph_nodes(text, rel_path, lang, file_symbols)
                    for n in nodes:
                        n["repo"] = repo_name
                    wave_graph_nodes.extend(nodes)
                    _g_wave_count += 1
                    if len(wave_graph_nodes) >= 2000:
                        _bulk_upsert_graph(repo_name, wave_graph_nodes)
                        wave_graph_nodes = []
                except Exception as _ge:
                    logger.warning(f"index_worker: graph extract skip {fpath}: {_ge}")
            if wave_graph_nodes:
                _bulk_upsert_graph(repo_name, wave_graph_nodes)
            _total_graph_nodes += _g_wave_count
            del wave_graph_nodes
            logger.info(
                f"index_worker: {repo_name} wave {wave_num}/{total_waves} — "
                f"graph extraction took {time.perf_counter() - graph_t0:.2f}s "
                f"({_g_wave_count} files processed)"
            )

        del wave_chunks, wave_symbols   # explicit release

        # ── Return freed memory to OS — prevents Python arena fragmentation ──
        # gc.collect() finalises reference cycles; malloc_trim(0) releases glibc
        # arenas back to the OS so RSS doesn't grow monotonically across waves.
        _gc.collect()
        try:
            _ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass

        # ── Persist wave checkpoint so a killed job can resume ────────────────
        try:
            _R.setex(_ckpt_key, 172800, json.dumps({
                "drop_done":   True,
                "wave":        wave_num,   # N waves completed
                "total_waves": total_waves,
            }))
        except Exception:
            pass

        logger.info(
            f"index_worker: {repo_name} wave {wave_num}/{total_waves} — "
            f"total wave time {time.perf_counter() - wave_t0:.2f}s"
        )

    logger.info(
        f"index_worker: {repo_name} — all waves done: {indexed}/{total_new} vectors indexed, "
        f"{_total_graph_nodes} files with graph nodes"
    )

    # ── Clear checkpoint — job completed successfully ─────────────────────────
    try:
        _R.delete(_ckpt_key)
    except Exception:
        pass

    # ── Phase 7: derive and cache codebase conventions from graph ─────────────
    # Runs once per successful index run. Falls back silently so a graph-query
    # failure never blocks the index job from completing.
    try:
        _derive_and_cache_conventions(repo_name)
    except Exception as _ce:
        logger.warning(f"index_worker: conventions derivation skipped for {repo_name}: {_ce}")

    return {"status": "done", "total": total_new, "indexed": indexed, "local_path": str(local_path)}

# ── Phase 7: codebase conventions derivation ──────────────────

def _derive_and_cache_conventions(repo_name: str) -> None:
    """Derive codebase conventions from the code_graph and cache them in Redis.

    Runs once per successful index run. Builds a JSON blob with:
      base_classes    — most-extended base class names (top 10)
      interfaces      — most-implemented interface names (top 10)
      common_imports  — most-imported targets (top 20)
      naming_pattern  — "snake_case" | "camelCase" | "mixed"
      test_file_pattern — detected test path prefix (e.g. "src/test/java")

    Writes to Redis db=0 at key sdlc:conventions:{repo_name}, TTL 86400 (24h).
    Falls back silently on any DB or Redis error.
    """
    import re as _re
    import json as _json
    import redis as _redis_c
    from db.database import SessionLocal
    from sqlalchemy import text as _sql
    from core.config import REDIS_HOST as _CH, REDIS_PORT as _CP

    try:
        db = SessionLocal()
        try:
            base_rows = db.execute(_sql("""
                SELECT rel->>'target_name' AS base, COUNT(*) AS n
                FROM code_graph, jsonb_array_elements(relations) rel
                WHERE repo = :r AND rel->>'type' = 'extends'
                GROUP BY base ORDER BY n DESC LIMIT 10
            """), {"r": repo_name}).fetchall()

            iface_rows = db.execute(_sql("""
                SELECT rel->>'target_name' AS iface, COUNT(*) AS n
                FROM code_graph, jsonb_array_elements(relations) rel
                WHERE repo = :r AND rel->>'type' = 'implements'
                GROUP BY iface ORDER BY n DESC LIMIT 10
            """), {"r": repo_name}).fetchall()

            import_rows = db.execute(_sql("""
                SELECT rel->>'target_name' AS imp, COUNT(*) AS n
                FROM code_graph, jsonb_array_elements(relations) rel
                WHERE repo = :r AND rel->>'type' = 'imports'
                GROUP BY imp ORDER BY n DESC LIMIT 20
            """), {"r": repo_name}).fetchall()

            name_rows = db.execute(_sql("""
                SELECT name FROM code_graph WHERE repo = :r
                  AND name IS NOT NULL AND length(name) > 3
                LIMIT 500
            """), {"r": repo_name}).fetchall()

            test_path_rows = db.execute(_sql("""
                SELECT file_path FROM code_graph WHERE repo = :r
                  AND (file_path ILIKE '%test%' OR file_path ILIKE '%spec%')
                LIMIT 30
            """), {"r": repo_name}).fetchall()
        finally:
            db.close()

        # ── Naming pattern heuristic ─────────────────────────────────────────
        snake_count = 0
        camel_count = 0
        for (nm,) in name_rows:
            nm = str(nm or "")
            if "_" in nm and nm == nm.lower():
                snake_count += 1
            elif nm and nm[0].islower() and any(c.isupper() for c in nm[1:]):
                camel_count += 1
        if snake_count > camel_count * 1.5:
            naming_pattern = "snake_case"
        elif camel_count > snake_count * 1.5:
            naming_pattern = "camelCase"
        else:
            naming_pattern = "mixed"

        # ── Test path pattern ─────────────────────────────────────────────────
        test_prefixes = {}
        for (fp,) in test_path_rows:
            fp = str(fp or "")
            parts = fp.split("/")
            # Find the deepest directory that contains "test" or "spec"
            for i, p in enumerate(parts):
                if p.lower() in ("test", "tests", "spec", "specs", "__tests__"):
                    prefix = "/".join(parts[:i + 1])
                    test_prefixes[prefix] = test_prefixes.get(prefix, 0) + 1
                    break
        test_file_pattern = max(test_prefixes, key=test_prefixes.get) if test_prefixes else "tests/"

        conventions = {
            "base_classes":    [r[0] for r in base_rows if r[0]],
            "interfaces":      [r[0] for r in iface_rows if r[0]],
            "common_imports":  [r[0] for r in import_rows if r[0]],
            "naming_pattern":  naming_pattern,
            "test_file_pattern": test_file_pattern,
        }

        _rc = _redis_c.Redis(host=_CH, port=_CP, db=0,
                             decode_responses=True, socket_connect_timeout=2)
        _rc.setex(f"sdlc:conventions:{repo_name}", 86400, _json.dumps(conventions))
        logger.info(
            f"index_worker: conventions cached for {repo_name} "
            f"(bases={len(conventions['base_classes'])} "
            f"ifaces={len(conventions['interfaces'])} "
            f"imports={len(conventions['common_imports'])} "
            f"naming={naming_pattern})"
        )
    except Exception as exc:
        logger.warning(f"index_worker: _derive_and_cache_conventions failed for {repo_name}: {exc}")

# ── File collection ────────────────────────────────────────────

def _collect_files(root: Path) -> Iterator[Path]:
    skip_dirs = {
        ".git", ".svn", "node_modules", "__pycache__", ".venv",
        "venv", "dist", "build", ".next", "target", ".idea",
    }
    for fpath in root.rglob("*"):
        if not fpath.is_file():
            continue
        if any(p in fpath.parts for p in skip_dirs):
            continue
        if fpath.suffix.lower() not in SUPPORTED_EXT:
            continue
        if fpath.stat().st_size > MAX_FILE_BYTES:
            continue
        yield fpath


# ── Chunking ───────────────────────────────────────────────────

def _chunk_file(fpath: Path, root: Path) -> tuple[list[dict], list[dict]]:
    """
    Parse a file into semantic chunks and extract code symbols.
    Returns (chunks, symbols) where:
      - chunks: list of dicts for document_embeddings
      - symbols: list of dicts for code_symbols table
    Falls back to line-based chunking with no symbols if tree-sitter unavailable.
    """
    rel_path = str(fpath.relative_to(root))
    try:
        text = fpath.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return [], []

    ext  = fpath.suffix.lower()
    lang = _LANG_MAP.get(ext)

    if _TS_AVAILABLE and lang and lang in _DEFINITION_NODES:
        try:
            return _ts_chunk(text, rel_path, lang)
        except Exception as e:
            logger.debug(f"index_worker: tree-sitter failed for {rel_path} ({lang}): {e} — line fallback")

    # Fallback: line-based chunking, no symbols
    return list(_line_chunk(text, rel_path)), []


def _ts_chunk(source: str, file_path: str, language: str) -> tuple[list[dict], list[dict]]:
    """
    Use tree-sitter to chunk source at definition boundaries and extract symbols.
    Every named definition (class, function, method, interface, etc.) becomes:
      - One chunk in document_embeddings (for semantic search)
      - One row in code_symbols (for exact symbol lookup)
    """
    parser  = _ts_get_parser(language)
    tree    = parser.parse(source.encode("utf-8", errors="replace"))
    lines   = source.splitlines(keepends=True)
    def_types = _DEFINITION_NODES.get(language, set())

    chunks:  list[dict] = []
    symbols: list[dict] = []
    used_lines: set[int] = set()

    def _get_name(node) -> str:
        """Extract the identifier/name child from a definition node."""
        # Try field name 'name' first (works for most languages in tree-sitter)
        name_node = node.child_by_field_name("name")
        if name_node:
            return source[name_node.start_byte:name_node.end_byte].strip()
        # Fallback: first identifier child
        for child in node.children:
            if child.type == "identifier":
                return source[child.start_byte:child.end_byte].strip()
        return ""

    def _get_parent_name(node) -> str:
        """Walk up to find the nearest enclosing class/struct/impl name."""
        parent = node.parent
        while parent:
            if parent.type in {"class_declaration", "class_definition", "class_specifier",
                                "class", "impl_item", "struct_specifier", "struct_item",
                                "object_definition", "object_declaration"}:
                pname = _get_name(parent)
                if pname:
                    return pname
            parent = parent.parent
        return ""

    def _get_signature(node, sym_name: str) -> str:
        """Extract a compact signature string for the symbol."""
        try:
            raw = source[node.start_byte:node.end_byte]
            # Take first line for function/method signatures (return type + params)
            first_line = raw.split("\n")[0].strip()
            if len(first_line) > 200:
                first_line = first_line[:200] + "..."
            return first_line
        except Exception:
            return sym_name

    def _walk(node):
        if node.type in def_types:
            sym_name = _get_name(node)
            if not sym_name:
                return  # anonymous node, skip
            start_line = node.start_point[0]   # 0-indexed
            end_line   = node.end_point[0] + 1  # exclusive
            content    = "".join(lines[start_line:end_line]).strip()
            if not content:
                return

            used_lines.update(range(start_line, end_line))

            chunk = _make_chunk(content, file_path, start_line + 1, end_line, len(chunks))
            # Phase 2 — section_path breadcrumb for code: "<file> > <Symbol>".
            # parent_idx is assigned at the end after we know if a file-level
            # parent will be emitted (only when there are ≥2 leaves).
            chunk["section_path"] = f"{file_path} > {sym_name}"
            chunk["_sym_name"]    = sym_name
            chunk["_line_range"]  = (start_line + 1, end_line)
            chunks.append(chunk)

            sym_type   = _SYMBOL_TYPE_MAP.get(node.type, node.type)
            parent     = _get_parent_name(node)
            signature  = _get_signature(node, sym_name)
            symbols.append({
                "repo":         "",   # filled in by caller with actual repo_name
                "file_path":    file_path,
                "symbol_name":  sym_name,
                "symbol_type":  sym_type,
                "language":     language,
                "line_start":   start_line + 1,
                "line_end":     end_line,
                "signature":    signature,
                "parent_name":  parent,
                "embedding_id": chunk["id"],  # link to the chunk
            })

        for child in node.children:
            _walk(child)

    _walk(tree.root_node)

    # Emit leftover lines not covered by any definition (imports, module-level code)
    remaining = [lines[i] for i in range(len(lines)) if i not in used_lines]
    leftover  = "".join(remaining).strip()
    if leftover:
        start_idx = len(chunks)
        leftover_chunks = list(_line_chunk(leftover, file_path))
        for lc in leftover_chunks:
            lc["chunk_index"] += start_idx
        chunks.extend(leftover_chunks)

    # Phase 2 — file-level parent symmetry with markdown side
    # (store/docs_store.py:_chunk_document_structured). When the file produced
    # ≥2 leaves, prepend one parent chunk whose body is a compact outline
    # (one line per symbol with line range). Every leaf gets parent_idx=0 so
    # hybrid_retriever's parent-expansion can swap a hot leaf for the file
    # outline — same behavior as docs.
    if len(chunks) >= 2:
        outline_lines = [f"# Outline: {file_path}"]
        max_end = 0
        for ch in chunks:
            nm = ch.get("_sym_name") or ""
            lr = ch.get("_line_range") or (ch.get("line_start") or 0, ch.get("line_end") or 0)
            ls, le = lr
            outline_lines.append(f"- {nm or 'block'} (L{ls}-L{le})")
            if le and le > max_end:
                max_end = le
        # Bump every existing leaf's chunk_index by 1 so the parent can sit at 0
        # without violating the unique (repo, file_path, chunk_index) constraint.
        for ch in chunks:
            ch["chunk_index"] = (ch.get("chunk_index") or 0) + 1
            ch["parent_idx"]  = 0
            # Drop scratch keys so they don't leak into metadata.
            ch.pop("_sym_name", None)
            ch.pop("_line_range", None)
        parent = _make_chunk(
            "\n".join(outline_lines),
            file_path,
            1,
            max_end or 1,
            0,
        )
        parent["section_path"]      = file_path
        parent["is_section_parent"] = True
        parent["parent_idx"]        = None
        chunks.insert(0, parent)
    else:
        # Single-leaf file: drop scratch keys; no parent needed.
        for ch in chunks:
            ch.pop("_sym_name", None)
            ch.pop("_line_range", None)

    return chunks, symbols


def _line_chunk(text: str, file_path: str) -> Iterator[dict]:
    """Split text into CHUNK_SIZE-character windows with CHUNK_OVERLAP."""
    char_size    = CHUNK_SIZE * 4
    char_overlap = CHUNK_OVERLAP * 4

    idx = 0
    i = 0
    while i < len(text):
        chunk = text[i:i + char_size]
        if chunk.strip():
            yield _make_chunk(chunk, file_path, None, None, idx)
            idx += 1
        i += char_size - char_overlap
        if i < 0:
            break


def _make_chunk(content: str, file_path: str, line_start, line_end, chunk_index: int = 0) -> dict:
    content_hash = hashlib.sha256(content.encode()).digest().hex()
    return {
        "id":           str(uuid.uuid4()),
        "content":      content[:ENRICH_CONTENT_MAX],
        "file_path":    file_path,
        "chunk_index":  chunk_index,
        "line_start":   line_start,
        "line_end":     line_end,
        "content_hash": content_hash,
        # Phase 2 — code-chunk parent symmetry (defaults; _ts_chunk overrides).
        # section_path matches the markdown-side breadcrumb shape so
        # hybrid_retriever's parent-expansion + section-coverage signals work
        # for code repos identically to KB docs.
        "section_path":      file_path,
        "is_section_parent": False,
        "parent_idx":        None,
    }


# ── Incremental dedup ─────────────────────────────────────────

def _filter_new_chunks(repo_name: str, chunks: list[dict]) -> list[dict]:
    """
    Return only chunks whose content_hash is not already in pgvector.
    Uses a single IN query to check all hashes at once.
    """
    from db.database import VectorSessionLocal
    from sqlalchemy import text as _sql

    hashes = [c["content_hash"] for c in chunks]
    if not hashes:
        return chunks

    try:
        db = VectorSessionLocal()
        try:
            rows = db.execute(
                _sql("SELECT content_hash FROM document_embeddings WHERE repo = :repo AND content_hash = ANY(:hashes)"),
                {"repo": f"repo_{repo_name}", "hashes": hashes},
            ).fetchall()
            existing = {r[0] for r in rows}
        finally:
            db.close()
        return [c for c in chunks if c["content_hash"] not in existing]
    except Exception as e:
        logger.warning(f"index_worker: dedup query failed ({e}) — indexing all chunks")
        return chunks


# ── Embed service call ────────────────────────────────────────

# nomic-embed-text hard limits:
#   max sequence length: 8192 tokens  ≈ 6000 UTF-8 chars for code
#   empty input → 400 Bad Request
#   null bytes in input → SentencePiece tokenizer crash → 500
_EMBED_MAX_CHARS = 1000
_EMBED_PLACEHOLDER = "<empty>"   # fallback for chunks that become empty after strip

def _sanitize_for_embed(text: str, file_path: str = "") -> str:
    """
    Clean text before sending to nomic-embed-text via Ollama.
    Logs whenever content is modified so the problematic file is visible in logs.
    """
    tag = f"[{file_path}]" if file_path else ""
    if not text:
        logger.warning(f"embed_sanitize{tag}: empty content — using placeholder")
        return _EMBED_PLACEHOLDER

    # Strip null bytes and ASCII control chars (except \t \n \r)
    cleaned = "".join(
        ch for ch in text
        if ch in ("\t", "\n", "\r") or (ord(ch) >= 32 and ord(ch) != 127)
    )

    null_count = len(text) - len(cleaned)
    if null_count:
        logger.warning(
            f"embed_sanitize{tag}: stripped {null_count} control/null chars from "
            f"{len(text)}-char chunk — THIS is likely what was crashing nomic"
        )

    cleaned = cleaned.strip()
    if not cleaned:
        logger.warning(f"embed_sanitize{tag}: content became empty after strip — using placeholder")
        return _EMBED_PLACEHOLDER

    if len(cleaned) > _EMBED_MAX_CHARS:
        logger.info(
            f"embed_sanitize{tag}: truncated {len(cleaned)} → {_EMBED_MAX_CHARS} chars"
        )
        return cleaned[:_EMBED_MAX_CHARS]

    return cleaned


def _call_embed_svc(texts: list[str]) -> list[list[float]]:
    """
    Call embed svc with round-robin across the _EMBED_SVC_POOL and exponential
    backoff retry.  Only retries on 5xx / network errors — 4xx (bad request) raises
    immediately since retrying won't fix a malformed payload.

    No direct-Ollama fallback: embed svc is the only path in production.
    If embed svc is down, this blocks with increasing delays (up to 5 min) until
    it recovers rather than failing the wave.
    """
    attempt = 0
    total_chars = sum(len(t) for t in texts)
    logger.debug(
        f"index_worker [→embed_svc]: texts={len(texts)} total_chars={total_chars} "
        f"pool={_EMBED_SVC_POOL}"
    )
    while True:
        url = _next_embed_url()
        try:
            resp = httpx.post(
                f"{url}/embed",
                json={"texts": texts, "provider": "ollama"},
                timeout=120.0,
            )
            resp.raise_for_status()
            logger.debug(f"index_worker [←embed_svc]: {url} OK texts={len(texts)}")
            return resp.json()["embeddings"]
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500]
            if 400 <= exc.response.status_code < 500:
                logger.error(
                    f"index_worker [←embed_svc 4xx]: {url} → HTTP {exc.response.status_code} "
                    f"body={body!r} | texts={len(texts)} total_chars={total_chars} — not retrying"
                )
                raise
            # 5xx — embed svc itself failed (Ollama error propagated up)
            logger.warning(
                f"index_worker [←embed_svc 5xx]: {url} → HTTP {exc.response.status_code} "
                f"body={body!r} | attempt={attempt + 1}"
            )
            last_exc: Exception = exc
        except httpx.TimeoutException as exc:
            logger.warning(
                f"index_worker [←embed_svc timeout]: {url} timed out after 120s "
                f"| texts={len(texts)} attempt={attempt + 1}"
            )
            last_exc = exc
        except Exception as exc:
            logger.warning(
                f"index_worker [←embed_svc error]: {url} {type(exc).__name__}: {exc} "
                f"| attempt={attempt + 1}"
            )
            last_exc = exc

        attempt += 1
        wait = min(10 * (2 ** min(attempt - 1, 5)), 300)
        logger.warning(
            f"index_worker [embed_svc retry]: attempt {attempt} — retry in {wait}s | "
            f"last_error={last_exc}"
        )
        time.sleep(wait)


# ── pgvector bulk upsert ──────────────────────────────────────

def _bulk_upsert(repo_name: str, chunks: list[dict], embeddings: list[list[float]], product_id: str = "", department: str = "", branch: str = "") -> None:
    """
    Batch upsert using executemany — 500 rows per round-trip.
    ON CONFLICT uses the partial unique index on (content_hash, repo).
    """
    from db.database import vector_engine
    from sqlalchemy import text as _sql

    _CODE_EXTS = {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".cpp",
        ".c", ".cs", ".rb", ".php", ".swift", ".kt", ".scala", ".sh", ".sql",
    }
    _DOC_EXTS  = {".md", ".rst", ".txt", ".pdf", ".docx", ".html", ".xml"}

    def _classify(file_path: str) -> str:
        ext = "." + file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
        if ext in _CODE_EXTS:  return "code"
        if ext in _DOC_EXTS:   return "doc"
        return "general"

    def _clean(text: str) -> str:
        return "".join(
            ch for ch in (text or "")
            if ch in ("\t", "\n", "\r") or (32 <= ord(ch) < 127) or ord(ch) > 127
        )

    # Phase 2 — resolve parent_idx → parent_chunk_id (UUID lookup) BEFORE
    # building rows so leaves can carry their parent's UUID at insert time.
    # Parents are always emitted before their children in the chunks list
    # (see _ts_chunk and _attach_parent), so the existing chunk order also
    # keeps parents inserted first under SAVEPOINT-based retry.
    _chunk_ids = [c.get("id") for c in chunks]

    rows = [
        {
            "id":             chunk["id"],
            "repo":           f"repo_{repo_name}",
            "file_path":      chunk.get("file_path", ""),
            "chunk_index":    chunk.get("chunk_index", i),
            "content":        _clean(chunk["content"]),
            "content_hash":   chunk["content_hash"],
            "embedding":      json.dumps(emb),
            "metadata":       json.dumps({
                **(chunk.get("metadata") or {}),
                **({"product_id": product_id} if product_id else {}),
                **({"department": department} if department else {}),
            }),
            "line_start":     chunk.get("line_start"),
            "line_end":       chunk.get("line_end"),
            "classification": chunk.get("classification") or _classify(chunk.get("file_path", "")),
            "allowed_roles":  "[]",
            "allowed_users":  "[]",
            "product_id":     product_id or "",
            "department":     department or "",
            "branch":         branch or None,
            # Phase 2 — section-aware chunking + parent linkage (code path).
            # parent_chunk_id resolves the parent's UUID via the pre-computed
            # _chunk_ids list. is_section_parent flags the file-outline row.
            "parent_chunk_id":    (
                _chunk_ids[chunk["parent_idx"]]
                if (chunk.get("parent_idx") is not None
                    and 0 <= chunk["parent_idx"] < len(_chunk_ids))
                else None
            ),
            "section_path":       chunk.get("section_path") or None,
            "is_section_parent":  bool(chunk.get("is_section_parent", False)),
            # Phase 1 closure — chunk-level active-version filter. Code chunks
            # are always 'ACTIVE' on insert; KB doc deprecation flow is handled
            # in store/docs_store.activate_doc, not here.
            "status":             "ACTIVE",
        }
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings))
    ]

    # Deduplicate by (content_hash, repo) to avoid violating uq_doc_embed_content_hash
    # when multiple files in the same batch contain identical content.
    seen_hashes: set = set()
    deduped_rows = []
    for row in rows:
        h = row.get("content_hash")
        key = (h, row["repo"]) if h else None
        if key and key in seen_hashes:
            continue
        if key:
            seen_hashes.add(key)
        deduped_rows.append(row)
    rows = deduped_rows

    sql = _sql("""
        INSERT INTO document_embeddings
            (id, repo, file_path, chunk_index, content, content_hash, embedding, metadata,
             line_start, line_end, classification, allowed_roles, allowed_users,
             product_id, department, branch,
             parent_chunk_id, section_path, is_section_parent, status,
             created_at)
        VALUES
            (CAST(:id AS uuid), :repo, :file_path, :chunk_index, :content, :content_hash,
             CAST(:embedding AS vector), CAST(:metadata AS jsonb),
             :line_start, :line_end, :classification,
             CAST(:allowed_roles AS jsonb), CAST(:allowed_users AS jsonb),
             CAST(NULLIF(:product_id, '') AS uuid), NULLIF(:department, ''), NULLIF(:branch, ''),
             CAST(:parent_chunk_id AS uuid), :section_path, :is_section_parent, :status,
             NOW())
        ON CONFLICT (repo, file_path, chunk_index)
        DO UPDATE SET
            content         = EXCLUDED.content,
            content_hash    = EXCLUDED.content_hash,
            embedding       = EXCLUDED.embedding,
            metadata        = EXCLUDED.metadata,
            line_start      = EXCLUDED.line_start,
            line_end        = EXCLUDED.line_end,
            classification  = EXCLUDED.classification,
            allowed_roles   = EXCLUDED.allowed_roles,
            allowed_users   = EXCLUDED.allowed_users,
            product_id      = COALESCE(EXCLUDED.product_id, document_embeddings.product_id),
            department        = COALESCE(EXCLUDED.department, document_embeddings.department),
            branch            = EXCLUDED.branch,
            parent_chunk_id   = EXCLUDED.parent_chunk_id,
            section_path      = EXCLUDED.section_path,
            is_section_parent = EXCLUDED.is_section_parent,
            status            = EXCLUDED.status
    """)

    from sqlalchemy.exc import IntegrityError as _IntegrityError
    from sqlalchemy import text as _text_sp
    with vector_engine.begin() as conn:
        for i in range(0, len(rows), BATCH_INSERT):
            batch = rows[i:i + BATCH_INSERT]
            try:
                conn.execute(_text_sp("SAVEPOINT chunk_ins"))
                conn.execute(sql, batch)
                conn.execute(_text_sp("RELEASE SAVEPOINT chunk_ins"))
            except _IntegrityError as _ie:
                conn.execute(_text_sp("ROLLBACK TO SAVEPOINT chunk_ins"))
                if "uq_doc_embed_content_hash" not in str(_ie):
                    raise
                # Content-hash collision: two files share identical content.
                # Fall back to per-row upsert, skipping duplicate-hash rows.
                skipped = 0
                for row in batch:
                    try:
                        conn.execute(_text_sp("SAVEPOINT row_ins"))
                        conn.execute(sql, [row])
                        conn.execute(_text_sp("RELEASE SAVEPOINT row_ins"))
                    except _IntegrityError as _row_ie:
                        conn.execute(_text_sp("ROLLBACK TO SAVEPOINT row_ins"))
                        if "uq_doc_embed_content_hash" not in str(_row_ie):
                            raise
                        skipped += 1
                if skipped:
                    logger.debug(
                        f"index_worker: skipped {skipped} duplicate-content chunks "
                        f"(uq_doc_embed_content_hash) in {batch[0]['repo']}"
                    )


def _bulk_upsert_symbols(repo_name: str, symbols: list[dict]) -> None:
    """
    Bulk upsert code symbols. Uses ON CONFLICT DO UPDATE so re-indexing
    the same file updates existing symbols rather than duplicating.
    Processes in batches of 500.
    """
    if not symbols:
        return
    from db.database import SessionLocal
    from sqlalchemy import text as _sql

    sql = _sql("""
        INSERT INTO code_symbols
            (repo, file_path, symbol_name, symbol_type, language,
             line_start, line_end, signature, parent_name, embedding_id, created_at)
        VALUES
            (:repo, :file_path, :symbol_name, :symbol_type, :language,
             :line_start, :line_end, :signature, :parent_name, :embedding_id, NOW())
        ON CONFLICT DO NOTHING
    """)

    try:
        db = SessionLocal()
        try:
            for i in range(0, len(symbols), 500):
                db.execute(sql, symbols[i:i + 500])
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"index_worker: symbol upsert failed: {e}")


# ── Status helpers ─────────────────────────────────────────────

def _update_status(
    repo_name:               str,
    status:                  str,
    triggered_by:            str  = "system",
    total_chunks:            int  = 0,
    indexed_chunks:          int  = 0,
    error_msg:               str  = None,
    git_url:                 str  = None,
    branch:                  str  = None,
    build_root:              str  = None,
) -> None:
    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _sql
        db = SessionLocal()
        try:
            db.execute(_sql("""
                INSERT INTO repo_index_status
                    (repo_name, status, triggered_by, total_chunks, indexed_chunks,
                     started_at, completed_at, error_msg, git_url, branch, build_root)
                VALUES
                    (:repo, :status, :by, :total, :indexed,
                     CASE WHEN :status = 'running' THEN NOW() ELSE NULL END,
                     CASE WHEN :status IN ('done','failed') THEN NOW() ELSE NULL END,
                     :err, :git_url, :branch, :build_root)
                ON CONFLICT (repo_name) DO UPDATE SET
                    status         = EXCLUDED.status,
                    triggered_by   = EXCLUDED.triggered_by,
                    total_chunks   = CASE WHEN EXCLUDED.total_chunks > 0 THEN EXCLUDED.total_chunks
                                         ELSE repo_index_status.total_chunks END,
                    indexed_chunks = CASE WHEN EXCLUDED.indexed_chunks > 0 THEN EXCLUDED.indexed_chunks
                                         ELSE repo_index_status.indexed_chunks END,
                    started_at     = CASE WHEN EXCLUDED.started_at IS NOT NULL THEN EXCLUDED.started_at
                                         ELSE repo_index_status.started_at END,
                    completed_at   = EXCLUDED.completed_at,
                    error_msg      = EXCLUDED.error_msg,
                    git_url        = CASE WHEN EXCLUDED.git_url IS NOT NULL THEN EXCLUDED.git_url
                                         ELSE repo_index_status.git_url END,
                    branch         = CASE WHEN EXCLUDED.branch IS NOT NULL THEN EXCLUDED.branch
                                         ELSE repo_index_status.branch END,
                    build_root     = CASE WHEN EXCLUDED.build_root IS NOT NULL THEN EXCLUDED.build_root
                                         ELSE repo_index_status.build_root END
            """), {
                "repo":       repo_name,
                "status":     status,
                "by":         triggered_by,
                "total":      total_chunks,
                "indexed":    indexed_chunks,
                "err":        error_msg,
                "git_url":    git_url,
                "branch":     branch or None,
                "build_root": build_root,
            })
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"index_worker: status update failed: {e}")


def _update_index_request(request_id: str | None, status: str, error_msg: str = None) -> None:
    """Update index_requests.status so index_router /index/repos reflects final state."""
    if not request_id:
        return
    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _sql
        db = SessionLocal()
        try:
            db.execute(_sql("""
                UPDATE index_requests
                SET status = :status,
                    error_msg = :err,
                    updated_at = NOW()
                WHERE id = CAST(:id AS uuid)
            """), {"id": request_id, "status": status, "err": error_msg})
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"index_worker: index_request status update failed: {e}")


def _update_progress(repo_name: str, total: int, indexed: int) -> None:
    """Lightweight progress update (no started_at/completed_at change)."""
    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _sql
        db = SessionLocal()
        try:
            db.execute(_sql("""
                UPDATE repo_index_status
                SET total_chunks = :total, indexed_chunks = :indexed
                WHERE repo_name = :repo
            """), {"repo": repo_name, "total": total, "indexed": indexed})
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


def _delete_repo_chunks(repo_name: str) -> None:
    from db.database import vector_engine, SessionLocal
    from sqlalchemy import text as _sql
    with vector_engine.begin() as conn:
        conn.execute(
            _sql("DELETE FROM document_embeddings WHERE repo = :repo"),
            {"repo": f"repo_{repo_name}"},
        )
    # Also clear symbol and graph index for this repo
    try:
        db = SessionLocal()
        try:
            db.execute(
                _sql("DELETE FROM code_symbols WHERE repo = :repo"),
                {"repo": repo_name},
            )
            db.execute(
                _sql("DELETE FROM code_graph WHERE repo = :repo"),
                {"repo": repo_name},
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"index_worker: symbol/graph delete failed: {e}")


# ── Git helpers ───────────────────────────────────────────────

def _resolve_path(repo_name: str, repo_path: str, branch: str = "") -> Path:
    """If repo_path is a local dir, return it. If HTTPS URL, clone/pull the given branch."""
    if os.path.isdir(repo_path):
        return Path(repo_path)

    if repo_path.startswith("https://") or repo_path.startswith("git@"):
        clone_root = Path(os.getenv("REPO_CLONE_ROOT", "/tmp/ainxt_repos"))
        clone_root.mkdir(parents=True, exist_ok=True)
        # Each branch gets its own subdirectory so the same repo indexed on multiple
        # branches (e.g. main vs feature/xyz) never clobber each other.
        branch_slug = (branch or "default").replace("/", "_").replace("\\", "_")
        dest = clone_root / repo_name / branch_slug

        # repo_path is expected to already carry the user's PAT (injected by index_router
        # before the job is enqueued). No token resolution here — admin credentials must
        # never be used, and there is no user context available in the worker.
        authed_url = repo_path

        # Disable ALL interactive git prompts — fail immediately if auth is wrong
        git_env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS":         "/usr/bin/false" if os.path.exists("/usr/bin/false") else "/bin/false",
            "GIT_SSH_COMMAND":     "ssh -o BatchMode=yes",
        }

        _git = ["git", "-c", "http.proxy=", "-c", "https.proxy="]

        if dest.exists():
            branch_info = f" branch={branch}" if branch else " (default branch)"
            logger.info(f"index_worker: updating {repo_name}{branch_info}")
            # Rotate PAT in remote URL (token may have changed since last index)
            subprocess.run(
                _git + ["-C", str(dest), "remote", "set-url", "origin", authed_url],
                capture_output=True, env=git_env,
            )
            if branch:
                # Fetch only the target branch (shallow), checkout, then hard-reset
                subprocess.run(
                    _git + ["-C", str(dest), "fetch", "--depth=1", "origin", branch],
                    capture_output=True, timeout=120, env=git_env,
                )
                subprocess.run(
                    _git + ["-C", str(dest), "checkout", branch],
                    capture_output=True, env=git_env,
                )
                r = subprocess.run(
                    _git + ["-C", str(dest), "reset", "--hard", "FETCH_HEAD"],
                    capture_output=True, timeout=60, env=git_env,
                )
            else:
                r = subprocess.run(
                    _git + ["-C", str(dest), "pull", "--ff-only"],
                    capture_output=True, timeout=120, env=git_env,
                )
            if r.returncode != 0:
                logger.warning(f"index_worker: git update failed for {repo_name}: {r.stderr.decode(errors='replace')[:500]}")
        else:
            branch_args = ["--branch", branch] if branch else []
            branch_info = f" branch={branch}" if branch else ""
            logger.info(f"index_worker: cloning {repo_name} (depth=1){branch_info}")
            r = subprocess.run(
                _git + ["clone", "--depth=1"] + branch_args + [authed_url, str(dest)],
                capture_output=True, timeout=300, env=git_env,
            )
            if r.returncode != 0:
                err = r.stderr.decode(errors="replace")[:800]
                import re as _re
                err_safe = _re.sub(r'https://[^@]+@', 'https://***@', err)
                logger.error(f"index_worker: git clone failed for {repo_name}: {err_safe}")
                raise RuntimeError(f"git clone failed (exit {r.returncode}): {err_safe}")
        return dest

    raise ValueError(f"repo_path must be a local directory or HTTPS git URL: {repo_path!r}")
