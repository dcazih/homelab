FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY assets ./assets
COPY app.py dn_accounts.py edition_matching.py index.html album_request_bridge_v6.py metadata_resolver_v1.py metadata_recovery_v1.py peer_scheduler.py peer_scheduler_api.py shared_acquisition.py ./
RUN useradd -r -u 10001 service && mkdir /data && chown service:service /data
USER service
EXPOSE 8765
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8765","--proxy-headers","--forwarded-allow-ips","127.0.0.1"]
