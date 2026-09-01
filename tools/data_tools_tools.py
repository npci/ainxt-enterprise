# SPDX-License-Identifier: Apache-2.0
"""
AiNxt Agentic Platform — data_tools MCP tools.

Tabular analysis over configured CSV/XLSX sources (or a warehouse). Used by
UC-69 (reconciliation), UC-70 (budget variance), UC-83 (survey analysis),
UC-84 (VoC theme extraction), UC-90 (data analysis & charting), UC-91
(dashboard generation), UC-97 (churn risk scoring). All read-only on input
data; charts and reports are written to the outbox.

Functions exposed:
  list_tables       — list CSV/XLSX sources under data_dir
  describe_table    — schema + sample rows + numeric summary
  query_table       — pandas-style filter / group_by / aggregate
  variance_report   — budget vs. actual variance with flagging
  reconcile         — match two transaction tables on fuzzy ref + amount
  make_chart        — render line/bar/scatter PNG to the charts outbox

Companion server: mcp/servers/data_tools_server.py
Registered in:   mcp/registry.py:_register_tools()

Configuration (env vars):
  DATA_TOOLS_DATA_DIR    — root for tabular sources (default ./data/tables)
  DATA_TOOLS_CHARTS_DIR  — where chart PNGs land
                            (default ./outbox/mcp/charts)
  DATA_TOOLS_READ_ONLY   — informational tag; tools never write input data
                            (default "true")
"""

import ast
import os
from typing import List

import pandas as pd

# Force a non-interactive backend before importing pyplot so it works under
# headless gunicorn / uvicorn workers.
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


# ── Configuration ────────────────────────────────────────────────────────────

_DATA_DIR   = os.getenv("DATA_TOOLS_DATA_DIR",   "./data/tables")
_CHARTS_DIR = os.getenv("DATA_TOOLS_CHARTS_DIR", "./outbox/mcp/charts")
_READ_ONLY  = os.getenv("DATA_TOOLS_READ_ONLY",  "true").lower() in {"1", "true", "yes"}


# ── Helpers ──────────────────────────────────────────────────────────────────

# SEC-F-002 (2026-08-26): safe filter evaluator for query_table().
#
# The original finding: filter_expr (an LLM-agent-controllable string) was
# passed straight to pandas.DataFrame.query(), which parses and evaluates an
# expression — a classic injection surface (query() with engine="python" can
# reach attribute access / callables depending on the expression shape). A
# first attempted fix used bare substring matching on "os"/"sys" to block
# dangerous-looking expressions; that was rejected as unsafe because it
# false-positively matched ordinary column names like "cost", "gross",
# "loss", "close", "boss_level", "system_id" (rejecting legitimate business
# queries) while still not actually being sound (substring matching is not a
# security boundary — it doesn't stop e.g. "@__builtins__").
#
# This implementation takes the report's other suggested direction: a
# whitelist of the table's ACTUAL columns, enforced structurally rather than
# textually. filter_expr is parsed once with Python's `ast` module (never
# eval()'d, never handed to df.query()) and only a small, closed set of node
# types is permitted:
#   - Compare      (==, !=, <, <=, >, >=, in, not in — chained comparisons
#                    like "0 < amount < 100" are rejected; write two clauses)
#   - BoolOp        (and / or)
#   - UnaryOp       (not)
#   - Name          (MUST be a real column of the table being queried — the
#                     whitelist; anything else, e.g. "os" or "__builtins__",
#                     is rejected because it isn't a column, not because its
#                     name looks suspicious)
#   - Constant      (str / int / float / bool / None)
#   - List / Tuple  (only of Constants — for the `in [...]` form)
# Every other node type — Call, Attribute, Subscript, Lambda, comprehensions,
# Starred, imports, f-strings with expressions, walrus, etc. — is rejected
# outright. There is no code execution path: the expression is evaluated by
# OUR small recursive walker directly against the DataFrame's columns, never
# passed to eval()/query()/exec(), so there is nothing for an attacker to
# smuggle a callable or attribute access into.
_ALLOWED_CMP_OPS = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a.isin(b) if hasattr(a, "isin") else (a in b),
    ast.NotIn: lambda a, b: ~a.isin(b) if hasattr(a, "isin") else (a not in b),
}


class _UnsafeFilterExpr(ValueError):
    """Raised when filter_expr contains a construct outside the safe subset."""


