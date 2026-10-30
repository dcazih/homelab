# Profile client update

The Music Profiles Bridge provides Navidrome sign-in, persistent per-device Bridge sessions, separate Bridge admin login, account management, and saved dashboard island layouts. The admin layout is shared and takes precedence during an admin session. The Output island can be dragged and its position is saved. Removal uses an inline cancel/confirm control, and the profile menu and page transitions animate. Scheduler retries can use the personally linked DroppedNeedle session. On the Account page, Bridge administrators can view enrolled Bridge accounts and revoke sessions. Resetting a Navidrome password requires the signed-in personal Navidrome account to have administrator rights.

The custom DroppedNeedle recovery image also includes the supplied fonts. The scheduler package has been updated to include the client changes and font assets. Its install and rollback scripts preserve database volumes and save the previous image IDs and affected source files before changes. Existing font directories are backed up when present; rollback retains newly added assets if there was no previous asset directory.

Verification: Bridge login against the running Navidrome succeeded and the temporary test session was revoked. In Safari, the enrollment link, profile menu, Account page, closable admin dialog, dashboard drag and layout persistence, and floating Output drag were checked. The Navidrome password reset API was tested with a mock server; no live password was changed. The DroppedNeedle static generator successfully rewrote the font stylesheet and assets for a non-root base path. The Bridge and DroppedNeedle containers reported healthy.

Before the update, the Bridge image was tagged `music-profiles-bridge:before-login-ui-20261029` and the DroppedNeedle image was tagged `cassette-dn-recovery-v1:before-typography-20261029`. A SQLite Bridge backup was saved inside its data volume as `profiles-login-ui-backup-20261029.db`. The scheduler rollback instructions remain in ROLLBACK.md.

The supplied refresh, back, cancel, and confirm icons are included. Browser checks used a disposable profile and did not alter any real request. The refresh icon rotates, page navigation starts at the top, and the administrator dialog and section size changes animate.

My Requested Music includes song and album requests. Clear All cancels active requests before removing them; shared acquisitions continue for other listeners and unconfirmed cancellations remain visible. Output stays inside the viewport and snaps to the bottom.
