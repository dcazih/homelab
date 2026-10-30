# Soulseek Peer-Aware Request Scheduler

This patch targets the current `music-requests-mac` tree. It extends the existing custom DroppedNeedle recovery image and Music Profiles Bridge. Cassette, slskd, Navidrome, existing custom base images, and existing music data are unchanged.

## Installed components

- `droppedneedle-recovery-v1/peer_scheduler.py`: durable SQLite peer statistics, queue admission, limits, search cache and rate gate, retry/backoff policy, active transfer ledger, manual peer controls, and acquisition phases. The database is in the separate `music-requests-mac_peer-scheduler-data` Docker volume.
- `droppedneedle-recovery-v1/slskd_repository.py`: gates slskd enqueue by peer, resumes deferred batches when eligible, correlates uncertain enqueues with existing slskd records, polls transfers, tracks speed/queue positions, and flags stalled transfers. It never retries an uncertain enqueue blindly.
- `droppedneedle-recovery-v1/strategy.py`: records searching, verified alternatives, source switching and verification handoff. Historical reliability and active peer load break ties only among candidates with equivalent existing acceptance tier, quality step, codec, and rounded identity score. DroppedNeedle's exact-edition verification and import pipeline remain in charge.
- `profiles/shared_acquisition.py`: claims an exact recording plus edition once across Bridge profiles before remote submission. Other profiles retain their own request records and are fulfilled by the existing shared-library reconciliation.
- `profiles/peer_scheduler_api.py` and `profiles/index.html`: admin-only Scheduler Settings, Peer History, and Active Scheduler controls. Settings are read by new scheduling decisions without a restart. A native DroppedNeedle session token is supplied only when retrying a failed task and is not stored by Bridge.

## Defaults

One active batch, one queued album, 15 queued files, 500 MB queued, and two new batches per peer per 24 hours; 30-minute batch cooldown and duplicate-search cooldown; 5-minute pending checks. Additional retry, stall, search, cache, concurrency, pause, and fairness controls are editable in the dashboard.

## Install

From the project root, run `share/install.sh "$PWD"`. The script saves source, image IDs, and consistent Bridge/DroppedNeedle database backups under the private `.peer-scheduler-backups/` directory, creates the scheduler volume, builds only DroppedNeedle and Bridge, and recreates only those two services with `--no-deps`.

The dashboard is at the existing Music Profiles Bridge URL. Open **Peer History**, enter the existing Bridge admin key, and select **Open scheduler**. The key stays in the browser field. The three sections appear on the same page.

## Verification boundary

The live Bridge API and dashboard were checked against the scheduler volume, and both deployed services reached healthy state. Offline tests cover admission, blocks, statistics, search cache, retry controls, reliability tie-breaking, shared claims, and slskd transfer reconciliation. A real Soulseek download was not initiated solely for testing. slskd exposes locked search files, which are excluded and recorded as restrictions; other peer-specific text rules need an administrator to record a block or numeric limit because no trustworthy structured rule signal was found in the installed slskd adapter.

See `test-results.txt` and `ROLLBACK.md` for commands and recovery steps.
