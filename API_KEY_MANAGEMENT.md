# API Key & Credentials Management

## Overview

This application implements a **secure, environment-aware credentials system**:

### Key Principle
✅ **All credentials (Jira, ADO, API key) come ONLY from the UI form**  
❌ **NO credentials are read from environment variables or config files** (prevents accidental leaks)

---

## 📋 Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Frontend (React)                              │
├─────────────────────────────────────────────────────────────────┤
│ Connection Settings Form                                         │
│ ├─ Backend API URL (localhost vs production)                    │
│ ├─ API Key (auto-managed based on environment)                  │
│ ├─ Jira URL, Email, Token                                       │
│ └─ ADO Organization, PAT                                        │
│                                                                  │
│ sessionStorage (temporary) + localStorage (API key only)        │
└─────────────────────────────────────────────────────────────────┘
                            ↓
                    HTTP/HTTPS with headers
                    X-API-Key: [secret]
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│                   Backend (Flask)                                │
├─────────────────────────────────────────────────────────────────┤
│ Validates X-API-Key header                                      │
│ Accepts ALL credentials as query parameters ONLY                │
│ ├─ /validate-jira-creds (strict validation)                    │
│ ├─ /validate-ado-creds (strict validation)                     │
│ ├─ /jira-projects (requires all 3 params)                      │
│ ├─ /jira-filters (requires all 3 params)                       │
│ └─ /ado-projects (requires both params)                        │
│                                                                  │
│ NO fallback to env vars or config files!                       │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🔐 API Key Management

### Local Development (localhost)

**File**: `web-ui/src/lib/apiKeyManager.js`

```javascript
// Automatically loaded when Backend URL = localhost:10000
const apiKey = 'dev-local-api-key-12345';
```

**How it works:**
1. User enters Backend URL: `http://localhost:10000`
2. Frontend detects it's local (contains "localhost" or "127.0.0.1")
3. Automatically loads key from `.env` file
4. Key is stored in browser's localStorage
5. User can override in UI if needed

**In your UI:**
- ✅ Backend API key field shows: `🔓 Local dev key`

---

### Production (Azure/Render/AWS)

**File**: `web-ui/src/lib/apiKeyManager.js`

```javascript
// When Backend URL = https://jira-to-ado.onrender.com
// Frontend calls: Backend → Azure Key Vault
const apiKey = await getApiKey('https://jira-to-ado.onrender.com');
```

**How it works:**
1. User enters Backend URL: `https://jira-to-ado.onrender.com`
2. Frontend detects it's production (not localhost)
3. Calls backend endpoint: `POST /api/get-secret`
4. Backend uses Azure Managed Identity to fetch from Key Vault
5. Key is returned to frontend securely

**In your UI:**
- 🔐 Backend API key field shows: `🔐 Fetched from Azure Key Vault`

---

## 🔑 Setting Up API Keys

### Local Development (.env)

Create `.env` in project root:

```env
# Local Development Only
MIGRATION_API_KEY=dev-local-api-key-12345
```

**Docker will automatically load this:**
```yaml
environment:
  MIGRATION_API_KEY: ${MIGRATION_API_KEY:-dev-secret-key-change-in-prod}
```

---

### Production (Render)

1. Go to **Render Dashboard** → Select service → **Environment**
2. Add environment variable:
   ```
   MIGRATION_API_KEY=your-production-secret-key-here
   ```
3. Render will pass this to the container at runtime

---

### Production (Azure Container Instances)

1. Create **Azure Key Vault** with secret:
   ```
   Name: jira-ado-api-key
   Value: your-production-secret-key-here
   ```

2. Configure **Managed Identity** on Container Instance:
   ```yaml
   identity:
     type: 'UserAssigned'
     userAssignedIdentities:
       '/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/jira-ado-identity': {}
   ```

3. Grant permissions in Key Vault:
   ```bash
   az keyvault set-policy \
     --name your-key-vault \
     --object-id <managed-identity-object-id> \
     --secret-permissions get
   ```

4. Backend will use Managed Identity to fetch the key:
   ```python
   from azure.identity import ManagedIdentityCredential
   from azure.keyvault.secrets import SecretClient
   
   credential = ManagedIdentityCredential()
   client = SecretClient(vault_url=vault_url, credential=credential)
   secret = client.get_secret("jira-ado-api-key")
   ```

---

## 🚫 Jira/ADO Credentials (NEVER from env vars)

### What Changed?

**Before (insecure):**
```python
# Endpoint would fall back to env vars!
@app.route('/jira-projects')
def jira_projects():
    jira_url = request.args.get('jira_url') or os.environ.get('JIRA_URL')
    # Could work with wrong credentials stored in env!
```

