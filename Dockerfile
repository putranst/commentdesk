# BUMEN Intelligence - Docker-based deployment v20260818-3
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for instaloader
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app.py .
COPY bumen-logo.png .
COPY admin.html .
COPY data.json .
COPY bumen.db .

# Create database directory
RUN mkdir -p /data

# Expose port
EXPOSE 8080

# Run with database in persistent volume - FORCE REBUILD 20260823-2
ENV DB_PATH=/data/bumen.db
ENTRYPOINT ["sh", "-c", "echo '=== STARTUP v7 ===' && echo 'DB_PATH=' $DB_PATH && ls -la /data/ && echo 'Removing old DB...' && rm -f /data/bumen.db && echo 'Copying fresh DB...' && cp /app/bumen.db /data/bumen.db && echo 'DB copied' && ls -la /data/bumen.db && echo 'Starting Python...' && python3 app.py"]