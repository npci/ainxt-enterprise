# SPDX-License-Identifier: Apache-2.0
# ============================================================
# MEETING TRANSCRIPT — WebVTT parsing + speaker attribution + MoM prompt
#
# Scope doc §4.2 / §5: Teams transcripts are fetched POST-meeting from
# Microsoft Graph as WebVTT (text/vtt). Speaker attribution comes from the
# VTT `<v Speaker Name>` voice tags (Graph transcript metadata) — NOT from
# any third-party STT. This module is pure (no I/O): it parses VTT, attributes
# speakers, flattens to text, and builds the MoM/summary prompt that
# model_router runs INSIDE AiNxt.
# ============================================================

import re
from typing import Optional

# 00:00:01.234 --> 00:00:05.678  (optional trailing cue settings)
_TS_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})"
)
# <v Speaker Name>spoken text</v>  (Teams voice span; closing tag optional)
_VOICE_RE = re.compile(r"<v\s+([^>]+)>(.*?)(?:</v>|$)", re.DOTALL)
# Any leftover HTML-ish tags to strip from spoken text
_TAG_RE = re.compile(r"<[^>]+>")


def parse_vtt(vtt_text: str) -> list[dict]:
    """Parse WebVTT into ordered cues.

    Returns a list of {start, end, speaker, text}. `speaker` is None when the
    cue carries no <v> voice tag. Robust to cue identifiers, NOTE blocks, and
    multi-line cue text.
    """
    if not vtt_text:
        return []

    # Normalise newlines, drop a leading BOM, split into blank-line-separated blocks.
    text = vtt_text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    blocks = re.split(r"\n\s*\n", text)

    cues: list[dict] = []
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if not lines:
            continue
        # Skip the WEBVTT header block and NOTE/STYLE/REGION metadata blocks.
        if lines[0].strip().upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue

        # Find the timestamp line; anything before it (cue id) is ignored.
        ts_idx = next((i for i, ln in enumerate(lines) if _TS_RE.search(ln)), None)
        if ts_idx is None:
            continue
        m = _TS_RE.search(lines[ts_idx])
        start, end = m.group(1), m.group(2)
        payload = "\n".join(lines[ts_idx + 1:]).strip()
        if not payload:
            continue

        speaker, spoken = _split_speaker(payload)
        spoken = _TAG_RE.sub("", spoken).strip()
        if not spoken:
            continue
        cues.append({"start": start, "end": end, "speaker": speaker, "text": spoken})

    return cues


def _split_speaker(payload: str) -> tuple[Optional[str], str]:
    """Extract (speaker, text) from a cue payload using the <v> voice tag."""
    vm = _VOICE_RE.search(payload)
    if vm:
        return vm.group(1).strip(), vm.group(2).strip()
    return None, payload


def attribute_speakers(cues: list[dict], participants: Optional[list[str]] = None) -> list[dict]:
    """Normalise/fill speaker names, optionally reconciling against a participant list.

    • Carries forward the last known speaker for cues missing a voice tag
      (Teams sometimes omits <v> on continuation cues).
    • If a participant display-name list is given, snaps near-matches (case/space
      insensitive, substring) to the canonical participant name.
    """
    canon = {}
    if participants:
        for p in participants:
            if p:
                canon[_norm(p)] = p

    last_speaker = "Unknown"
    out = []
    for cue in cues:
        sp = cue.get("speaker")
        if sp:
            key = _norm(sp)
            sp = canon.get(key) or _best_participant_match(key, canon) or sp
            last_speaker = sp
        else:
            sp = last_speaker
        c = dict(cue)
        c["speaker"] = sp
        out.append(c)
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _best_participant_match(key: str, canon: dict) -> Optional[str]:
    """Substring match a speaker key against canonical participant names."""
    for ckey, cval in canon.items():
        if key and (key in ckey or ckey in key):
            return cval
    return None


def build_transcript_text(cues: list[dict], max_chars: int = 0) -> str:
    """Flatten cues to 'Speaker: text' lines for the summariser.

    max_chars > 0 truncates from the FRONT-kept side (keeps the head of the
    meeting) and appends a truncation marker.
    """
    lines = [f"{c['speaker']}: {c['text']}" for c in cues]
    text = "\n".join(lines)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n…[transcript truncated]"
    return text


def summarize_participation(cues: list[dict]) -> dict:
    """Per-speaker {turns, words} stats from attributed cues."""
    stats: dict[str, dict] = {}
    for c in cues:
        sp = c.get("speaker") or "Unknown"
        s = stats.setdefault(sp, {"turns": 0, "words": 0})
        s["turns"] += 1
        s["words"] += len(c.get("text", "").split())
    return stats


def build_mom_prompt(subject: str, transcript_text: str, participants: Optional[list[str]] = None) -> str:
    """Build the Minutes-of-Meeting / summary prompt run INSIDE AiNxt (model_router).

    No provider-specific phrasing — model-agnostic per the platform's
    multi-model policy.
    """
    who = ", ".join(p for p in (participants or []) if p) or "the attendees"
    return (
        "You are AiNxt, AiNxt's in-house meeting assistant. Produce concise, accurate "
        "Minutes of Meeting from the transcript below. Do not invent content; attribute "
        "decisions and action items to the named speakers only where the transcript "
        "supports it.\n\n"
        f"Meeting subject: {subject or '(untitled)'}\n"
        f"Participants: {who}\n\n"
        "Return Markdown with exactly these sections:\n"
        "## Summary\n"
        "## Key Discussion Points\n"
        "## Decisions\n"
        "## Action Items\n"
        "(For each action item use: - [ ] <owner> — <action> (<due if mentioned>))\n"
        "## Risks / Open Questions\n\n"
        "Transcript:\n"
        f"{transcript_text}"
    )
