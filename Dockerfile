# Kuznitsa — self-hosted 3D-printer farm dashboard
# Minimal image: install deps, copy source, run the poller + web server.
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first so the layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source.
COPY . .

# Bind to all interfaces inside the container; the host/proxy decides exposure.
ENV WEB_HOST=0.0.0.0 \
    WEB_PORT=8000 \
    PYTHONUNBUFFERED=1 \
    # Behind a TLS-terminating reverse proxy set COOKIE_SECURE=1. For plain
    # HTTP on the LAN (no proxy) it must be 0 or auth cookies are dropped.
    COOKIE_SECURE=0

EXPOSE 8000

CMD ["python", "-u", "main.py"]
