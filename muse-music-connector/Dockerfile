FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chmod=644 server.py .
RUN useradd -r -u 10001 bridge && mkdir /data && chown bridge:bridge /data
USER bridge
EXPOSE 8788
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8788", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
