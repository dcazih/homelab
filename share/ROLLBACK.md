# Rollback

For this installation, run:

```bash
/Users/squarescan/Downloads/music-requests-mac/share/rollback.sh /Users/squarescan/Downloads/music-requests-mac/.peer-scheduler-backups/20261029-144834
```

The script restores the pre-scheduler source and image IDs, then recreates only DroppedNeedle and Music Profiles Bridge with `--no-deps --no-build`. It leaves the scheduler volume, Bridge database, DroppedNeedle database, music files, and every other Docker image and container intact. Keep the backup directory and the `before-peer-scheduler-20261029` image tags until satisfied with the deployment.

To inspect the saved data before rollback:

```bash
python3 -c 'import sqlite3; print(sqlite3.connect(".peer-scheduler-backups/20261029-144834/data/profiles.db").execute("PRAGMA integrity_check").fetchone()[0])'
python3 -c 'import sqlite3; print(sqlite3.connect(".peer-scheduler-backups/20261029-144834/data/droppedneedle-library.db").execute("PRAGMA integrity_check").fetchone()[0])'
```

Restoring database bytes is a separate recovery action and is not part of the rollback script, since doing so would discard requests made after installation.
