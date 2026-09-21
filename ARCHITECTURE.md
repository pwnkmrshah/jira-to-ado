# Separated Backend & Frontend Architecture

This project now has **separate Docker containers** for backend and frontend:

## 📦 Deployment Architecture

### Backend Service (`jira-to-ado-api`)
- **Type**: Flask REST API (Python)
- **Docker Image**: `Dockerfile.backend`
- **Port**: 10000
- **URL**: https://jira-to-ado-api.onrender.com
- **Responsibilities**:
  - Jira/ADO credential validation
  - Migration job execution
  - Gap analysis
  - API endpoints only (no static files)

### Frontend Service (`jira-to-ado-web`)
- **Type**: React SPA + Nginx (Node.js + Alpine)
- **Docker Image**: `Dockerfile.frontend`
- **Port**: 3000
- **URL**: https://jira-to-ado.onrender.com
- **Responsibilities**:
  - React UI serving
  - Static asset caching
  - SPA routing via Nginx

## 🚀 Local Development

### Using Docker Compose
```bash
docker-compose up --build
```

This will:
- Build backend Flask API on `http://localhost:10000`
- Build frontend React + Nginx on `http://localhost:3000`
- Frontend automatically points to `http://api:10000` (Docker network)

Visit: **http://localhost:3000**

### Running Without Docker
Terminal 1 (Backend):
```bash
cd web-ui
npm install
npm run build
cd ..
pip install -r requirements.txt
python api_server.py
```

Terminal 2 (Frontend Dev):
```bash
cd web-ui
npm run dev
```

## 🌍 Render Deployment

### Two Separate Services on Render

**1. Backend Service**
- Service name: `jira-to-ado-api`
- Dockerfile: `Dockerfile.backend`
- Port: 10000
- Environment variables:
  - `MIGRATION_API_KEY` (secret)
  - `CORS_ALLOWED_ORIGINS`: `"*"` or specific domains
  - `FLASK_ENV`: `production`

**2. Frontend Service**
- Service name: `jira-to-ado-web`
- Dockerfile: `Dockerfile.frontend`
- Port: 3000
- Environment variables:
  - `REACT_APP_API_URL`: `https://jira-to-ado-api.onrender.com`

### Deployment Flow
1. Push to `main` branch
2. Render auto-builds both services from separate Dockerfiles
3. Backend starts first and listens on port 10000
4. Frontend starts, makes API calls to backend
5. Both services deployed and accessible

## 📝 Environment Variables

### Frontend (.env.production)
```
VITE_API_BASE_URL=https://jira-to-ado-api.onrender.com
```

### Backend
- `MIGRATION_API_KEY` - Required for API authentication
- `CORS_ALLOWED_ORIGINS` - CORS policy (use `*` for dev, specific domains for prod)
- `FLASK_ENV` - Set to `production`
- `PORT` - Should be `10000`

## ✅ Benefits of Separation

- **Independent Scaling**: Scale API or UI separately based on load
- **Separate Deployments**: Deploy frontend and backend on different schedules
- **Cleaner Architecture**: Clear separation of concerns
- **Better for Development**: Run and debug each service independently
- **Language Freedom**: Backend in Python, Frontend in Node.js/React
- **Easier Monitoring**: Monitor API and UI metrics separately

## 🐛 Troubleshooting

### Frontend shows "Frontend not deployed" error
- This is expected if you're using the old combined setup
- Make sure both services are running:
  - Backend on port 10000
  - Frontend on port 3000

### CORS errors
- Check `CORS_ALLOWED_ORIGINS` environment variable
- For dev: set to `*`
- For prod: set to frontend domain (e.g., `https://jira-to-ado.onrender.com`)

### API calls failing
- Verify backend service is running and accessible
- Check `VITE_API_BASE_URL` in frontend matches backend URL
- Check `MIGRATION_API_KEY` is set correctly
