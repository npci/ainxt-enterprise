// SPDX-License-Identifier: MIT
// Document skeleton — copy this file and replace only the CONTENT lines.
// `ainxt-doc` is PREINSTALLED in the sandbox: A4 geometry, the brand type scale,
// heading rules (with keepNext), table styling and the page footer all live inside
// it. This is also the PDF path — write output.docx and it is exported for you.
// Runs as-is.

const doc = require('ainxt-doc');

const d = doc.create({
  title: 'REPLACE — Document Title',
  subtitle: 'REPLACE — one-line subtitle',
  date: '31 Dec 2025',
  classification: 'Confidential',
});

// Spine: Purpose → Summary → Body → Data → Risks → Next steps.
// The answer goes in Summary, before the detail. Never emit an empty heading.

d.h1('Purpose');
d.p('REPLACE — two to four sentences on why this document exists and what decision it supports.');

d.h1('Summary');
// Single bullet: d.bullet('text')
// Multiple bullets at once: d.bullets(['a', 'b', 'c'])  ← pass an ARRAY, not separate strings
d.bullets([
  'REPLACE — the headline finding, stated as a conclusion.',
  'REPLACE — the second finding.',
  'REPLACE — the third finding.',
]);

d.h1('REPLACE — first body section');
d.p('REPLACE — body paragraph, four sentences maximum.');
d.h2('REPLACE — subsection');
d.p('REPLACE — body paragraph.');

d.h1('Data');
// Every figure below is illustrative. REPLACE them with data the user actually
// gave you — if the user gave no numbers, drop this section rather than inventing.
d.table(['Member bank', 'Cycles', 'Value (₹ Cr)', 'Exceptions'],
        [['Bank A', '1,204', '8,431', '3'],
         ['Bank B', '987',   '6,220', '1']],
        { pct: [34, 22, 22, 22], rightCols: [1, 2, 3] });   // numerals right-aligned
d.caption('REPLACE — what this table shows and the period it covers.');

d.h1('Risks');
d.bullet('REPLACE — risk and its mitigation.');

d.h1('Next steps');
d.step('REPLACE — action, owner, and date.');

d.save();   // writes /work/output.docx
