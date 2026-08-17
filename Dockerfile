# BUMEN Intelligence - Docker-based deployment
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

# Run with database in persistent volume
ENV DB_PATH=/data/bumen.db
CMD ["sh", "-c", "if [ ! -f /data/bumen.db ]; then cp /app/bumen.db /data/bumen.db; fi && python3 app.py"]