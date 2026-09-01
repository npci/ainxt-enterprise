# SPDX-License-Identifier: Apache-2.0
# ============================================================
# TREE-SITTER AST CHUNKER — Universal Language Support
# ============================================================
# Chunks code files at function/class/method boundaries rather
# than at fixed character counts. This dramatically improves RAG
# retrieval quality for Java, Go, Kotlin, JS, TS, Rust etc.
#
# Usage:
#   from workers.tree_sitter_chunker import chunk_with_treesitter
#   chunks = chunk_with_treesitter(content, "java")
#   # Returns: [{"text": str, "metadata": {...}}, ...]
#
# Install deps:
#   pip install tree-sitter
#   pip install tree-sitter-java tree-sitter-javascript
#   pip install tree-sitter-typescript tree-sitter-go
# ============================================================

import re
from typing import List, Dict, Optional, Any
from core.logger import logger

# ============================================================
# LANGUAGE EXTENSION MAP
# ============================================================

EXTENSION_TO_LANG = {
    ".java":  "java",
    ".js":    "javascript",
    ".jsx":   "javascript",
    ".ts":    "typescript",
    ".tsx":   "tsx",
    ".go":    "go",
    ".kt":    "kotlin",
    ".kts":   "kotlin",
    ".scala": "scala",
    ".rs":    "rust",
    ".c":     "c",
    ".cpp":   "cpp",
    ".cc":    "cpp",
    ".h":     "c",
    ".hpp":   "cpp",
    ".cs":    "c_sharp",
    ".rb":    "ruby",
    ".php":   "php",
}

# ============================================================
# NODE TYPES THAT DEFINE CHUNK BOUNDARIES PER LANGUAGE
# ============================================================

_CHUNK_NODE_TYPES = {
    "java":       {"method_declaration", "constructor_declaration",
                   "class_declaration", "interface_declaration",
                   "annotation_type_declaration", "enum_declaration"},
    "javascript": {"function_declaration", "function_expression",
                   "arrow_function", "method_definition", "class_declaration"},
    "typescript": {"function_declaration", "function_expression",
                   "arrow_function", "method_definition", "class_declaration",
                   "interface_declaration", "type_alias_declaration"},
    "tsx":        {"function_declaration", "function_expression",
                   "arrow_function", "method_definition", "class_declaration"},
    "go":         {"function_declaration", "method_declaration",
                   "type_declaration", "type_spec"},
    "kotlin":     {"function_declaration", "class_declaration",
                   "object_declaration", "secondary_constructor"},
    "scala":      {"def", "class", "object", "trait"},
    "rust":       {"function_item", "impl_item", "struct_item",
                   "enum_item", "trait_item"},
    "c":          {"function_definition", "struct_specifier"},
    "cpp":        {"function_definition", "class_specifier",
                   "struct_specifier", "namespace_definition"},
    "c_sharp":    {"method_declaration", "class_declaration",
                   "interface_declaration", "constructor_declaration"},
    "ruby":       {"method", "singleton_method", "class", "module"},
    "php":        {"function_definition", "method_declaration",
                   "class_declaration"},
}


# ============================================================
# TREE-SITTER PARSER CACHE
# (parsers are expensive to initialise — reuse across calls)
# ============================================================

_parser_cache: Dict[str, Any] = {}


def _get_parser(lang: str):
    """Return a cached tree-sitter parser for the given language."""
    if lang in _parser_cache:
        return _parser_cache[lang]

    try:
        import tree_sitter_languages as _tsl
        parser = _tsl.get_parser(lang)
        _parser_cache[lang] = parser
        return parser
    except Exception:
        pass

    # Fallback: try individual tree-sitter-X packages
    try:
        import tree_sitter as ts
        lang_mod_name = f"tree_sitter_{lang.replace('-', '_')}"
        lang_mod = __import__(lang_mod_name)
        language = ts.Language(lang_mod.language())
        parser = ts.Parser(language)
        _parser_cache[lang] = parser
        return parser
    except Exception as e:
        logger.debug(f"[TreeSitter] no parser for {lang}: {e}")
        _parser_cache[lang] = None
        return None


# ============================================================
# MAIN CHUNKER
# ============================================================

