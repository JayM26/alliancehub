FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

RUN apt-get purge -y --auto-remove build-essential

COPY . /app/

RUN useradd -r -s /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser
