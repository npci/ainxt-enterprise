// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.
// lib/yaml.js — Minimal YAML parser (subset).
//
// Supports the subset used by the test files in the spec:
//   - block mappings ("key: value")
//   - block sequences ("- item")
//   - flow mappings ("{ k: v, k: v }")
//   - flow sequences ("[a, b, c]")
//   - quoted strings, numbers, booleans, null
//   - nested structures via 2-space indentation
//   - "#" comments
//
// Not a full YAML 1.2 implementation. If a user pastes something this
// parser can't handle, they can use the JSON form instead.

export function parseYaml(text) {
  const lines = preprocess(text);
  const root = {};
  const stack = [{ indent: -1, container: root, type: "map" }];

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const indent = raw.match(/^( *)/)[1].length;
    const line = raw.slice(indent);
    if (!line.replace(/^\s+|\s+$/g, "")) continue;

    // pop deeper frames
    while (stack.length > 1 && indent <= stack[stack.length - 1].indent) {
      stack.pop();
    }
    const top = stack[stack.length - 1];

    if (line.startsWith("- ")) {
      // list item
      const after = line.slice(2);
      if (top.type !== "list") {
        // promote into a list (when key: was followed by - on next line)
        // top should have last key still pending; fall through to error
        throw new Error("Unexpected '-' at line " + (i + 1));
      }

      // "- key: value" — start a map item
      if (/^[^\s].*?:(?:\s|$)/.test(after) && !after.startsWith("{")) {
        const obj = {};
        top.container.push(obj);
        stack.push({ indent, container: obj, type: "map" });
        // re-process the rest of the line as "key: value"
        const rest = after;
        consumeKeyValue(rest, obj, indent, stack, lines, i);
      } else if (after.startsWith("{")) {
        top.container.push(parseFlow(after));
      } else if (after.startsWith("[")) {
        top.container.push(parseFlow(after));
      } else {
        top.container.push(parseScalar(after.trim()));
      }
      continue;
    }

    // map entry: "key:" or "key: value"
    if (top.type !== "map") {
      throw new Error("Expected list item at line " + (i + 1));
    }
    consumeKeyValue(line, top.container, indent, stack, lines, i);
  }

  return root;
}

function consumeKeyValue(line, mapObj, indent, stack, lines, idx) {
  const m = line.match(/^([^:]+):\s?(.*)$/);
  if (!m) throw new Error("Bad mapping at line " + (idx + 1) + ": " + line);
  const key = m[1].trim();
  const rest = m[2];

  if (rest === "" || rest === undefined) {
    // Value on subsequent indented lines — peek to decide list vs map.
    // Frame indent is set to the *parent's* indent so the unified pop
    // rule (`current_indent <= top.indent`) keeps children alive while
    // popping siblings at the parent's indent.
    const next = peekNext(lines, idx);
    if (next && next.text.startsWith("- ")) {
      const arr = [];
      mapObj[key] = arr;
      stack.push({ indent, container: arr, type: "list" });
    } else {
      const obj = {};
      mapObj[key] = obj;
      stack.push({ indent, container: obj, type: "map" });
    }
    return;
  }

  if (rest.startsWith("{") || rest.startsWith("[")) {
    mapObj[key] = parseFlow(rest);
    return;
  }

  mapObj[key] = parseScalar(rest);
}

function peekNext(lines, idx) {
  for (let j = idx + 1; j < lines.length; j++) {
    const raw = lines[j];
    if (!raw.replace(/^\s+|\s+$/g, "")) continue;
    const indent = raw.match(/^( *)/)[1].length;
    return { indent, text: raw.slice(indent) };
  }
  return null;
}

function parseFlow(s) {
  // Convert YAML flow style to JSON. We allow unquoted identifiers as
  // keys/values and ${...} variable refs.
  // Heuristic: replace bare keys (k:) with "k": and bare scalars with strings
  // when not already quoted, numeric, boolean, or null.
  const json = yamlFlowToJson(s);
  return JSON.parse(json);
}

function yamlFlowToJson(s) {
  // tokenize and re-emit as JSON
  let out = "";
  let i = 0;
  const n = s.length;
  let context = []; // stack of '{' or '['
  while (i < n) {
    const c = s[i];

    if (c === "{" || c === "[") {
      out += c;
      context.push(c);
      i++;
      continue;
    }
    if (c === "}" || c === "]") {
      out += c;
      context.pop();
      i++;
      continue;
    }
    if (c === "," || c === ":") {
      out += c;
      i++;
      continue;
    }
    if (/\s/.test(c)) {
      out += c;
      i++;
      continue;
    }

    // scalar / key token until next structural char
    let j = i;
    if (c === '"' || c === "'") {
      const quote = c;
      j++;
      while (j < n) {
        if (s[j] === "\\") {
          j += 2;
          continue;
        }
        if (s[j] === quote) {
          j++;
          break;
        }
        j++;
      }
      const tokenRaw = s.slice(i, j);
      // normalize single-quoted to double-quoted JSON string
      if (quote === "'") {
        const inner = tokenRaw
          .slice(1, -1)
          .replace(/\\/g, "\\\\")
          .replace(/"/g, '\\"');
        out += '"' + inner + '"';
      } else {
        out += tokenRaw;
      }
      i = j;
      continue;
    }

    while (j < n && !"{}[],:".includes(s[j])) j++;
    const token = s.slice(i, j).trim();
    out += quoteIfNeeded(token);
    i = j;
  }
  return out;
}

function quoteIfNeeded(token) {
  if (token === "") return "";
  if (token === "true" || token === "false" || token === "null") return token;
  if (/^-?\d+(\.\d+)?$/.test(token)) return token;
  // already quoted
  if (
    (token.startsWith('"') && token.endsWith('"')) ||
    (token.startsWith("'") && token.endsWith("'"))
  ) {
    if (token.startsWith("'")) {
      const inner = token
        .slice(1, -1)
        .replace(/\\/g, "\\\\")
        .replace(/"/g, '\\"');
      return '"' + inner + '"';
    }
    return token;
  }
  // bare identifier / scalar — quote it
  return '"' + token.replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
}

function parseScalar(s) {
  s = s.trim();
  if (s === "") return "";
  if (s === "null" || s === "~") return null;
  if (s === "true") return true;
  if (s === "false") return false;
  if (/^-?\d+$/.test(s)) return parseInt(s, 10);
  if (/^-?\d+\.\d+$/.test(s)) return parseFloat(s);
  if (s.startsWith('"') && s.endsWith('"')) {
    // Use JSON.parse so JSON-style escapes (\", \\, \n) decode correctly.
    // The recorder emits selectors like "[data-testid=\"x\"]" and depends on this.
    try { return JSON.parse(s); } catch { return s.slice(1, -1); }
  }
  if (s.startsWith("'") && s.endsWith("'")) {
    return s.slice(1, -1);
  }
  return s;
}

function preprocess(text) {
  // strip comments and trailing whitespace; keep blank lines
  return text.split(/\r?\n/).map((line) => {
    // very simple comment strip: " # ..." or "# ..." at line start when not in quotes
    let out = "";
    let inQuote = null;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (inQuote) {
        out += c;
        if (c === "\\" && i + 1 < line.length) {
          out += line[++i];
          continue;
        }
        if (c === inQuote) inQuote = null;
        continue;
      }
      if (c === '"' || c === "'") {
        inQuote = c;
        out += c;
        continue;
      }
      if (c === "#") break;
      out += c;
    }
    return out.replace(/\s+$/, "");
  });
}
