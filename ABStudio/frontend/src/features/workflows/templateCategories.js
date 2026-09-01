// SPDX-License-Identifier: Apache-2.0
// Shared domain-category taxonomy for the workflow template catalog.
// Kept as a fixed list (rather than deriving options from whatever is in
// the DB) so the catalog filter chips and the template create/edit forms
// can never drift into the old free-text mess (e.g. "Support, Finance,
// Legal, HR") that the visibility-chip refactor replaced. Add a domain
// here and it becomes available everywhere: the dashboard filter row and
// both admin modals.
export const CATEGORY_OPTIONS = [
    'Security',
    'Finance',
    'HR',
    'Sales & Marketing',
    'Operations',
    'Compliance',
    'Engineering',
    'Research & Exec',
];

// Default for brand-new templates. Named explicitly (rather than
// `CATEGORY_OPTIONS[0]`) so the default doesn't silently change if the
// taxonomy above gets reordered, and so it isn't an arbitrary domain like
// "Security" just because it happens to sort first.
export const DEFAULT_CATEGORY = 'Operations';
