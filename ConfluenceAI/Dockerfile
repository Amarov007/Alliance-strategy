FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/trades.db LOG_FILE=/data/logs/trading_bot.log \
    API_HOST=0.0.0.0 API_PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY web ./web

RUN useradd --create-home --uid 10001 bot && mkdir -p /data && chown -R bot /data /app
USER bot
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.getenv('API_PORT','8080')+'/api/health',timeout=4)" || exit 1

CMD ["python", "main.py"]
