FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY exporter.py spec.example.yaml entrypoint.sh ./
RUN chmod +x entrypoint.sh
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
ENTRYPOINT ["/app/entrypoint.sh"]
