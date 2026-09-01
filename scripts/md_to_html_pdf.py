# SPDX-License-Identifier: Apache-2.0
"""
Convert a Markdown file to a print-ready HTML document.

Open the resulting .html in a browser and use "Print > Save as PDF"
(or Ctrl+P) to produce a PDF. The CSS is tuned for paged output:
A4, sensible margins, monospace code blocks, page-breaks before
top-level headings, and table styling that matches the deployment
guide's tone.

Usage:
    python scripts/md_to_html_pdf.py <input.md> [<output.html>]
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path


# ── Inline conversion helpers ───────────────────────────────────────────────


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _convert_inline(text: str) -> str:
    """Convert inline markdown: code spans, bold, italics, links."""
    # Protect inline code spans first by stashing them.
    code_spans: list[str] = []

    def _stash_code(match: re.Match[str]) -> str:
        code_spans.append(match.group(1))
        return f"\x00CODE{len(code_spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", _stash_code, text)

    # Escape HTML, then apply formatting.
    text = _escape(text)

    # Links: [label](url)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
        text,
    )
    # Bold: **text**
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    # Italic: *text* (avoid matching list bullets — already stripped by caller).
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)

    # Restore code spans (escaped).
    def _restore_code(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        return f"<code>{_escape(code_spans[idx])}</code>"

    text = re.sub(r"\x00CODE(\d+)\x00", _restore_code, text)
    return text


# ── Block conversion ────────────────────────────────────────────────────────


def _slugify(text: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"\s+", "-", slug).strip("-")
    return slug


def _render_table(rows: list[str]) -> str:
    """Render a GFM pipe table (header row, separator, body rows)."""
    def _split(row: str) -> list[str]:
        row = row.strip()
        if row.startswith("|"):
            row = row[1:]
        if row.endswith("|"):
            row = row[:-1]
        return [cell.strip() for cell in row.split("|")]

    if len(rows) < 2:
        return ""

    headers = _split(rows[0])
    body = [_split(r) for r in rows[2:]]

    out = ["<table>", "<thead><tr>"]
    out.extend(f"<th>{_convert_inline(h)}</th>" for h in headers)
    out.append("</tr></thead>")
    out.append("<tbody>")
    for row in body:
        out.append("<tr>")
        out.extend(f"<td>{_convert_inline(c)}</td>" for c in row)
        out.append("</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def convert(md: str) -> str:
    """Convert markdown body text to an HTML body fragment."""
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)

    # Each entry: (tag, indent_spaces, li_open). Lists nest when indent
    # grows; they close when indent shrinks below their level. A list at
    # the same indent but different tag (ul vs ol) closes and replaces.
    # `li_open` tracks whether the current frame has an unclosed <li>
    # waiting for either a sibling <li> or a nested list inside it.
    list_stack: list[list] = []  # [tag, indent, li_open]

    def close_top() -> None:
        frame = list_stack.pop()
        tag, _indent, li_open = frame
        if li_open:
            out.append("</li>")
        out.append(f"</{tag}>")
        # The closed list belonged inside the parent's open <li>, so we
        # leave the parent's li_open flag alone — the parent will emit
        # its </li> when it sees the next sibling or itself closes.

    def close_lists_to(indent: int | None = None, tag: str | None = None) -> None:
        """Close lists deeper than `indent`. If `tag` given, also close
        the top frame when its indent matches but its tag differs."""
        while list_stack:
            top_tag, top_indent, _ = list_stack[-1]
            if indent is None:
                close_top()
                continue
            if top_indent > indent:
                close_top()
            elif top_indent == indent and tag is not None and top_tag != tag:
                close_top()
            else:
                break

    def close_lists() -> None:
        while list_stack:
            close_top()

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Fenced code block
        fence = re.match(r"^```(\w*)\s*$", stripped)
        if fence:
            close_lists()
            lang = fence.group(1) or ""
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            body_text = "\n".join(buf)
            # Mermaid fences render as real diagrams (mermaid.js in template).
            if lang.lower() == "mermaid":
                out.append(f'<div class="mermaid">\n{_escape(body_text)}\n</div>')
                continue
            code = _escape(body_text)
            cls = f' class="lang-{lang}"' if lang else ""
            out.append(f'<pre><code{cls}>{code}</code></pre>')
            continue

        # Horizontal rule
        if re.match(r"^-{3,}\s*$", stripped):
            close_lists()
            out.append("<hr />")
            i += 1
            continue

        # Fenced callout / banner:  :::banner  ...  :::   (or :::note, :::verdict)
        cfence = re.match(r"^:::\s*(\w+)\s*$", stripped)
        if cfence:
            close_lists()
            kind = cfence.group(1).lower()
            i += 1
            cbuf: list[str] = []
            while i < n and lines[i].strip() != ":::":
                cbuf.append(lines[i])
                i += 1
            i += 1  # skip closing :::
            # Extract "@chips: a | b | c" lines → styled pill row.
            chip_html = ""
            keep: list[str] = []
            for cl in cbuf:
                m = re.match(r"^\s*@chips:\s*(.+)$", cl)
                if m:
                    chips = [c.strip() for c in m.group(1).split("|") if c.strip()]
                    spans = "".join(
                        f'<span class="chip">{_convert_inline(c)}</span>' for c in chips
                    )
                    chip_html += f'<div class="chip-row">{spans}</div>'
                else:
                    keep.append(cl)
            inner_html = convert("\n".join(keep))
            # Insert chips just before the trailing <em> attribution if present.
            if chip_html and "<p><em>" in inner_html:
                inner_html = inner_html.replace("<p><em>", chip_html + "<p><em>", 1)
            else:
                inner_html += chip_html
            out.append(f'<div class="callout callout-{kind}">{inner_html}</div>')
            continue

        # Heading
        heading = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", stripped)
        if heading:
            close_lists()
            level = len(heading.group(1))
            text = heading.group(2)
            slug = _slugify(text)
            inner = _convert_inline(text)
            cls = ' class="page-break"' if level == 2 else ""
            out.append(f'<h{level} id="{slug}"{cls}>{inner}</h{level}>')
            i += 1
            continue

        # Blockquote
        if stripped.startswith(">"):
            close_lists()
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            inner = _convert_inline(" ".join(buf))
            out.append(f"<blockquote><p>{inner}</p></blockquote>")
            continue

        # Table (must have header + separator like |---|---|)
        if "|" in stripped and i + 1 < n and re.match(r"^\|?\s*:?-{3,}", lines[i + 1].strip()):
            close_lists()
            tbl: list[str] = []
            while i < n and "|" in lines[i]:
                tbl.append(lines[i])
                i += 1
            out.append(_render_table(tbl))
            continue

        # Unordered / ordered list (with indent-based nesting)
        ul = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        ol = re.match(r"^(\s*)(\d+)\.\s+(.*)$", line)
        if ul or ol:
            tag = "ul" if ul else "ol"
            indent_str = (ul or ol).group(1)
            indent = len(indent_str.expandtabs(4))
            content = ul.group(2) if ul else ol.group(3)

            # Pop deeper lists; if same depth but different tag, swap.
            close_lists_to(indent=indent, tag=tag)

            need_open = not list_stack or list_stack[-1][1] < indent
            if need_open:
                # Nested list goes inside the parent's open <li>; do NOT
                # close that <li> first. Just push a new frame.
                out.append(f"<{tag}>")
                list_stack.append([tag, indent, False])
            else:
                # Same level — close the previous sibling's <li> first.
                if list_stack[-1][2]:
                    out.append("</li>")
                    list_stack[-1][2] = False

            out.append(f"<li>{_convert_inline(content)}")
            list_stack[-1][2] = True
            i += 1
            continue

        # Blank line — paragraph break
        if stripped == "":
            close_lists()
            i += 1
            continue

        # Paragraph (gather consecutive non-blank lines)
        close_lists()
        buf = [stripped]
        i += 1
        while i < n and lines[i].strip() and not re.match(
            r"^(#|```|>|-{3,}|\s*[-*+]\s|\s*\d+\.\s)", lines[i]
        ):
            buf.append(lines[i].strip())
            i += 1
        para = " ".join(buf)
        out.append(f"<p>{_convert_inline(para)}</p>")

    close_lists()
    return "\n".join(out)


# ── HTML shell with print-ready CSS ─────────────────────────────────────────


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>{title}</title>
<style>
  @page {{
    size: A4;
    margin: 16mm 15mm;
  }}
  :root {{
    --fg: #1f2933;
    --muted: #6b7784;
    --accent: #0e7490;
    --accent-2: #0891b2;
    --accent-soft: #e0f2f6;
    --rule: #e2e8ee;
    --card: #ffffff;
    --bg: #f6f9fb;
    --code-bg: #f1f5f9;
    --code-fg: #0f3b46;
    --table-head: #eef6f9;
    --table-alt: #fafcfd;
    --link: #0e7490;
    --ok: #15803d;
    --warn: #b45309;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    color: var(--fg);
    background: var(--bg);
    font-family: -apple-system, "Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.62;
    margin: 0;
    padding: 0;
    -webkit-font-smoothing: antialiased;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
  }}
  /* Content is a centered reading column that also works on mobile. */
  .cover, .content {{
    max-width: 820px;
    margin: 0 auto;
    padding: 0 20px;
  }}
  .content {{ padding-bottom: 48px; }}
  h1, h2, h3, h4, h5, h6 {{
    font-family: -apple-system, "Segoe UI", "Inter", Arial, sans-serif;
    color: var(--fg);
    line-height: 1.25;
    letter-spacing: -0.01em;
    margin: 1.5em 0 0.5em;
    font-weight: 700;
  }}
  h1 {{ font-size: 24pt; color: var(--accent); }}
  h2 {{
    font-size: 16pt;
    margin-top: 1.9em;
    padding-bottom: 6px;
    border-bottom: 2px solid var(--accent-soft);
  }}
  h3 {{ font-size: 13pt; color: var(--accent); }}
  h4 {{ font-size: 11.5pt; color: var(--muted); text-transform: none; }}
  h2.page-break {{ page-break-before: always; }}
  h1 + h2.page-break:first-of-type {{ page-break-before: avoid; }}
  p {{ margin: 0.65em 0; }}
  a {{ color: var(--link); text-decoration: none; border-bottom: 1px solid var(--accent-soft); }}
  a:hover {{ border-bottom-color: var(--accent); }}
  ul, ol {{ margin: 0.5em 0 0.9em 1.5em; padding: 0; }}
  li {{ margin: 0.28em 0; }}
  strong {{ color: #14323b; }}
  code {{
    font-family: "SF Mono", "Consolas", "Cascadia Mono", "Menlo", monospace;
    font-size: 9.5pt;
    background: var(--code-bg);
    color: var(--code-fg);
    padding: 1.5px 5px;
    border-radius: 5px;
  }}
  pre {{
    background: #0f2b33;
    color: #dbeef2;
    border: none;
    border-radius: 10px;
    padding: 14px 16px;
    overflow-x: auto;
    page-break-inside: avoid;
    font-size: 9pt;
    line-height: 1.5;
    box-shadow: 0 1px 3px rgba(15,43,51,0.12);
  }}
  pre code {{ background: transparent; color: inherit; padding: 0; font-size: inherit; }}
  blockquote {{
    border-left: 4px solid var(--accent-2);
    margin: 1em 0;
    padding: 0.6em 1.1em;
    color: #37474f;
    background: var(--accent-soft);
    border-radius: 0 10px 10px 0;
  }}
  blockquote p {{ margin: 0.3em 0; }}
  /* Card-style tables. */
  table {{
    border-collapse: separate;
    border-spacing: 0;
    margin: 1.1em 0;
    width: 100%;
    page-break-inside: avoid;
    font-size: 9.8pt;
    background: var(--card);
    border: 1px solid var(--rule);
    border-radius: 10px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(31,41,51,0.05);
  }}
  th, td {{
    padding: 8px 11px;
    text-align: left;
    vertical-align: top;
    border-bottom: 1px solid var(--rule);
  }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr:nth-child(even) {{ background: var(--table-alt); }}
  th {{
    background: var(--table-head);
    font-weight: 700;
    color: #0d5563;
    border-bottom: 2px solid var(--accent-soft);
  }}
  hr {{ border: 0; border-top: 1px solid var(--rule); margin: 2em 0; }}
  /* Cover. */
  .cover {{
    text-align: center;
    padding-top: 46mm;
    page-break-after: always;
  }}
  .cover .badge {{
    display: inline-block;
    font-size: 9.5pt;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--accent);
    background: var(--accent-soft);
    padding: 6px 16px;
    border-radius: 30px;
    margin-bottom: 22px;
  }}
  .cover h1 {{
    border: none;
    font-size: 32pt;
    margin: 0 0 14px;
    letter-spacing: -0.02em;
  }}
  .cover .subtitle {{
    color: var(--muted);
    font-size: 13pt;
    max-width: 620px;
    margin: 0 auto;
    line-height: 1.5;
  }}
  .cover .meta {{
    margin-top: 42mm;
    color: var(--muted);
    font-size: 10pt;
  }}
  .cover .rule {{
    width: 60px; height: 4px; border-radius: 4px;
    background: var(--accent); margin: 26px auto 0;
  }}
  /* Mermaid diagrams — framed card, centered. */
  .mermaid {{
    background: var(--card);
    border: 1px solid var(--rule);
    border-radius: 12px;
    text-align: center;
    margin: 1.4em 0;
    padding: 18px 12px;
    page-break-inside: avoid;
    box-shadow: 0 1px 4px rgba(31,41,51,0.06);
    font-family: "Segoe UI", "Inter", Arial, sans-serif;
  }}
  .mermaid svg {{ max-width: 100%; height: auto; }}
  /* Callouts. */
  .callout {{
    border-radius: 12px;
    padding: 16px 20px;
    margin: 1.4em 0;
    page-break-inside: avoid;
  }}
  .callout p:first-child {{ margin-top: 0; }}
  .callout p:last-child {{ margin-bottom: 0; }}
  .callout-note {{
    background: var(--accent-soft);
    border-left: 4px solid var(--accent);
    color: #14323b;
  }}
  .callout-verdict {{
    background: #fef8ec;
    border-left: 4px solid #e0a400;
    color: #5a4300;
  }}
  /* Hero banner. */
  .callout-banner {{
    background: linear-gradient(135deg, #0e7490 0%, #0b4a5c 55%, #0f3b46 100%);
    color: #ffffff;
    text-align: center;
    padding: 34px 30px;
    border: none;
    border-radius: 16px;
    box-shadow: 0 10px 30px rgba(14,116,144,0.28);
  }}
  .callout-banner strong {{
    display: block;
    font-size: 18pt;
    font-weight: 800;
    line-height: 1.32;
    letter-spacing: -0.01em;
    margin: 0 auto 10px;
    max-width: 640px;
    color: #ffffff;
  }}
  .callout-banner p {{ color: #d6eef4; max-width: 640px; margin: 8px auto; font-size: 10.5pt; }}
  .callout-banner .chip-row {{
    display: flex; flex-wrap: wrap; gap: 8px;
    justify-content: center; margin: 18px auto 6px; max-width: 680px;
  }}
  .callout-banner .chip {{
    display: inline-block;
    background: rgba(255,255,255,0.14);
    border: 1px solid rgba(255,255,255,0.28);
    color: #ffffff;
    font-size: 9pt;
    font-weight: 600;
    padding: 6px 13px;
    border-radius: 30px;
    white-space: nowrap;
  }}
  .callout-banner em {{
    display: inline-block;
    margin-top: 16px;
    font-style: normal;
    font-size: 9pt;
    color: #b9dfe8;
    letter-spacing: 0.02em;
  }}
  /* Mobile / narrow screens. */
  @media screen and (max-width: 640px) {{
    html, body {{ font-size: 10.5pt; }}
    .cover, .content {{ padding: 0 14px; }}
    .cover {{ padding-top: 18mm; }}
    .cover h1 {{ font-size: 24pt; }}
    h1 {{ font-size: 20pt; }}
    h2 {{ font-size: 14pt; }}
    table {{ font-size: 9pt; display: block; overflow-x: auto; white-space: nowrap; }}
    .callout-banner strong {{ font-size: 15pt; }}
    .callout-banner {{ padding: 24px 18px; }}
  }}
  @media print {{
    html, body {{ background: #ffffff; }}
  }}
</style>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script>
  window.addEventListener('load', function () {{
    if (window.mermaid) {{
      mermaid.initialize({{
        startOnLoad: true,
        theme: 'base',
        themeVariables: {{
          fontFamily: 'Segoe UI, Inter, Arial, sans-serif',
          primaryColor: '#e0f2f6',
          primaryBorderColor: '#0e7490',
          primaryTextColor: '#14323b',
          lineColor: '#0891b2',
          clusterBkg: '#ffffff',
          clusterBorder: '#cbd9e0'
        }}
      }});
    }}
  }});
</script>
</head>
<body>
<div class="cover">
  <div class="badge">AiNxt Platform &middot; White Paper</div>
  <h1>{title}</h1>
  <div class="subtitle">{subtitle}</div>
  <div class="rule"></div>
  <div class="meta">AiNxt Platform &middot; Generated {date}</div>
</div>
<div class="content">
{body}
</div>
</body>
</html>
"""