_MAX_CHUNK_CHARS = 4000   # cap so embeddings stay within context limits
_MIN_CHUNK_CHARS = 80     # ignore tiny nodes (single-line annotations etc.)


def chunk_with_treesitter(
    content: str,
    lang: str,
    file_path: str = "",
    fallback_size: int = 1500,
) -> List[Dict]:
    """
    Parse `content` using tree-sitter and return chunks at AST boundaries.

    Each chunk dict:
        {
          "text":         str,            # full chunk text (signature + body)
          "section_path": str,            # Phase 2: breadcrumb e.g. "src/foo.py > Foo > bar"
          "is_parent":    bool,           # Phase 2: True for the file/container parent row
          "parent_idx":   int | None,     # Phase 2: leaf → index of its parent in this list
          "metadata": {
            "file_path":    str,
            "language":     str,
            "node_type":    str,
            "name":         str,          # function/class name if extractable
            "line_start":   int,
            "line_end":     int,
          }
        }

    Phase 2 (parent symmetry with markdown side, store/docs_store.py:
    _chunk_document_structured): when the file has ≥2 leaf chunks, we prepend
    a single file-level parent chunk whose body is a compact outline of every
    leaf (name + line range). This mirrors the markdown structure graph:
    retrieve a hot leaf → fast-tier parent-expansion swaps it for the parent
    so the reasoner sees the whole file's outline, not just one function.

    Falls back to fixed-size chunking if tree-sitter parser unavailable.
    """
    if not content or not content.strip():
        return []

    parser = _get_parser(lang)
    if parser is None:
        logger.debug(f"[TreeSitter] {lang}: parser unavailable, using fixed-size fallback")
        return _attach_parent(_fixed_size_chunks(content, fallback_size, file_path, lang), file_path)

    try:
        tree = parser.parse(content.encode("utf-8", errors="replace"))
        chunk_types = _CHUNK_NODE_TYPES.get(lang, set())
        chunks = _walk_tree(tree.root_node, content, chunk_types, file_path, lang)

        if not chunks:
            # File has no function/class nodes (e.g. config file, constants)
            return _attach_parent(_fixed_size_chunks(content, fallback_size, file_path, lang), file_path)

        return _attach_parent(chunks, file_path)

    except Exception as e:
        logger.warning(f"[TreeSitter] parse failed for {lang} ({file_path}): {e}")
        return _attach_parent(_fixed_size_chunks(content, fallback_size, file_path, lang), file_path)


def _attach_parent(leaves: List[Dict], file_path: str) -> List[Dict]:
    """
    Phase 2 — wrap a list of leaf chunks with a file-level parent so leaves can
    point at it via parent_idx. Symmetry with the markdown structured chunker
    (store/docs_store.py:_chunk_document_structured) so the same fast-tier
    parent-expansion path in hybrid_retriever fires for code AND docs.

    - Files with 0 or 1 leaves stay as-is (no parent — nothing to expand).
    - Otherwise prepend a parent at index 0 whose text is a compact outline of
      every leaf's name + line range, and every leaf gets parent_idx=0.
    - section_path on a leaf is "<file_path> > <name or node_type>" so the
      retriever's section-coverage signals work for code repos too.
    """
    if len(leaves) < 2:
        # Stamp section_path even on single-leaf outputs so downstream code
        # has a uniform shape — but no parent_idx (no parent emitted).
        for ch in leaves:
            meta = ch.get("metadata") or {}
            ch.setdefault("section_path", _leaf_section_path(file_path, meta))
            ch.setdefault("is_parent", False)
            ch.setdefault("parent_idx", None)
        return leaves

    # Build the parent outline: one line per leaf — kept short on purpose so
    # the parent embedding is dominated by the file-level signal, not the body.
    outline_lines = [f"# Outline: {file_path}"]
    for i, ch in enumerate(leaves):
        meta = ch.get("metadata") or {}
        nm = (meta.get("name") or meta.get("node_type") or f"chunk_{i}").strip()
        ls = meta.get("line_start") or 0
        le = meta.get("line_end")   or 0
        outline_lines.append(f"- {nm} (L{ls}-L{le})")
    parent = {
        "text":         "\n".join(outline_lines),
        "section_path": file_path,
        "is_parent":    True,
        "parent_idx":   None,
        "metadata": {
            "file_path":  file_path,
            "language":   (leaves[0].get("metadata") or {}).get("language", "text"),
            "node_type":  "file_outline",
            "name":       file_path,
            "line_start": 1,
            "line_end":   (leaves[-1].get("metadata") or {}).get("line_end", 1),
        },
    }
    # Leaves: parent_idx=0 references the parent we're prepending, section_path
    # is "<file> > <leaf_name>".
    for ch in leaves:
        meta = ch.get("metadata") or {}
        ch["section_path"] = _leaf_section_path(file_path, meta)
        ch["is_parent"]    = False
        ch["parent_idx"]   = 0
    return [parent] + leaves


