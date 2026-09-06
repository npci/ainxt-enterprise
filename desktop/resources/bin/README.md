# resources/bin

Populated at build time by `desktop/scripts/fetch-cli.mjs` (run via the
electron-builder `beforePack` hook). **Do not commit binaries here** —
`desktop/resources/bin/ainxt-*` is git-ignored.

The embedded CLI version is pinned in `desktop/cli-version.json`.
Manual populate: `npm run fetch-cli -- --target=host`
Air-gapped:      `AINXT_CLI_BIN_SRC=/path/to/dir npm run fetch-cli -- --target=win`
From source:     `AINXT_CLI_FROM_SOURCE=1 npm run fetch-cli -- --target=host`
Skip entirely:   `AINXT_CLI_SKIP_FETCH=1`