def _safe_filter_mask(df: pd.DataFrame, filter_expr: str) -> pd.Series:
    """Parse and evaluate `filter_expr` against `df` using only a whitelisted,
    structurally-verified subset of Python expression syntax. Raises
    _UnsafeFilterExpr (a ValueError) for anything outside that subset —
    including any identifier that is not one of df's actual column names."""
    try:
        tree = ast.parse(filter_expr, mode="eval")
    except SyntaxError as e:
        raise _UnsafeFilterExpr(f"filter_expr is not a valid expression: {e}")

    columns = set(df.columns)

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.BoolOp):
            values = [_eval(v) for v in node.values]
            result = values[0]
            op = (lambda a, b: a & b) if isinstance(node.op, ast.And) else (lambda a, b: a | b)
            for v in values[1:]:
                result = op(result, v)
            return result
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return ~_eval(node.operand)
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1 or len(node.comparators) != 1:
                raise _UnsafeFilterExpr(
                    "chained comparisons (e.g. '0 < amount < 100') are not supported — "
                    "write as two clauses joined with 'and'"
                )
            op_type = type(node.ops[0])
            if op_type not in _ALLOWED_CMP_OPS:
                raise _UnsafeFilterExpr(f"comparison operator {op_type.__name__!r} is not allowed")
            left = _eval(node.left)
            right = _eval(node.comparators[0])
            return _ALLOWED_CMP_OPS[op_type](left, right)
        if isinstance(node, ast.Name):
            if node.id not in columns:
                raise _UnsafeFilterExpr(
                    f"{node.id!r} is not a column of this table — filter_expr may only "
                    f"reference: {sorted(columns)}"
                )
            return df[node.id]
        if isinstance(node, ast.Constant):
            if node.value is not None and not isinstance(node.value, (str, int, float, bool)):
                raise _UnsafeFilterExpr(f"unsupported constant type {type(node.value).__name__!r}")
            return node.value
        if isinstance(node, (ast.List, ast.Tuple)):
            return [_eval(el) for el in node.elts]
        # Explicitly rejected: Call, Attribute, Subscript, Lambda, comprehensions,
        # Starred, Dict/Set, NamedExpr (walrus), and anything else not listed above.
        raise _UnsafeFilterExpr(
            f"filter_expr contains an unsupported construct ({type(node).__name__}) — "
            f"only column comparisons joined with and/or/not are allowed"
        )

    result = _eval(tree)
    if not isinstance(result, pd.Series):
        raise _UnsafeFilterExpr("filter_expr must evaluate to a per-row boolean condition")
    return result.astype(bool)


def _df(path: str, sheet: str = "") -> pd.DataFrame:
    full = os.path.normpath(os.path.join(_DATA_DIR, path))
    if not full.startswith(os.path.normpath(_DATA_DIR)):
        raise ValueError("Path escapes configured data_dir")
    low = full.lower()
    if low.endswith(".csv"):
        return pd.read_csv(full)
    if low.endswith(".xls"):
        # Legacy binary Excel needs the xlrd engine (Fix #6).
        return pd.read_excel(full, sheet_name=sheet or 0, engine="xlrd")
    return pd.read_excel(full, sheet_name=sheet or 0)


# ── Tool functions ───────────────────────────────────────────────────────────

def list_tables() -> List[str]:
    """List CSV/XLSX tabular sources under the configured data root."""
    out: List[str] = []
    for r, _, files in os.walk(_DATA_DIR):
        out += [
            os.path.relpath(os.path.join(r, f), _DATA_DIR)
            for f in files if f.lower().endswith((".csv", ".xlsx", ".xls"))
        ]
    return out


def describe_table(path: str, sheet: str = "") -> dict:
    """Schema + sample rows + numeric summary for a CSV/XLSX source."""
    df = _df(path, sheet)
    return {
        "columns": {c: str(t) for c, t in df.dtypes.items()},
        "rows":    len(df),
        "head":    df.head(5).to_dict("records"),
        "numeric_summary": df.describe(include="number").round(2).to_dict(),
    }


