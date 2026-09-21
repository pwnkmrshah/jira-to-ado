# Separated Backend & Frontend Architecture

This project has **separate Docker containers** for backend and frontend with production-grade containerization.

## 📦 Deployment Architecture

### Backend Service (`jira-ado-api`)
- **Type**: Flask REST API (Python 3.11)
- **Docker Image**: `Dockerfile.backend`
- **Port**: 10000
- **URL (Render)**: https://jira-to-ado-api.onrender.com
- **Server**: Gunicorn (production WSGI)
- **Responsibilities**:
  - Jira/ADO credential validation
  - Migration job execution
  - Gap analysis
  - API endpoints only (no static files)

### Frontend Service (`jira-ado-web`)
- **Type**: React SPA + Nginx (Node.js 18 + Alpine + Nginx)
- **Docker Image**: `Dockerfile.frontend`
- **Port**: 3000
- **URL (Render)**: https://jira-to-ado.onrender.com
- **Server**: Nginx (optimized for SPA)
- **Responsibilities**:
  - React UI serving
  - Static asset caching & gzip compression
  - SPA routing via Nginx
  - CORS handling

## 🚀 Local Development

### Prerequisites
- **Docker Desktop**: https://www.docker.com/products/docker-desktop
- **Verify installation**:
  ```powershell
  docker --version
  docker-compose --version
  ```

### Quick Start with Docker Compose

```powershell
# Development mode (with live reload)
docker-compose up --build

# Then visit: http://localhost:3000
# API available at: http://localhost:10000
# Health check: http://localhost:10000/health
```

### Build & Push to Registry (Azure ACR)

**PowerShell:**
```powershell
# Build both locally
.\build-docker.ps1 -Action all

# Build and push to Azure Container Registry
$env:REGISTRY = "myregistry.azurecr.io/"
.\build-docker.ps1 -Action all -Push

# Build specific version and push
.\build-docker.ps1 -Action all -Registry "myregistry.azurecr.io/" -Tag "v1.0.0" -Push
```

**Bash (Linux/Mac):**
```bash
# Build both
./build-docker.sh all

# Build and push
REGISTRY=myregistry.azurecr.io/ ./build-docker.sh all push

# With version tag
REGISTRY=myregistry.azurecr.io/ TAG=v1.0.0 ./build-docker.sh all push
```

### Running Without Docker

**Terminal 1 (Backend):**
```powershell
pip install -r requirements.txt
python api_server.py
# API runs on http://localhost:5001
```

**Terminal 2 (Frontend):**
```powershell
cd web-ui
npm install
npm run dev
# UI runs on http://localhost:5173
# Configure VITE_API_BASE_URL=http://localhost:5001
```

## 🌍 Production Deployment

### Render (Current Setup)
Two separate services auto-deploy on `git push`:
1. Backend from `Dockerfile.backend`
2. Frontend from `Dockerfile.frontend`

Each service rebuilds independently based on git changes.

### Azure Container Instances (Example)

```powershell
# 1. Create Azure Container Registry
az acr create --resource-group myRG --name myregistry --sku Basic

# 2. Login to ACR
az acr login --name myregistry

# 3. Build and push
REGISTRY=myregistry.azurecr.io/ .\build-docker.ps1 -Action all -Push

# 4. Deploy to Azure Container Instances
az container create `
  --resource-group myRG `
  --name jira-ado-api `
  --image myregistry.azurecr.io/jira-ado-api:latest `
  --registry-username <username> `
  --registry-password <password> `
  --ports 10000 `
  --environment-variables MIGRATION_API_KEY=your-key FLASK_ENV=production `
  --memory 1.5 `
  --cpu 1
```

### Production Docker Compose
```powershell
# For Azure or self-hosted Docker hosts
docker-compose -f docker-compose.prod.yml up -d

# Set required environment variables
$env:MIGRATION_API_KEY = "your-secret-key"
$env:VITE_API_BASE_URL = "https://your-api-domain.com"
docker-compose -f docker-compose.prod.yml up -d
```

## 🐳 Docker Features

### Health Checks
Both containers include health checks:
- **Backend**: `curl http://localhost:10000/health`
- **Frontend**: `wget http://localhost:3000/health`

