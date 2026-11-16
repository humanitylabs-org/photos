# Photos for Wizard (Immich)

Your own Google Photos replacement on your [Wizard](https://github.com/humanitylabs-org/wizard-os): [Immich](https://immich.app) v3.4.1, unmodified and pinned by digest, with a small Wizard front.

| Service | Image | What it is |
|---|---|---|
| `photos` (main) | `python:3.12-alpine@sha256:1b66…` | Wizard front: page at `/photos/`, agent tools at `/mcp`, passes everything else unchanged to Immich |
| `photos-server` | `ghcr.io/immich-app/immich-server:v3.4.1@sha256:7c48…` | Immich server and web app |
| `photos-ml` | `ghcr.io/immich-app/immich-machine-learning:v3.4.1@sha256:8ade…` | Faces and smart search (CPU) |
| `photos-db` | `ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0@sha256:bcf6…` | Postgres + VectorChord (as pinned by Immich) |
| `photos-redis` | `docker.io/valkey/valkey:9@sha256:4186…` | Valkey (as pinned by Immich) |

Images and environment follow Immich's own [v3.4.1 docker-compose.yml](https://github.com/immich-app/immich/releases/download/v3.4.1/docker-compose.yml). Differences: the database password is random per box (made on first start into a private volume and read through Immich's documented `DB_PASSWORD_FILE`), resource limits, named volumes, and the library under the owner's Files (`Photos/Library`).

## Owner flow
1. Open Photos in Wizard. Create the Immich admin account (Getting started).
2. Agent › **Let my agent help**: one tap, while signed in, makes a narrow Immich API key that stays on the box.
3. Import from Google: Takeout (Google Photos only, .zip, 50 GB) → put every part in `Files/Photos/Import` → **Import now**. Uses [immich-go](https://github.com/simulot/immich-go) v0.32.0, fetched on first use and checked against its release SHA-256.
4. Phone: the official Immich app, server = the app's own Tailscale address (`https://<box>.ts.net:8871`).

## Agent tools (MCP, box-only)
`photos_status`, `photos_people`, `photos_search`, `photos_move_to_trash` (preview, then confirmed with the exact count; Immich trash only, never `force`), `photos_restore_from_trash`, `photos_import_status`, `photos_import_start`. See [SKILL.md](SKILL.md).

## Develop
Edit `mcp/front.py`, `mcp/tools.py` or `web/`, then `python3 mcp/inline.py` (regenerates the compose `command` and `app/front.py`), `python3 -m unittest discover -s tests`, `wam verify .`.

## License and attribution
Immich is © Immich contributors, [AGPL-3.0](https://github.com/immich-app/immich/blob/main/LICENSE); used unmodified from its official images. immich-go is © simulot and contributors, AGPL-3.0, downloaded unmodified. This packaging is AGPL-3.0-only (`LICENSE`). The shared Wizard UI kit files in `web/` (`wizard-*.css`, `boot.js`, `wizard-sheet.js`, `standalone*.css`) come from humanitylabs-org/wizard-os under GPL-3.0 (`LICENSE-GPL-3.0-kit`, `appearance-provenance.json`). Immich is not affiliated with this package. Not affiliated with Google.
