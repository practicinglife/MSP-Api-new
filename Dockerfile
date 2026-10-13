FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libfontconfig1 \
    libcairo2 libgdk-pixbuf-2.0-0 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# HTTPS only: the same runner as a local install. A local CA certificate (certs/) until Get started installs a
# Let's Encrypt or uploaded one (data/tls/); the wizard can then restart the app in place.
CMD ["python", "-m", "app.cli", "run", "--host", "0.0.0.0", "--port", "8000"]
