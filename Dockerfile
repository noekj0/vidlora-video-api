# Python API + FFmpeg + Node.js 22 (required by yt-dlp's current YouTube EJS support)
FROM node:22-bookworm-slim AS node
FROM python:3.12-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py worker.py ./
RUN useradd --uid 10001 -m vidlora && mkdir /home/vidlora/tmp && chown -R vidlora:vidlora /home/vidlora/tmp
USER vidlora
ENV TMPDIR=/home/vidlora/tmp
ENV PYTHONUNBUFFERED=1
EXPOSE 10000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --proxy-headers"]
