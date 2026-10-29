FROM cassette-dn-recovery-base:c3dd5cd127b3
COPY slskd_repository.py /app/repositories/slskd/slskd_repository.py
COPY scoring_core.py /app/services/native/acquisition/scoring_core.py
COPY peer_scheduler.py /app/repositories/slskd/peer_scheduler.py
COPY strategy.py /app/services/native/acquisition/strategy.py
RUN python -m py_compile /app/repositories/slskd/slskd_repository.py /app/repositories/slskd/peer_scheduler.py /app/services/native/acquisition/scoring_core.py /app/services/native/acquisition/strategy.py
COPY assets /app/static-template/fonts/peer-ui
COPY typography.css /app/static-template/typography.css
RUN python -c "from pathlib import Path; p=Path('/app/static-template/index.html'); s=p.read_text(); p.write_text(s.replace('</head>', '<link rel=\"stylesheet\" href=\"/__DROPPEDNEEDLE_BASE__/typography.css\" /></head>', 1))"
