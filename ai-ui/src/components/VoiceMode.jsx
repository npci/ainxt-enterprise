// SPDX-License-Identifier: Apache-2.0
/**
 * VoiceMode — ChatGPT-style conversational voice overlay
 *
 * Streaming text + sentence-by-sentence TTS pre-fetching:
 *   1. As LLM tokens stream in, text is displayed progressively
 *   2. Each completed sentence is immediately sent to TTS API (pre-fetch)
 *   3. After LLM finishes, play pre-fetched audio blobs sequentially
 *   → Sentence 1 audio is ready before streaming even completes
 *
 * Props:
 *   onClose(void)
 *   onSendVoice(text, mode, onToken) → Promise<string>
 *   micLang: string
 *   ttsApi(text) → Promise<Blob>
 */

import { useEffect, useRef, useState, useCallback } from "react";
import { Mic, Volume2, Loader2, X, MicOff, Globe, Building2 } from "lucide-react";

const SILENCE_MS = 1800;

// ── STT correction dictionary ─────────────────────────────────────────────
// Web Speech API mishears technical platform terms. Correct before sending
// to the backend so the LLM gets accurate domain vocabulary.
// Keys are lowercase STT output patterns; values are the correct term.
// Order matters: longer/more-specific phrases first.
const STT_CORRECTIONS = [
  // SDLC variants (most common misread)
  [/\bh\s*d\s*f\s*c\b/gi,                        "SDLC"],
  [/\bh d f c\b/gi,                               "SDLC"],
  [/\bsoftware development (life ?cycle|lifecycle)\b/gi, "SDLC pipeline"],

  // Human in the loop variants
  [/\bhuman in (the )?control\b/gi,               "human in the loop"],
  [/\bhr integration\b/gi,                         "human in the loop"],
  [/\bhuman oversight (and )?control\b/gi,         "human in the loop"],

  // RAG / retrieval
  [/\b(rack|rag retrieval augmented)\b/gi,         "RAG"],
  [/\bretrieval (augmented|arguments) generation\b/gi, "RAG"],
  [/\bp\s*g\s*vector\b/gi,                         "pgvector"],
  [/\bpage vector\b/gi,                            "pgvector"],
  [/\bpp vector\b/gi,                              "pgvector"],
  [/\bbbm25\b/gi,                                  "BM25"],
  [/\bb\s*m\s*25\b/gi,                             "BM25"],

  // LLM / AI
  [/\bl\s*l\s*m\b/gi,                             "LLM"],
  [/\blarge language model(s)?\b/gi,               "LLM"],
  [/\bagentic ai\b/gi,                             "agentic AI"],
  [/\bagency ai\b/gi,                              "agentic AI"],
  [/\borchestrate?r\b/gi,                          "orchestrator"],

  // Infra terms
  [/\bdo?cker\b/gi,                               "Docker"],
  [/\bread is\b/gi,                               "Redis"],
  [/\breeds\b/gi,                                 "Redis"],
  [/\bpost?gres\b/gi,                             "Postgres"],

  // Compliance / security
  [/\bp\s*c\s*i\s*d\s*s\s*s\b/gi,               "PCI DSS"],
  [/\bp\s*c\s*i\b/gi,                             "PCI"],
  [/\bp\s*i\s*i\b/gi,                             "PII"],
  [/\baadhar\b/gi,                                "Aadhaar"],

  // Platform-specific
  [/\bai next\b/gi,                               "AiNxt"],
  [/\bai nxt\b/gi,                                "AiNxt"],
  [/\bi next\b/gi,                                "AiNxt"],
  [/\bainxt\b/gi,                                  "AiNxt"],
  [/\bopen ai\b/gi,                               "OpenAI"],
  [/\bchat gpt\b/gi,                              "ChatGPT"],
];

function correctTranscript(text) {
  let out = text;
  for (const [pattern, replacement] of STT_CORRECTIONS) {
    out = out.replace(pattern, replacement);
  }
  return out;
}

// Strip markdown so TTS doesn't read out symbols
function cleanForTTS(text) {
  return text
    .replace(/```[\s\S]*?```/g, "code block omitted.")
    .replace(/`[^`\n]+`/g, "")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/\*{1,2}([^*\n]+)\*{1,2}/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/^[-*+]\s+/gm, "")
    .replace(/^\d+\.\s+/gm, "")
    .replace(/https?:\/\/\S+/g, "")       // remove URLs
    .replace(/\s{2,}/g, " ")              // collapse extra whitespace
    .trim();
}

// Safety net: replace any negative phrases that slipped through the LLM prompt
// with positive pivots. Platform mode only — keeps conclave demo 100% positive.
const NEGATIVE_PIVOTS = [
  [/\bwe don'?t (have|support|offer)\b/gi,      "we've built"],
  [/\b(not|isn'?t) (yet )?(available|supported|implemented)\b/gi, "fully integrated"],
  [/\b(limitation|gap|missing feature|shortcoming)\b/gi, "exciting opportunity"],
  [/\bwe can'?t\b/gi,                           "we're designed to"],
  [/\bunfortunately\b/gi,                        "interestingly"],
  [/\bhowever,? (we|it|that)\b/gi,              "and"],
  [/\bwe'?re still working on\b/gi,             "we've been advancing"],
  [/\bon (our )?roadmap\b/gi,                   "in our evolution"],
  [/\bnot (yet )?built\b/gi,                    "actively developing"],
  [/\bi don'?t know\b/gi,                       "great question — our platform addresses this through"],
];

function applyPositivePivots(text) {
  let out = text;
  for (const [pattern, replacement] of NEGATIVE_PIVOTS) {
    out = out.replace(pattern, replacement);
  }
  return out;
}

// Only return sentences that end with terminal punctuation (complete sentences).
// Partial trailing text (no . ! ?) is excluded — checked again on next token.
function extractCompleteSentences(text) {
  const matches = text.match(/[^.!?]+[.!?]+/g) || [];
  return matches.map(s => s.trim()).filter(s => s.length > 8);
}

export default function VoiceMode({ onClose, onSendVoice, micLang = "en-IN", ttsApi }) {
  const [phase, setPhase]           = useState("listening");
  const [transcript, setTranscript] = useState("");
  const [response,   setResponse]   = useState("");
  const [errorMsg,   setErrorMsg]   = useState("");
  const [turnCount,  setTurnCount]  = useState(0);
  const [mode,       setMode]       = useState("generic");
  // Last completed exchange — shown as context during next turn
  const [lastExchange, setLastExchange] = useState(null); // { q, a }

  const recRef            = useRef(null);
  const silenceTimer      = useRef(null);
  const transcriptRef     = useRef("");
  const phaseRef          = useRef("listening");
  const closedRef         = useRef(false);
  const audioRef          = useRef(null);

  // Sentence-level TTS pre-fetch state
  const sentencePromises  = useRef([]);  // Promise<Blob>[] — one per sentence
  const prefetchCount     = useRef(0);   // how many sentences pre-fetched so far

  function setPhaseSync(p) {
    phaseRef.current = p;
    setPhase(p);
  }

  const stopAudio = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      try { URL.revokeObjectURL(audioRef.current.src); } catch (_) {}
      audioRef.current = null;
    }
  }, []);

  const stopAll = useCallback(() => {
    closedRef.current = true;
    clearTimeout(silenceTimer.current);
    try { recRef.current?.stop(); } catch (_) {}
    stopAudio();
    window.speechSynthesis?.cancel();
  }, [stopAudio]);

  function handleClose() {
    stopAll();
    onClose();
  }

  // Play a single Blob and return a Promise that resolves when audio ends
  function playBlobAsync(blob) {
    return new Promise((resolve) => {
      if (closedRef.current) { resolve(); return; }
      stopAudio();
      const url   = URL.createObjectURL(blob);
      const audio = new Audio(url);
      audioRef.current = audio;
      const cleanup = () => {
        URL.revokeObjectURL(url);
        audioRef.current = null;
        resolve();
      };
      audio.onended = cleanup;
      audio.onerror = cleanup;
      audio.play().catch(cleanup);
    });
  }

  // Called on every new accumulated token string from LLM stream
  function onToken(accumulated) {
    // Apply positive pivots before display (platform mode)
    const safe = mode === "platform" ? applyPositivePivots(accumulated) : accumulated;
    setResponse(safe);

    // Only pre-fetch sentences ending with . ! ? (complete sentences)
    const sentences = extractCompleteSentences(cleanForTTS(safe));
    while (prefetchCount.current < sentences.length) {
      sentencePromises.current.push(ttsApi(sentences[prefetchCount.current]));
      prefetchCount.current++;
    }
  }

  // STT → send → progressive TTS loop
  const startListening = useCallback(() => {
    if (closedRef.current) return;
    setPhaseSync("listening");
    setTranscript("");
    setResponse("");
    transcriptRef.current = "";

    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      setErrorMsg("Speech recognition not supported. Use Chrome or Edge.");
      setPhaseSync("error");
      return;
    }

    const rec = new SR();
    rec.lang           = micLang;
    rec.continuous     = true;
    rec.interimResults = true;

    rec.onresult = (e) => {
      const t = Array.from(e.results).map(r => r[0].transcript).join("");
      transcriptRef.current = t;
      setTranscript(t);
      clearTimeout(silenceTimer.current);
      silenceTimer.current = setTimeout(() => rec.stop(), SILENCE_MS);
    };

    rec.onend = async () => {
      clearTimeout(silenceTimer.current);
      if (closedRef.current) return;
      const rawText = transcriptRef.current.trim();
      if (!rawText) { setTimeout(startListening, 400); return; }

      // Correct STT mishearings before sending to backend
      const text = correctTranscript(rawText);

      // Reset pre-fetch state for this turn
      sentencePromises.current = [];
      prefetchCount.current    = 0;

      setPhaseSync("processing");
      setTranscript(text);   // show corrected text in UI
      setResponse("");

      // ── True pipeline: stream + play concurrently ─────────────────────
      // streamTask: runs LLM streaming, pushes TTS promises into sentencePromises
      // playTask:   plays blobs the moment each promise resolves — no waiting for full stream
      // Result: sentence 1 audio starts as soon as its TTS call returns (~500ms),
      // regardless of how long LLM takes to finish the full response.

      let finalAnswer  = "";
      const streamDone = { current: false };

      const streamTask = async () => {
        const rawAnswer = await onSendVoice(text, mode, onToken);
        if (closedRef.current) return;

        const answer = mode === "platform" ? applyPositivePivots(rawAnswer) : rawAnswer;
        finalAnswer  = answer;

        // Flush any remaining complete sentences not yet pre-fetched
        const finalSentences = extractCompleteSentences(cleanForTTS(answer));
        while (prefetchCount.current < finalSentences.length) {
          sentencePromises.current.push(ttsApi(finalSentences[prefetchCount.current]));
          prefetchCount.current++;
        }
        // Fallback: answer has no terminal punctuation (single short phrase)
        if (sentencePromises.current.length === 0) {
          const clean = cleanForTTS(answer).slice(0, 2000);
          if (clean.length > 5) sentencePromises.current.push(ttsApi(clean));
        }
        streamDone.current = true;
      };

      const playTask = async () => {
        let i = 0;
        while (true) {
          if (closedRef.current) return;
          if (i < sentencePromises.current.length) {
            // Switch to "speaking" phase the moment first audio is ready
            if (i === 0) setPhaseSync("speaking");
            try {
              const blob = await sentencePromises.current[i];
              if (closedRef.current) return;
              await playBlobAsync(blob);
            } catch (ttsErr) {
              console.warn("TTS sentence failed:", ttsErr);
              if (i === 0) { setErrorMsg("TTS failed — check OPENAI_API_KEY or server connectivity."); setPhaseSync("error"); return; }
            }
            i++;
          } else if (streamDone.current) {
            break; // LLM done + no more sentences queued
          } else {
            // Yield to allow streamTask / onToken to push more sentences
            await new Promise(r => setTimeout(r, 25));
          }
        }
      };

      try {
        await Promise.all([streamTask(), playTask()]);
        if (closedRef.current) return;
        setResponse(finalAnswer);
        setLastExchange({ q: text, a: finalAnswer });
        setTurnCount(n => n + 1);
        if (phaseRef.current === "speaking") setPhaseSync("listening");
        setTimeout(startListening, 500);
      } catch (err) {
        if (closedRef.current) return;
        setErrorMsg("Failed to get response. Tap mic to retry.");
        setPhaseSync("error");
      }
    };

    rec.onerror = (e) => {
      if (e.error === "aborted") return;
      if (closedRef.current) return;
      setErrorMsg(`Mic error: ${e.error}. Tap to retry.`);
      setPhaseSync("error");
    };

    recRef.current = rec;
    try { rec.start(); } catch (_) {}
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [micLang, onSendVoice, mode]);

  useEffect(() => {
    closedRef.current = false;
    const t = setTimeout(startListening, 600);
    return () => {
      clearTimeout(t);
      stopAll();
    };
  }, []);

  // Orb colours
  const orbColors = {
    listening:  { ring: "bg-blue-400",    core: "bg-blue-500",    shadow: "shadow-blue-500/40"    },
    processing: { ring: "bg-amber-400",   core: "bg-amber-500",   shadow: "shadow-amber-500/40"   },
    speaking:   { ring: "bg-emerald-400", core: "bg-emerald-500", shadow: "shadow-emerald-500/40" },
    error:      { ring: "bg-red-400",     core: "bg-red-500",     shadow: "shadow-red-500/40"     },
  };
  const c = orbColors[phase] || orbColors.listening;

  const phaseLabel = {
    listening:  "Listening…",
    processing: "Thinking…",
    speaking:   "Speaking…",
    error:      "Error",
  }[phase];

  // During processing show streaming text; during speaking show final text
  const showStreamingText = phase === "processing" && response;

  return (
    <div className="absolute inset-0 z-40 flex flex-col items-center justify-center bg-white/98 backdrop-blur-sm">

      {/* Turn counter */}
      {turnCount > 0 && (
        <div className="absolute top-5 left-1/2 -translate-x-1/2">
          <span className="text-xs text-gray-400">
            {turnCount} {turnCount === 1 ? "exchange" : "exchanges"}
          </span>
        </div>
      )}

      {/* Animated orb */}
      <div className="relative flex items-center justify-center w-52 h-52">
        <div className={`absolute inset-0 rounded-full ${c.ring} opacity-10
          ${phase !== "processing" ? "animate-ping" : ""}`}
          style={{ animationDuration: "2s" }}
        />
        <div className={`absolute inset-4 rounded-full ${c.ring} opacity-15
          ${phase === "listening" ? "animate-ping" : ""}`}
          style={{ animationDuration: "1.4s", animationDelay: "0.3s" }}
        />
        <div className={`absolute inset-8 rounded-full ${c.ring} opacity-20
          ${phase === "listening" ? "animate-ping" : ""}`}
          style={{ animationDuration: "1s", animationDelay: "0.6s" }}
        />
        <div className={`relative w-28 h-28 rounded-full ${c.core} ${c.shadow} shadow-2xl
          flex items-center justify-center transition-colors duration-500`}>
          {phase === "listening"  && <Mic     size={44} className="text-white drop-shadow" />}
          {phase === "processing" && <Loader2 size={44} className="text-white animate-spin drop-shadow" />}
          {phase === "speaking"   && <Volume2 size={44} className="text-white drop-shadow" />}
          {phase === "error"      && <MicOff  size={44} className="text-white drop-shadow" />}
        </div>
      </div>

      {/* Status label */}
      <p className="mt-8 text-gray-800 text-xl font-medium tracking-wide">{phaseLabel}</p>

      {/* Transcript / streaming response / final response */}
      <div className="mt-4 px-8 max-w-xl w-full text-center min-h-[6rem]">
        {phase === "listening" && (
          <>
            <p className="text-gray-400 text-sm leading-relaxed mb-3">
              {transcript || "Say something…"}
            </p>
            {/* Show last exchange as conversation context */}
            {lastExchange && (
              <div className="mt-2 rounded-xl bg-gray-50 border border-gray-100 px-4 py-3 text-left space-y-1.5">
                <p className="text-xs text-gray-400 font-medium uppercase tracking-wide">Last exchange</p>
                <p className="text-xs text-gray-500 italic">"{lastExchange.q}"</p>
                <p className="text-xs text-gray-700 leading-relaxed line-clamp-3">{lastExchange.a}</p>
              </div>
            )}
          </>
        )}
        {phase === "processing" && !showStreamingText && (
          <p className="text-amber-600 text-sm leading-relaxed italic">"{transcript}"</p>
        )}
        {phase === "processing" && showStreamingText && (
          <div className="rounded-xl bg-amber-50 border border-amber-100 px-4 py-3 text-left">
            <p className="text-xs text-amber-500 font-medium mb-1.5">Generating response…</p>
            <p className="text-gray-700 text-sm leading-relaxed line-clamp-5">{response}</p>
          </div>
        )}
        {phase === "speaking" && (
          <div className="rounded-xl bg-emerald-50 border border-emerald-100 px-4 py-3 text-left">
            <p className="text-xs text-emerald-600 font-medium mb-1.5 flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
              Speaking
            </p>
            <p className="text-gray-700 text-sm leading-relaxed line-clamp-5">{response}</p>
          </div>
        )}
        {phase === "error" && (
          <p className="text-red-500 text-sm">{errorMsg}</p>
        )}
      </div>

      {/* Retry */}
      {phase === "error" && (
        <button
          onClick={() => { setErrorMsg(""); startListening(); }}
          className="mt-6 px-5 py-2.5 bg-gray-100 hover:bg-gray-200 text-gray-700 text-sm rounded-full transition"
        >
          Tap to retry
        </button>
      )}

      {/* Skip TTS */}
      {phase === "speaking" && (
        <button
          onClick={() => {
            stopAudio();
            // Cancel remaining pre-fetched promises by clearing the array
            sentencePromises.current = [];
            setTimeout(startListening, 300);
          }}
          className="mt-6 px-5 py-2.5 mb-5 bg-gray-100 hover:bg-gray-200 text-gray-700 text-sm rounded-full transition"
        >
          Skip — listen now
        </button>
      )}

      {/* Language badge */}
      <div className="absolute bottom-5 mt-5 text-xs text-gray-400 uppercase tracking-widest">
        {micLang}
      </div>

      {/* Close */}
      <button
        onClick={handleClose}
        className="absolute bottom-6 right-6 p-3 rounded-full bg-gray-100 hover:bg-red-100 text-gray-500 hover:text-red-500 transition-colors duration-200"
        title="Exit voice mode"
      >
        <X size={20} />
      </button>
    </div>
  );
}
