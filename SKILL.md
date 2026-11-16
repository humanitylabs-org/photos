---
name: photos
description: "Use when the owner asks about his photos, Google Photos, Takeout import, faces/people, or deleting photos."
version: 0.1.0
author: Oscar Henry Collins, Hermes Agent
license: AGPL-3.0-only
metadata:
  hermes:
    tags: [photos, immich, google-takeout, faces, backup]
    related_skills: [files]
---
# Photos (Immich)

The owner's own Google Photos replacement: unmodified Immich on his box, opened in Wizard at `?open=photos`. Tools come through the single Wizard MCP gateway as `photos_*`.

## Tools
- `photos_status`: start here. Immich up?, owner account made?, agent connected?, photo/video counts, background work (face detection/grouping, search indexing), import state, free disk, links (`links.mobile_server_url` is what the Immich phone app needs), and `next` = the owner's next step. Read `next` back to him in plain words.
- `photos_people`: face groups by name with photo counts (`include_unnamed` for unnamed groups). Faces appear only after the background work finishes; a big import takes hours to a day on CPU.
- `photos_search`: `query` = meaning search ("dog on the beach"); without it, filters (`person` name or id, `taken_after`/`taken_before` YYYY-MM-DD, `media_type`, `city`, `favorite`) with an exact `total_matching`.
- `photos_move_to_trash`: by `asset_ids` or by `person` and/or dates. Always twice: first call (no `confirmed`) returns the count and a sample. Tell the owner the exact number, get his explicit yes, then call with `confirmed=true, expected_count=<that number>`. Moves to Immich's trash (30 days by default); never permanent. It refuses an empty filter.
- `photos_restore_from_trash`: undo, by ids or `everything=true`.
- `photos_import_status` / `photos_import_start`: Google Takeout import (below). `photos_import_start` without `confirmed` only lists the files.

Permanent deletion is the owner's own act in Immich: Trash › Empty trash (or wait 30 days). Never claim a photo is gone for good after `photos_move_to_trash`.

## First open (owner only; never do this for him)
1. Open Photos in Wizard. Immich shows **Getting started**: he enters his email, name and a password he chooses (he is the admin). Never type or suggest passwords.
2. Tap **Agent › Let my agent help** (while signed in). That makes a narrow Immich API key kept on the box; until then every tool except `photos_status` answers "not connected".
3. Phone backup: install **Immich** from Google Play/F-Droid/App Store, server URL = `links.mobile_server_url` (his Tailscale address with port 8871; Tailscale must be on), sign in, Backup › choose albums (Camera) › turn on backup. Optional: Settings › Background backup.

## Google Takeout (Photos only)
1. On a computer: takeout.google.com › **Deselect all** › tick only **Google Photos** (All photo albums included) › Next step.
2. Destination **Send download link via email**, Frequency **Export once**, File type **.zip**, File size **50 GB** › Create export. Google emails a link in hours to days.
3. Download every part (`takeout-…-001.zip`, `-002.zip`, …). Put all of them in **Files › Photos › Import** (upload in Files, or copy to `~/runtipi/media/Photos/Import/` on the box). Downloading straight on the box is fine too.
4. Wait until every part is there (parts share JSON across zips), then **Photos › Import from Google › Import now**, or `photos_import_start(confirmed=true)`.
5. Watch `photos_import_status`. Re-running is safe: immich-go skips what Immich already has (checksums). Options on the Import sheet: archived (on), partner-shared (on), albums (on), Google trash (off).
6. After it finishes and he has checked the library, the zips in Import can be deleted to free space (the photos now live in Photos/Library). Wiping Google is his own step; suggest he keeps the Takeout zips until he has checked.

immich-go v0.32.0 (AGPL-3.0, github.com/simulot/immich-go) is downloaded on the first import and checked against its pinned SHA-256; it runs inside the Photos front with Immich's jobs paused during upload.

## Where things are
- Originals and thumbnails: `~/runtipi/media/Photos/Library` (Immich-managed; don't rename or edit files there). Takeout drop folder: `~/runtipi/media/Photos/Import`.
- Database, its random password, the agent key and ML models: the app's Docker volumes `photos_wizardapps_photos-db`, `…_photos-secrets`, `…_photos-state`, `…_photos-model-cache` (kept across app updates; an uninstall that removes volumes deletes the database, not the photo files).
- Immich full screen: the app's own port (`links.immich`). The Wizard page at `/photos/` frames it and adds Import and Agent.

## Failure notes
- "not connected" or 401: the key was deleted in Immich; owner taps Agent › Let my agent help again.
- Import `failed`: read `log_tail`; common causes are a missing zip part or a full disk (`photos_status.disk`). Re-run after fixing.
- Immich not reachable for >5 min after install: check `docker ps --filter name=photos` health and `docker logs photos_wizardapps-photos-server-1`.
