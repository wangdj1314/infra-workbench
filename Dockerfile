FROM python:3.12-slim

LABEL maintainer="wangdj"
LABEL description="基础架构工作台 - LDAP Auth + Team Management"

WORKDIR /app

# Install dependencies first (better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY app.py .
COPY static/ static/

# Create data directory for SQLite
RUN mkdir -p /app/data

EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')" || exit 1

CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:8080", "--timeout", "120", "--access-logfile", "-", "app:app"]
