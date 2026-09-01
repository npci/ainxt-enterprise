// SPDX-License-Identifier: Apache-2.0
/**
 * Shared name validation for workflows and agents.
 *
 * Mirrors the rules enforced by the backend in
 * ``ABStudio/backend/app/core/workflow_repo.py`` (see ``_validate_name_format``):
 *   - non-empty after trimming
 *   - length ≤ 100 characters
 *   - cannot be digits only (e.g. "123" is rejected)
 *   - must start with a letter; remaining chars: letters, digits, spaces,
 *     and common name punctuation ( . _ - & / , ' ( ) : )
 *
 * Returns ``null`` if valid, otherwise a human-readable error string ready to
 * display next to the input. Uniqueness is enforced server-side; we surface
 * that error reactively when the API call returns 400.
 *
 * @param {string} rawName
 * @param {"workflow" | "agent"} [kind]
 * @param {{
 *   existingNames?: string[],
 *   existingItems?: Array<{ id?: string, name: string }>,
 *   currentName?: string,
 *   currentId?: string,
 * }} [opts]
 *        Two equivalent ways to pre-check uniqueness:
 *
 *        1. Pass ``existingItems`` (an array of ``{id, name}`` objects)
 *           together with ``currentId``. This is the preferred shape — the
 *           row being edited is excluded by id, so renaming a workflow that
 *           lives in the dashboard list never falsely collides with itself
 *           after an autosave has updated its name in the list.
 *
 *        2. Legacy: pass ``existingNames`` (a plain array of strings) and
 *           ``currentName`` (the *original* name at load time). The check
 *           is case-insensitive and ``currentName`` is excluded by name.
 * @returns {string | null}
 */
export function validateEntityName(rawName, kind = 'workflow', opts = {}) {
    const label = kind === 'agent' ? 'Agent' : 'Workflow';

    if (rawName == null) {
        return `${label} name is required.`;
    }
    if (typeof rawName !== 'string') {
        return `${label} name must be text.`;
    }
    const name = rawName.trim();
    if (!name) {
        return `${label} name is required.`;
    }
    if (name.length > 100) {
        return `${label} name is too long (max 100).`;
    }
    if (/^\d+$/.test(name)) {
        return `${label} name must start with a letter.`;
    }
    if (!/^[A-Za-z][A-Za-z0-9 _.\-&/,'():]{0,99}$/.test(name)) {
        return `Invalid ${label.toLowerCase()} name. Use letters, digits, spaces, . _ - & / , ' ( ) :`;
    }

    const { existingNames, existingItems, currentName, currentId } = opts;
    const lower = name.toLowerCase();

    // Preferred: id-based exclusion. Avoids the "renaming itself collides
    // with itself" false-positive when the dashboard list has already been
    // updated by an autosave.
    if (Array.isArray(existingItems)) {
        const skipId = currentId || '';
        const clash = existingItems.some((item) => {
            if (!item || typeof item.name !== 'string') return false;
            if (skipId && item.id === skipId) return false;
            return item.name.trim().toLowerCase() === lower;
        });
        if (clash) return `Name already in use.`;
    }

    // Legacy: name-based exclusion. Kept for callsites that don't have the
    // ``id`` of the row being edited.
    if (Array.isArray(existingNames)) {
        const skip = (currentName || '').trim().toLowerCase();
        const clash = existingNames.some(
            (n) => typeof n === 'string' && n.trim().toLowerCase() === lower && lower !== skip,
        );
        if (clash) return `Name already in use.`;
    }

    return null;
}

/**
 * Return a collision-free version of ``desiredName``.
 *
 * If the trimmed name doesn't clash with any ``existingItems`` (case-insensitive,
 * excluding the row identified by ``currentId``), it's returned unchanged.
 * Otherwise the lowest free " N" suffix (N ≥ 2) is appended, stripping any
 * existing trailing number first: "Onboarding" → "Onboarding 2" → "Onboarding 3".
 *
 * This lets the UI silently pick a free name instead of blocking the user with a
 * "Name already in use." error. Format validation (charset/length) is a separate
 * concern handled by ``validateEntityName`` — this only resolves duplicates.
 *
 * @param {string} desiredName
 * @param {Array<{ id?: string, name: string }>} existingItems
 * @param {string} [currentId]  id of the row being edited (excluded from clashes)
 * @returns {string} a name that doesn't collide with existingItems
 */
export function suggestFreeName(desiredName, existingItems = [], currentId = '') {
    const raw = (desiredName == null ? '' : String(desiredName)).trim();
    if (!raw) return raw;

    const items = Array.isArray(existingItems) ? existingItems : [];
    const skipId = currentId || '';
    const taken = new Set(
        items
            .filter((it) => it && typeof it.name === 'string' && !(skipId && it.id === skipId))
            .map((it) => it.name.trim().toLowerCase()),
    );

    if (!taken.has(raw.toLowerCase())) return raw;

    // Strip an existing trailing " N" so re-bumping doesn't stack ("X 2 3").
    const base = raw.replace(/\s+\d+$/, '').trim() || raw;
    for (let n = 2; n < 10000; n++) {
        const candidate = `${base} ${n}`;
        if (!taken.has(candidate.toLowerCase())) return candidate;
    }
    return raw; // pathological fallback — should never hit
}