def query_table(path: str, filter_expr: str = "", group_by: str = "",
                aggregate: str = "", sheet: str = "", limit: int = 100) -> List[dict]:
    """Query a table: optional filter expression restricted to the table's own
    columns (e.g. "dept == 'HR' and amount > 100" — see SEC-F-002 / _safe_filter_mask
    for exactly what's allowed), optional group_by column(s) comma-separated
    with aggregate like 'sum' | 'mean' | 'count'."""
    df = _df(path, sheet)
    if filter_expr:
        # SEC-F-002: filter_expr comes from an LLM tool call — untrusted input.
        # df.query() parses/evaluates arbitrary expressions; _safe_filter_mask()
        # instead only permits column-vs-constant comparisons (validated against
        # this table's REAL columns, not a hardcoded/substring denylist) joined
        # by and/or/not, with no code-execution path at all. See its docstring.
        df = df[_safe_filter_mask(df, filter_expr)]
    if group_by:
        keys = [k.strip() for k in group_by.split(",")]
        df = getattr(df.groupby(keys), aggregate or "sum")(numeric_only=True).reset_index()
    return df.head(limit).round(3).to_dict("records")


def variance_report(path: str, budget_col: str, actual_col: str, label_col: str,
                    flag_pct: float = 5.0, sheet: str = "") -> List[dict]:
    """Compute budget-vs-actual variance per row and flag rows whose
    absolute variance %% exceeds flag_pct."""
    df = _df(path, sheet).dropna(subset=[budget_col, actual_col])
    out: List[dict] = []
    for _, row in df.iterrows():
        b, a = float(row[budget_col]), float(row[actual_col])
        pct = round((a - b) / b * 100, 1) if b else None
        out.append({
            "label":        row[label_col],
            "budget":       b,
            "actual":       a,
            "variance_pct": pct,
            "flagged":      abs(pct) >= flag_pct if pct is not None else True,
        })
    return out


def reconcile(left_path: str, right_path: str, amount_col_left: str,
              amount_col_right: str, ref_col_left: str, ref_col_right: str,
              tolerance: float = 1.0) -> dict:
    """Match two transaction tables on fuzzy reference + amount tolerance;
    report matches and discrepancies."""
    L, R = _df(left_path), _df(right_path)
    matches, unmatched_left, used = [], [], set()
    for _, l in L.iterrows():
        hit = None
        for j, r in R.iterrows():
            if j in used:
                continue
            ref_ok = (
                str(l[ref_col_left]).split("/")[-1] in str(r[ref_col_right])
                or str(r[ref_col_right]).split("/")[-1] in str(l[ref_col_left])
            )
            amt_ok = abs(float(l[amount_col_left]) - float(r[amount_col_right])) <= tolerance
            if ref_ok and amt_ok:
                hit = j
                break
        if hit is not None:
            used.add(hit)
            matches.append({"left_ref": l[ref_col_left], "right_ref": R.loc[hit, ref_col_right]})
        else:
            near = [
                {
                    "right_ref":   r[ref_col_right],
                    "amount_diff": round(abs(float(l[amount_col_left]) - float(r[amount_col_right])), 2),
                }
                for j, r in R.iterrows()
                if j not in used
                and (str(l[ref_col_left]).split("/")[-1] in str(r[ref_col_right]))
            ]
            unmatched_left.append({
                "ref":         l[ref_col_left],
                "amount":      float(l[amount_col_left]),
                "near_misses": near,
            })
    unmatched_right = [
        {"ref": r[ref_col_right], "amount": float(r[amount_col_right])}
        for j, r in R.iterrows() if j not in used
    ]
    return {
        "matched":         len(matches),
        "unmatched_left":  unmatched_left,
        "unmatched_right": unmatched_right,
    }


def make_chart(path: str, chart: str, x: str, y: str, series: str = "",
               title: str = "", sheet: str = "") -> dict:
    """Render a chart (line | bar | scatter) from a table to PNG in the
    charts outbox; returns the file path."""
    df = _df(path, sheet)
    os.makedirs(_CHARTS_DIR, exist_ok=True)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    groups = df.groupby(series) if series else [("", df)]
    for name, g in groups:
        if chart == "line":
            ax.plot(g[x], g[y], marker="o", label=str(name))
        elif chart == "bar":
            ax.bar(g[x].astype(str), g[y], label=str(name))
        else:
            ax.scatter(g[x], g[y], label=str(name))
    if series:
        ax.legend()
    ax.set_xlabel(x)
    ax.set_ylabel(y)
    ax.set_title(title or f"{y} by {x}")
    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()
    out = os.path.join(_CHARTS_DIR, f"{chart}_{y}_by_{x}.png".replace(" ", "_"))
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return {"chart_file": out}
