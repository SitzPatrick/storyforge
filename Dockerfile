FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1             PYTHONUNBUFFERED=1             PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update             && apt-get install -y --no-install-recommends ffmpeg             && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/pyproject.toml
COPY README.md /app/README.md
COPY LICENSE /app/LICENSE
COPY app /app/app
COPY storyforge /app/storyforge
COPY config /app/config
COPY .env.example /app/.env.example

RUN pip install --no-cache-dir .

CMD ["storyforge", "--help"]
