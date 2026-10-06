FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY templates/ ./templates/

EXPOSE 5000

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/api/data')"

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--timeout", "30", "app.main:app"]
