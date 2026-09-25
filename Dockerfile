# Builds the Kingshot bot + self-hosted extension from this checkout (main).
# Deployed via the docker-compose.yaml at the repository root, which points its
# build context at this directory.
FROM python:3.12.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# curl/unzip: the bot's self-heal bootstrap + release handling.
# libgomp1/libgl1/libglib2.0-0: runtime libs for the OCR stack
# (onnxruntime / rapidocr / opencv / matplotlib) at import time.
RUN apt-get update && apt-get install --no-install-recommends -y \
        curl ca-certificates unzip \
        libgomp1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the bot (cogs/, extension/, fonts/, main.py, ...).
COPY . .

RUN chmod +x entrypoint.sh

ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
