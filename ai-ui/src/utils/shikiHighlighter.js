// SPDX-License-Identifier: MIT
import { createHighlighter } from "shiki";

let highlighter;

export async function getShikiHighlighter() {

  if (!highlighter) {

    highlighter = await createHighlighter({
      themes: ["github-light"],
      langs: [
        "javascript",
        "typescript",
        "python",
        "java",
        "json",
        "bash",
        "text"
      ]
    });

  }

  return highlighter;
}