Docker automatically restarts unhealthy containers.

### Layer Caching Optimization
- `Dockerfile.backend`: Installs deps first, copies code second → faster rebuilds
- `Dockerfile.frontend`: Multi-stage build → smaller final image

### Security
- Runs as non-root user (`appuser` for backend, `nginx` for frontend)
- No unnecessary packages
- Clean base images (slim Python, Alpine Nginx)

### Logging
Production setup includes:
- JSON logging format
- Max file size: 10MB
- Keep 3 rotated logs per service

## 📝 Environment Variables

### Frontend (.env files)
```bash
# .env.development
VITE_API_BASE_URL=http://localhost:5000

# .env.production
VITE_API_BASE_URL=https://jira-to-ado-api.onrender.com
```

### Backend
| Variable | Required | Default | Example |
|----------|----------|---------|---------|
| `MIGRATION_API_KEY` | ✅ Yes | - | `your-secret-key` |
| `FLASK_ENV` | ❌ No | `production` | `production` or `development` |
| `CORS_ALLOWED_ORIGINS` | ❌ No | `*` | `http://localhost:3000` |
| `PORT` | ❌ No | `10000` | `10000` |

## ✅ Benefits of Separation

- **Independent Scaling**: Scale API or UI separately based on load
- **Separate Deployments**: Deploy frontend and backend on different schedules
- **Cleaner Architecture**: Clear separation of concerns
- **Better Monitoring**: Monitor API and UI metrics separately
- **Language Freedom**: Use appropriate tech stack for each service
- **Easier Debugging**: Run services independently
- **Production Ready**: Gunicorn + Nginx production servers

## 🛠️ Development Workflow

### Local Development
```powershell
# 1. Make changes to code
# 2. Rebuild containers
docker-compose down
docker-compose up --build

# 3. Changes to backend Python code
#    - Volume mount watches changes
#    - Restart container or use Flask reload

# 4. Changes to frontend React code
#    - Volume mount watches changes
#    - Hot reload via Vite
```

### Deployment Workflow
```powershell
# 1. Test locally with docker-compose
docker-compose up --build

# 2. Commit and push to main
git add .
git commit -m "Your changes"
git push origin main

# 3. Render auto-deploys
#    - Detects Dockerfile changes
#    - Rebuilds and redeploys services
#    - Takes 5-10 minutes

# 4. Verify deployment
#    - Check Render dashboard
#    - Test API: https://jira-to-ado-api.onrender.com/health
#    - Test UI: https://jira-to-ado.onrender.com
```

## 🐛 Troubleshooting

### Frontend shows "API not responding"
```powershell
# Check backend is running
curl http://localhost:10000/health

# Check VITE_API_BASE_URL in frontend
docker logs jira-ado-web | grep VITE_API_BASE_URL

# Rebuild both
docker-compose down
docker-compose up --build
```

### CORS errors in browser console
- Backend: Check `CORS_ALLOWED_ORIGINS` matches frontend URL
- For dev: Use `CORS_ALLOWED_ORIGINS="*"`
- For prod: Use specific domain like `https://jira-to-ado.onrender.com`

### Container keeps restarting
```powershell
# Check container logs
docker logs jira-ado-api
docker logs jira-ado-web

# Check health status
docker ps  # Look at STATUS column
```

### Can't connect to API from frontend
- Verify both containers are running: `docker ps`
- Check API container logs: `docker logs jira-ado-api`
- Verify `VITE_API_BASE_URL` in frontend points to API service
- For local dev: Should be `http://api:10000` (Docker network)
- For remote: Should be full HTTPS URL

## 📖 Files Reference

| File | Purpose |
|------|---------|
| `Dockerfile.backend` | Production Flask API image |
| `Dockerfile.frontend` | Production React + Nginx image |
| `docker-compose.yml` | Local development (with volume mounts) |
| `docker-compose.prod.yml` | Production deployment |
| `nginx.conf` | Nginx configuration for SPA routing |
| `.dockerignore` | Exclude files from Docker build |
| `build-docker.ps1` | PowerShell build script |
| `build-docker.sh` | Bash build script |
