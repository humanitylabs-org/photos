# Source and license notice

- **Immich** (https://github.com/immich-app/immich), © Immich contributors, AGPL-3.0. Used unmodified from its official, digest-pinned images (`immich-server`, `immich-machine-learning`, `postgres` with VectorChord) at release v3.4.1. Service layout follows Immich's release `docker-compose.yml`. Immich's API is called only through its documented REST endpoints (https://api.immich.app).
- **Valkey** (https://valkey.io), BSD-3-Clause, official image as pinned by Immich.
- **immich-go** (https://github.com/simulot/immich-go), © simulot and contributors, AGPL-3.0. Release v0.32.0 is downloaded unmodified on the owner's first import and checked against the release's published SHA-256.
- **Wizard UI kit** (`web/wizard-tokens.css`, `web/wizard-ui.css`, `web/boot.js`, `web/wizard-sheet.js`, `web/standalone*.css`) from humanitylabs-org/wizard-os, GPL-3.0-only, unmodified; see `appearance-provenance.json` and `LICENSE-GPL-3.0-kit`.
- **MCP front** (`mcp/front.py`) adapted from the Wizard app pattern used by oscarhenrycollins/files and smart-home (route matcher, extra response headers, startup hook added).
- Everything else in this repository: © 2026 Oscar Henry Collins, AGPL-3.0-only (`LICENSE`). No warranty.

Immich and immich-go are independent projects and do not endorse this package. Google and Google Photos are trademarks of Google LLC; this package is not affiliated with Google.
