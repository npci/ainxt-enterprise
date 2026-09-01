# SPDX-License-Identifier: Apache-2.0
"""Materialize prior DSLAR agent output into a clean JSON file."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any


FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def _try_load_json(text: str) -> Any:
    return json.loads(text)


def _extract_balanced_json(text: str) -> Any:
    starts = [idx for idx, char in enumerate(text) if char in "{["]
    pairs = {"{": "}", "[": "]"}
    closers = set(pairs.values())
    for start in starts:
        stack: list[str] = []
        in_string = False
        escape = False
        for idx in range(start, len(text)):
            char = text[idx]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char in pairs:
                stack.append(pairs[char])
            elif char in closers:
                if not stack or char != stack[-1]:
                    break
                stack.pop()
                if not stack:
                    candidate = text[start:idx + 1]
                    try:
                        return _try_load_json(candidate)
                    except Exception:
                        break
    raise ValueError("Could not find a valid JSON object or array in the supplied text.")


def parse_raw_agent_output(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        raise ValueError("No raw agent output supplied.")

    try:
        return _try_load_json(stripped)
    except Exception:
        pass

    for match in FENCED_JSON_RE.finditer(stripped):
        candidate = match.group(1).strip()
        if not candidate:
            continue
        try:
            return _try_load_json(candidate)
        except Exception:
            continue

    return _extract_balanced_json(stripped)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert raw previous-agent output into clean JSON.")
    parser.add_argument("--raw-input", help="Path to raw previous-agent output. If omitted, stdin is used.")
    parser.add_argument("--output-json", default="extracted.json", help="Path to write clean JSON.")
    args = parser.parse_args()

    if args.raw_input:
        with open(args.raw_input, "r", encoding="utf-8") as fh:
            raw = fh.read()
    else:
        raw = sys.stdin.read()

    payload = parse_raw_agent_output(raw)
    with open(args.output_json, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    print(args.output_json)


if __name__ == "__main__":
    main()