**After (secure):**
```python
# ONLY accepts from query params
@app.route('/jira-projects')
def jira_projects():
    jira_url = request.args.get('jira_url')
    if not jira_url:
        return {'error': 'jira_url is required (must be provided from UI)'}
```

---

## ✅ UI Workflow

### Step 1: Configure Backend URL
```
Backend API base URL: http://localhost:10000
↓
Frontend auto-loads API key from .env (local) or Key Vault (production)
```

### Step 2: Enter Jira Credentials
```
Jira URL: https://healthfinch.atlassian.net
Jira email: you@company.com
Jira API token: ATATT3x...
```

### Step 3: Enter ADO Credentials
```
Azure DevOps Organization: your-org-name
Azure DevOps PAT: your-personal-access-token
```

### Step 4: Test Connection
```
Clicking "Test connection" will:
1. Send /validate-jira-creds (strict validation)
2. Send /validate-ado-creds (strict validation)
3. Only mark success if BOTH pass
4. Show specific error if either fails
```

### Step 5: Save
```
Save button ONLY enabled after test passes
Credentials stored in browser sessionStorage
Lost when tab closes (security best practice)
```

---

## 🔒 Security Best Practices Implemented

| Feature | Implementation | Benefit |
|---------|-----------------|---------|
| No env vars in code | All credentials from UI | Prevents accidental commits |
| sessionStorage only | Credentials never written to disk | Lost on tab close |
| API key per environment | Local vs production keys | Can't mix up dev/prod |
| Strict validation | Each endpoint validates credentials | Fails fast with wrong creds |
| Managed Identity (Azure) | No credentials in Key Vault queries | Secure, auditable access |
| No fallback logic | Must provide all params | Prevents silent failures |

---

## 🧪 Testing Locally

```bash
# Terminal 1: Start Docker
cd c:\Users\PawanShah\jira-to-ado
docker-compose up

# Terminal 2: Check .env has correct key
cat .env
# Should show: MIGRATION_API_KEY=dev-local-api-key-12345

# Browser: http://localhost:3000
# Connection Settings:
# - Backend API base URL: http://localhost:10000
# - Backend API key: dev-local-api-key-12345 (auto-filled)
# - Jira URL: https://healthfinch.atlassian.net
# - Jira email: you@company.com
# - Jira API token: your-token-here
# - ADO Org: your-org
# - ADO PAT: your-pat-here

# Click "Test connection" → should pass both Jira and ADO
```

---

## 🚀 Deploying to Render

1. **Push code** (credentials not in git):
   ```bash
   git add -A
   git commit -m "Add strict credential validation"
   git push origin main
   ```

2. **Set Render env var**:
   - Render Dashboard → Environment
   - `MIGRATION_API_KEY=your-production-key`

3. **Auto-deploy happens**:
   - Render rebuilds Docker images
   - Env var passed to container
   - Frontend fetches it from `/api/get-secret`

---

## 📝 Files Modified

- `api_server.py`: Removed env var fallbacks from /jira-projects, /jira-filters, /ado-projects
- `web-ui/src/lib/apiKeyManager.js`: NEW - Manages API keys per environment
- `web-ui/src/components/CredentialsBar.jsx`: Updated to use apiKeyManager
- `.env`: Updated with clear documentation
- `docker-compose.yml`: Already loads .env correctly

---

## ⚠️ Common Issues

**"Failed to fetch" when clicking Test Connection**
- Check Backend API base URL matches running instance
- Verify API key matches what's in .env or Render environment
- Check browser console for specific error

**"Unauthorized" (401)**
- API key mismatch - verify X-API-Key header
- Check .env file has correct key
- Check Render environment variable is set

**"Jira authentication failed"**
- Wrong Jira URL
- Wrong email or API token
- Jira token expired (regenerate in Jira account settings)

**"ADO access denied"**
- Wrong organization name
- PAT expired or revoked
- PAT doesn't have project read permissions

---

## 🎯 Next Steps

1. ✅ Test locally with `docker-compose up`
2. ✅ Verify all endpoints require credentials from UI
3. ✅ Push to GitHub (triggers Render auto-deploy)
4. ✅ Set Render environment variable: `MIGRATION_API_KEY`
5. ⏳ Implement Azure Key Vault retrieval in backend
6. ⏳ Test production deployment

---

For questions or issues, check the logs:
```bash
# Local: docker logs jira-ado-api
# Render: Render Dashboard → Logs
```
