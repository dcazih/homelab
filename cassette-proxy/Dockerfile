FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd -m -u 10001 appuser
RUN mkdir -p /data && chown 10001:10001 /data
COPY main.py personal.py actions.py virtual.py collection.py discovery.py cover_art.py artist_identity.py music_search_upgrade.py artist_enrichment_v5.py spelling_v5.py album_requests_v6.py placeholder_audio_v7.py album_pages_stage5.py saved_favorites.py unavailable-9m11s.mp3 ./
USER appuser
EXPOSE 8787
# Never log request URLs: OpenSubsonic sends auth tokens in query parameters.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8787", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
