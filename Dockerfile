# Stage 1: Build React frontend
FROM node:18-alpine AS frontend-builder

WORKDIR /app/web-ui
COPY web-ui/package*.json ./
RUN npm install
COPY web-ui .
RUN npm run build

# Stage 2: Python Flask backend with frontend
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy Python requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY api_server.py .
COPY api/ ./api/
COPY utilities/ ./utilities/
COPY config/ ./config/
COPY jira_ado_copy/ ./jira_ado_copy/

# Copy built frontend from builder stage
COPY --from=frontend-builder /app/web-ui/dist ./web-ui/dist

# Set environment variables
ENV FLASK_ENV=production
ENV PORT=10000

# Expose port
EXPOSE 10000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:10000/').read()" || exit 1

# Run Flask app with gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:10000", "--workers", "2", "--timeout", "180", "api_server:app"]
