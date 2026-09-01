// SPDX-License-Identifier: Apache-2.0
// Deck skeleton — copy this file and replace only the CONTENT lines.
// `ainxt-deck` is PREINSTALLED in the sandbox: every colour, size, margin and
// accessibility rule from INTERNAL_BRAND.md lives inside it, so you write content only.
// Runs as-is.

const deck = require('ainxt-deck');

const d = deck.create({ classification: 'Confidential' });   // Public|Internal|Confidential|Restricted

// Slide plan: one idea per slide; never two identical patterns back to back;
// alternate dark (cover/statement/close) and light (everything else).

d.cover('REPLACE — Deck Title',
        'REPLACE — one-line subtitle',
        '31 Dec 2025');

d.contents('What this covers', [
  'REPLACE — section one',
  'REPLACE — section two',
  'REPLACE — section three',
]);

d.metric('Headline position', [
  { figure: '98.7%', label: 'REPLACE — what this measures', status: 'good' },
  { figure: '1.3%',  label: 'REPLACE — what this measures', status: 'warn' },
  { figure: '0',     label: 'REPLACE — what this measures', status: 'good' },
], 'REPLACE — one sentence of context, or omit this argument.');

d.evidence('REPLACE — what the numbers mean', [
  'REPLACE — first point',
  'REPLACE — second point',
  'REPLACE — third point',
], { chart: { type: 'bar',
              data: [{ name: 'REPLACE — series name',
                       labels: ['Oct', 'Nov', 'Dec'],
                       values: [2.1, 1.4, 0.9] }] } });
// Bars start at zero. If your values cluster far from zero (98.1/98.6/99.1),
// chart the COMPLEMENT (the exception rate) so the movement is real and honest.

d.statement('REPLACE — the single sentence you want remembered.');

d.split('Risk and mitigation', [
  { title: 'Risk',       bullets: ['REPLACE', 'REPLACE'] },
  { title: 'Mitigation', bullets: ['REPLACE', 'REPLACE'] },
]);

d.table('REPLACE — table heading',
        ['Member bank', 'Cycles', 'Value (₹ Cr)', 'Status'],
        [['Bank A', '1,204', '8,431', 'Settled'],
         ['Bank B', '987',   '6,220', 'Settled']],
        { colW: [3.6, 2.6, 3.0, 2.9], rightCols: [1, 2] });   // numerals right-aligned

d.close('REPLACE — closing line', [
  'REPLACE — next step, owner, date',
]);

d.save();   // writes /work/output.pptx