def render_html(md_text: str, title: str, subtitle: str, date: str) -> str:
    body = convert(md_text)
    return HTML_TEMPLATE.format(
        title=_escape(title),
        subtitle=_escape(subtitle),
        date=_escape(date),
        body=body,
    )


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    src = Path(sys.argv[1])
    if not src.exists():
        print(f"error: {src} not found", file=sys.stderr)
        return 1

    dst = Path(sys.argv[2]) if len(sys.argv) >= 3 else src.with_suffix(".html")

    md_text = src.read_text(encoding="utf-8")

    # Pull the H1 line for the cover; fall back to file stem.
    h1_match = re.search(r"^#\s+(.+?)\s*$", md_text, flags=re.MULTILINE)
    title = h1_match.group(1) if h1_match else src.stem

    # Pull the first blockquote line as subtitle, if any.
    bq_match = re.search(r"^>\s*(.+?)\s*$", md_text, flags=re.MULTILINE)
    subtitle = bq_match.group(1) if bq_match else ""

    import datetime
    date = datetime.date.today().isoformat()

    html_out = render_html(md_text, title=title, subtitle=subtitle, date=date)
    dst.write_text(html_out, encoding="utf-8")
    print(f"wrote {dst}")
    print("Open in a browser and use Ctrl+P > Save as PDF.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
