# Runs the Django/DRF API and, with a different CMD (see docker-compose.yml),
# the Celery worker and beat scheduler. Far lighter than masterflow's image:
# no Node, no Playwright, no WeasyPrint — this service only talks HTTP to
# Deepgram and Gemini and writes to Postgres.
FROM python:3.14-slim

# build-essential/libffi-dev/libpq5 cover psycopg2-binary's runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
RUN chmod +x /app/backend/docker-entrypoint.sh

# Uploaded audio spools here between the upload request and the transcribe
# task, then is deleted. Created explicitly so the directory exists even
# when no volume is mounted (e.g. `manage.py` one-shots).
RUN mkdir -p /app/backend/spool

WORKDIR /app/backend

EXPOSE 8000

# docker-compose.yml sets `entrypoint: docker-entrypoint.sh` (runs
# migrations) only for the `backend` service — containers racing migrate
# on startup is exactly what a multi-container compose-up shouldn't do.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
