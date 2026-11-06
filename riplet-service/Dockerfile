FROM node:24-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/ytdlp \
 && /opt/ytdlp/bin/pip install --no-cache-dir --upgrade pip 'yt-dlp[default]'
ENV PATH="/opt/ytdlp/bin:${PATH}" NODE_ENV=production PYTHONDONTWRITEBYTECODE=1 HOME=/tmp PORT=8080
WORKDIR /app
COPY service ./service
COPY public ./public
USER node
EXPOSE 8080
CMD ["node", "service/server.mjs"]
