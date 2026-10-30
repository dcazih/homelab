FROM node:22-alpine AS frontend
WORKDIR /build
COPY package*.json ./
RUN npm ci
COPY index.html vite.config.js ./
COPY public ./public
COPY src ./src
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd -r -u 10001 cassette && mkdir /data && chown cassette:cassette /data
COPY server.py ./
COPY LICENSE UPSTREAM-NOTICE UPSTREAM-COMMIT ./
COPY --from=frontend /build/dist ./dist
USER cassette
EXPOSE 8790
CMD ["uvicorn","server:app","--host","0.0.0.0","--port","8790","--no-access-log","--proxy-headers","--forwarded-allow-ips","127.0.0.1"]
