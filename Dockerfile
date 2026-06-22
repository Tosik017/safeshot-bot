# Свіжий Chromium закриває головний Critical (старий движок під --no-sandbox).
# Версія pip playwright (requirements.txt) мусить точно збігатися з цим тегом.
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

RUN apt-get update \
 && apt-get install -y --no-install-recommends dumb-init \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Браузери вже в образі v1.60.0 — playwright install НЕ запускаємо.
COPY --chown=pwuser:pwuser requirements.txt .
# --require-hashes: requirements.txt — повний lock (uv pip compile --generate-hashes),
# build падає, якщо хоч один пакет (включно з транзитивними) прийде з іншим вмістом.
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY --chown=pwuser:pwuser . .

# Non-root: втеча рендера Chromium = непривілейований pwuser, а не root.
USER pwuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["dumb-init", "--"]
CMD ["python", "main.py"]

# Render користується власним healthCheckPath (render.yaml). Цей HEALTHCHECK —
# для docker-compose/локалі (PORT за замовчуванням 8000).
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ping',timeout=5).status==200 else 1)"
