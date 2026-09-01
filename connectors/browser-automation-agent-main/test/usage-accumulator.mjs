// Dev-only tests for the LLM usage accumulator in lib/runner.js (no framework).
// Token accounting fails quietly — a dropped `onUsage` or a mis-seeded
// accumulator shows a plausible-looking smaller number, not an error — so these
// assertions are the guard for the arithmetic the run bubble reports.
// Run: node test/usage-accumulator.mjs
import { newUsageAccumulator, accumulateUsage } from "../lib/runner.js";

let failed = 0;
function eq(name, got, want) {
  if (got === want) {
    console.log(`PASS ${name}`);
  } else {
    failed++;
    console.log(`FAIL ${name}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`);
  }
}

// ---- fresh accumulator ----
{
  const acc = newUsageAccumulator();
  eq("fresh: total_tokens", acc.total_tokens, 0);
  eq("fresh: llm_calls", acc.llm_calls, 0);
  eq("fresh: usage_missing", acc.usage_missing, 0);
  eq("fresh: notes", JSON.stringify(acc.notes), "[]");
}

// ---- basic accumulation across calls ----
{
  const acc = newUsageAccumulator();
  accumulateUsage(acc, { usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 } });
  accumulateUsage(acc, { usage: { prompt_tokens: 300, completion_tokens: 40, total_tokens: 340 } });
  eq("accumulate: prompt_tokens", acc.prompt_tokens, 400);
  eq("accumulate: completion_tokens", acc.completion_tokens, 60);
  eq("accumulate: total_tokens", acc.total_tokens, 460);
  eq("accumulate: llm_calls", acc.llm_calls, 2);
  eq("accumulate: usage_missing", acc.usage_missing, 0);
}

// ---- total_tokens falls back to prompt+completion when the endpoint omits it ----
{
  const acc = newUsageAccumulator();
  accumulateUsage(acc, { usage: { prompt_tokens: 70, completion_tokens: 30 } });
  eq("no total_tokens: derived", acc.total_tokens, 100);
}

// ---- a missing usage block counts the call but fabricates no tokens ----
{
  const acc = newUsageAccumulator();
  accumulateUsage(acc, { usage: null });
  accumulateUsage(acc, {}); // onUsage fired with no usage key at all
  eq("missing usage: total_tokens", acc.total_tokens, 0);
  eq("missing usage: llm_calls", acc.llm_calls, 2);
  eq("missing usage: usage_missing", acc.usage_missing, 2);
}

// ---- partially-reported run: tokens counted, shortfall recorded ----
{
  const acc = newUsageAccumulator();
  accumulateUsage(acc, { usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } });
  accumulateUsage(acc, { usage: null });
  eq("partial: total_tokens", acc.total_tokens, 15);
  eq("partial: llm_calls", acc.llm_calls, 2);
  eq("partial: usage_missing", acc.usage_missing, 1);
}

// ---- garbage values never poison the totals with NaN ----
{
  const acc = newUsageAccumulator();
  accumulateUsage(acc, { usage: { prompt_tokens: "abc", completion_tokens: undefined, total_tokens: null } });
  eq("garbage: total_tokens is 0 not NaN", acc.total_tokens, 0);
  eq("garbage: counted as reported", acc.usage_missing, 0);
}

// ---- seeding: pre-run (plan preview) tokens carry into the run's accumulator ----
{
  const seed = newUsageAccumulator();
  accumulateUsage(seed, { usage: { prompt_tokens: 900, completion_tokens: 100, total_tokens: 1000 } });
  accumulateUsage(seed, { usage: null });
  seed.notes.push("Primary endpoint failed — served by https://alt.example");

  const acc = newUsageAccumulator(seed);
  eq("seeded: prompt_tokens", acc.prompt_tokens, 900);
  eq("seeded: total_tokens", acc.total_tokens, 1000);
  eq("seeded: llm_calls", acc.llm_calls, 2);
  eq("seeded: usage_missing", acc.usage_missing, 1);
  eq("seeded: notes carried", acc.notes.length, 1);

  // Seeded notes must be a copy — mutating the run must not reach back into the
  // seed the caller still holds.
  acc.notes.push("second note");
  eq("seeded: notes are copied, not shared", seed.notes.length, 1);

  accumulateUsage(acc, { usage: { prompt_tokens: 50, completion_tokens: 10, total_tokens: 60 } });
  eq("seeded: run tokens add to the seed", acc.total_tokens, 1060);
  eq("seeded: seed is unchanged by the run", seed.total_tokens, 1000);
}

// ---- seeding tolerates null / partial seeds ----
{
  eq("seed null: total_tokens", newUsageAccumulator(null).total_tokens, 0);
  eq("seed partial: total_tokens", newUsageAccumulator({ total_tokens: 7 }).total_tokens, 7);
  eq("seed partial: llm_calls defaults", newUsageAccumulator({ total_tokens: 7 }).llm_calls, 0);
  eq("seed junk notes: ignored", JSON.stringify(newUsageAccumulator({ notes: "nope" }).notes), "[]");
}

// ---- fallback notes: recorded once per endpoint, only on fallback ----
{
  const acc = newUsageAccumulator();
  const alt = "https://alt.example/v1";
  accumulateUsage(acc, { usage: null, servedBy: "https://primary.example/v1", isFallback: false });
  eq("notes: primary adds none", acc.notes.length, 0);
  accumulateUsage(acc, { usage: null, servedBy: alt, isFallback: true });
  accumulateUsage(acc, { usage: null, servedBy: alt, isFallback: true });
  eq("notes: fallback deduped", acc.notes.length, 1);
  eq("notes: names the endpoint", acc.notes[0].includes(alt), true);
  accumulateUsage(acc, { usage: null, servedBy: "https://third.example/v1", isFallback: true });
  eq("notes: second endpoint adds one", acc.notes.length, 2);
}

if (failed) {
  console.log(`\n${failed} usage-accumulator assertion(s) failed.`);
  process.exitCode = 1;
} else {
  console.log("\nAll usage-accumulator tests passed.");
}
