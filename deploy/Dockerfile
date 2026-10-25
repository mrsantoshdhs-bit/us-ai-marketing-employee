# Optional container image (not required for local use).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY aime ./aime
COPY config ./config
COPY scripts ./scripts
COPY .env.example ./.env.example
RUN useradd --create-home aime && mkdir -p /app/data && chown -R aime /app
USER aime
VOLUME ["/app/data"]
EXPOSE 8765
# The dashboard has no login of its own: publish this port only on 127.0.0.1 or behind an authenticating proxy.
CMD ["python", "-m", "uvicorn", "aime.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765"]