def _leaf_section_path(file_path: str, meta: Dict) -> str:
    """Breadcrumb for a code-chunk leaf — file > symbol."""
    nm = (meta.get("name") or meta.get("node_type") or "").strip()
    return f"{file_path} > {nm}" if nm else file_path


def _walk_tree(
    node,
    content: str,
    chunk_types: set,
    file_path: str,
    lang: str,
) -> List[Dict]:
    """DFS walk; collect chunks at nodes whose type is in chunk_types."""
    results = []
    visited_ranges: List[tuple] = []  # avoid overlapping chunks

    def _visit(n):
        if n.type in chunk_types:
            start_byte = n.start_byte
            end_byte   = n.end_byte
            # Skip if this range is already covered by a parent chunk
            for (vs, ve) in visited_ranges:
                if start_byte >= vs and end_byte <= ve:
                    return
            text = content[start_byte:end_byte]
            if len(text) < _MIN_CHUNK_CHARS:
                return

            # Split very large nodes (e.g. God classes) into sub-chunks
            if len(text) > _MAX_CHUNK_CHARS:
                sub = _fixed_size_chunks(text, _MAX_CHUNK_CHARS, file_path, lang,
                                         line_offset=n.start_point[0])
                results.extend(sub)
                visited_ranges.append((start_byte, end_byte))
                return

            results.append({
                "text": text,
                "metadata": {
                    "file_path":  file_path,
                    "language":   lang,
                    "node_type":  n.type,
                    "name":       _extract_name(n, content),
                    "line_start": n.start_point[0] + 1,
                    "line_end":   n.end_point[0] + 1,
                },
            })
            visited_ranges.append((start_byte, end_byte))
        else:
            for child in n.children:
                _visit(child)

    _visit(node)
    return results


def _extract_name(node, content: str) -> str:
    """Try to find the identifier child of a declaration node."""
    for child in node.children:
        if child.type in ("identifier", "name", "simple_identifier",
                          "type_identifier", "field_identifier"):
            return content[child.start_byte:child.end_byte]
    return ""


def _fixed_size_chunks(
    content: str,
    size: int,
    file_path: str,
    lang: str,
    line_offset: int = 0,
) -> List[Dict]:
    """Fallback: split content into overlapping fixed-size character chunks."""
    chunks = []
    step = size - 200  # 200-char overlap for continuity
    lines = content.split("\n")
    char_pos = 0
    chunk_start_line = line_offset

    for i in range(0, len(content), max(step, 1)):
        chunk = content[i : i + size]
        if not chunk.strip():
            continue
        n_lines = chunk.count("\n")
        chunks.append({
            "text": chunk,
            "metadata": {
                "file_path":  file_path,
                "language":   lang,
                "node_type":  "fixed_chunk",
                "name":       "",
                "line_start": chunk_start_line + 1,
                "line_end":   chunk_start_line + n_lines + 1,
            },
        })
        chunk_start_line += n_lines

    return chunks


# ============================================================
# CONVENIENCE: chunk a file by extension
# ============================================================

def chunk_file_by_extension(
    file_path: str,
    content: str,
    fallback_size: int = 1500,
) -> List[Dict]:
    """
    Detect language from file extension and chunk accordingly.
    Returns list of chunk dicts (same format as chunk_with_treesitter).
    """
    import os
    ext = os.path.splitext(file_path)[1].lower()
    lang = EXTENSION_TO_LANG.get(ext)
    if lang:
        return chunk_with_treesitter(content, lang, file_path, fallback_size)
    # Unknown extension — plain fixed-size
    return _fixed_size_chunks(content, fallback_size, file_path, ext or "text")
