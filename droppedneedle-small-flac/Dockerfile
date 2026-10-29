FROM cassette-dn-compact-base:6d58d829cc5a
COPY track_matcher.py /app/services/native/track_matcher.py
COPY album_preflight_scorer.py /app/services/native/album_preflight_scorer.py
RUN python -m py_compile /app/services/native/track_matcher.py /app/services/native/album_preflight_scorer.py
RUN find /app -type f ! -name .droppedneedle-source-revision -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1 > /app/.droppedneedle-source-revision
