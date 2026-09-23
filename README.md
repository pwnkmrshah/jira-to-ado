# Jira to ADO Copy

Complete migration suite for copying work items from Jira to Azure DevOps with validation, gap analysis, and verification.

**Three main tools:**
1. **`worker_jira_to_ado_copy.py`** — Migrate cards by filter ID or specific keys
2. **`migration_gap_analysis.py`** — Identify missing or misaligned cards after migration
3. **`verify_migration.py`** — Verify field-by-field accuracy of migrated items

**API bridge (Forge integration):**
- **`api_server.py`** — Flask HTTP server that wraps the three tools above. Used when the Jira Forge app (`jira-ado-migrator-forge-app`) triggers migrations from the browser UI instead of the CLI.

---

## 🚀 Quick Start (5 Minutes)

### Prerequisites

- **Docker & Docker Compose** (recommended) — Windows, Mac, or Linux
  - Download: https://www.docker.com/products/docker-desktop
  - Verify: `docker --version && docker-compose --version`

- **OR Python 3.11+** (if running without Docker)
  - Download: https://www.python.org/downloads/
  - Verify: `python3 --version`

- **Jira API Token** — Get from https://id.atlassian.com/manage-profile/security/api-tokens
- **ADO Personal Access Token** — Get from https://dev.azure.com/{org}/_usersSettings/tokens
  - Must include scopes: **Work Items (Read & Write)**, **Project & Team (Read)**
  - For field creation: **Edit process** permission at Organization level (Organization Settings → Permissions)

### Step 1: Clone & Setup

```bash
git clone https://github.com/health-catalyst/jira-to-ado.git
cd jira-to-ado
```

### Step 2: Create Environment File

Create a `.env` file in the project root with your credentials:

```bash
# .env
JIRA_INSTANCE=healthfinch
JIRA_EMAIL=your.email@healthcatalyst.com
JIRA_TOKEN=your-jira-api-token-here

ADO_ORG=healthcatalyst
ADO_PAT=your-ado-pat-here

MIGRATION_API_KEY=demo-key-change-me
GPT_API_KEY=your-azure-openai-key-for-ai-mapping
```

### Step 3: Start the Application

**Using Docker (Recommended):**
```bash
docker-compose up --build
```

**OR manually (without Docker):**
```bash
# Install dependencies
pip install -r requirements.txt
pip install -r jira_ado_copy/requirements.txt
pip install flask requests

# Start backend
python3 api_server.py

# In another terminal, start frontend
cd web-ui
npm install
npm run dev
```

### Step 4: Open Web UI

- **Backend API:** http://localhost:5001
- **Web UI:** http://localhost:3000

You should see:

```
Jira → Azure DevOps Migration

Connection Settings
──────────────────────────────────────────────
Backend API base URL    │ https://localhost:5001
API Key                 │ [demo-key-change-me]

Jira URL                │ https://healthfinch.atlassian.net
Jira Email              │ your.email@...
Jira API Token          │ [demo-key-change-me]

ADO Organization        │ healthcatalyst
ADO Personal Access Tok │ [your-ado-pat]
ADO Default Project     │ Embedded Refills Engineering

                        [Test Connection] [Save]
```

### Step 5: Test Connection

1. Fill in all credentials above
2. Click **[Test Connection]** — should show ✅ **Credentials validated**
3. Click **[Save]** — credentials stored in browser session
4. Navigate to **AI Migration** tab

### Step 6: Your First Migration

**Tab: AI Migration**

```
SOURCE
  Jira Board: [Select from dropdown] ← Shows all Jira projects

SCOPE
  ○ Entire board                       ← Start here!
  
TARGET
  ADO Project: [dropdown]              ← Shows all ADO projects

                    [🔍 Analyze]
```

1. Select a Jira board (small project recommended for first run)
2. Keep "Entire board" selected
3. Select target ADO project
4. Click **[Analyze]** — waits 30-60 seconds

**Analysis results show:**
```
✓ Issue Counts
  • Jira issues found: 256
  • Ready to migrate: 256

⚠️  Type Gaps (if any)
  • Jira Epic → [User Story] (AI suggestion, can change)

⚠️  State Gaps (if any)
  • Jira "Blocked" → [New] (AI suggestion, can change)

👥 User Gaps (if any)
  • user@example.com not found in ADO

📊 Custom Fields
  • Story Points (found & will map)
  • Release (will create if permission available)
```

5. Review mappings, adjust if needed
6. Click **[Proceed to Migration]** — creates work items in ADO

**Results shown in JobStatusPanel:**
```
Status: running
Output: [migration progress...]
```

Wait for status to change to `completed` (green ✅) or `warning` (yellow ⚠️).

---

## Installation & Configuration

### Full Prerequisites

**System:**
- Windows, Mac, or Linux
- 2GB+ RAM (for Docker)
- 500MB+ disk space (for dependencies + logs)

**Software:**
- Docker & Docker Compose (recommended) — https://www.docker.com/
- OR Python 3.11+ — https://www.python.org/
- Node.js 18+ (only if running frontend manually) — https://nodejs.org/

**Access Tokens (Required):**

1. **Jira API Token**
   - Go to: https://id.atlassian.com/manage-profile/security/api-tokens
   - Click **Create API token**
   - Copy the token (won't be shown again)
   - Use with your Jira email for authentication

2. **Azure DevOps Personal Access Token**
   - Go to: https://dev.azure.com/{org}/_usersSettings/tokens
   - Click **New Token**
   - Name: `jira-to-ado-migration`
   - Scopes:
     - ✅ **Work Items**: Read & Write
     - ✅ **Project & Team**: Read
     - ✅ **Process**: Read & Manage (optional, for field creation)
   - Expiration: 90 days minimum
   - Copy token (won't be shown again)

3. **Azure OpenAI API Key** (optional, for AI-driven mapping)
   - Used by `/analyze` endpoint to generate field/state mapping suggestions
   - Get from: https://portal.azure.com → OpenAI resource → Keys
   - Falls back to manual mapping if not provided

### Configuration Files

**config/jira_config.json:**
```json
{
  "server": "https://healthfinch.atlassian.net",
  "email": "your.email@healthcatalyst.com",
  "access_token": "your-jira-api-token"
}
```

**config/ado_config.json:**
```json
{
  "organization_url": "https://dev.azure.com/healthcatalyst",
  "username": "your.name@healthcatalyst.com",
  "access_token": "your-ado-pat",
  "project": "Embedded Refills Engineering"
}
```

**Environment variables (used by docker-compose):**
```bash
export JIRA_INSTANCE=healthfinch
export JIRA_EMAIL=your.email@healthcatalyst.com
export JIRA_TOKEN=your-jira-api-token
export ADO_ORG=healthcatalyst
export ADO_PAT=your-ado-pat
export MIGRATION_API_KEY=demo-key-change-me
export GPT_API_KEY=your-azure-openai-key
```

### Directory Structure After Setup

```
jira-to-ado/
├── config/
│   ├── jira_config.json              ← Jira credentials (create this)
│   ├── ado_config.json               ← ADO credentials (create this)
│   └── ...
├── jira_ado_copy/
│   ├── scripts/
│   │   ├── worker_jira_to_ado_copy.py ← Main migration script
│   │   ├── migration_gap_analysis.py  ← Gap finder script
│   │   ├── verify_migration.py        ← Verification script
│   │   └── ...
│   └── jira_ado_copy.yaml            ← Config template
├── utilities/
│   ├── utils_ado.py                  ← ADO API wrapper
│   ├── utils_jira.py                 ← Jira API wrapper
│   └── ...
├── web-ui/
│   ├── src/
│   │   ├── App.jsx                   ← Main React component
│   │   ├── tabs/
│   │   │   ├── AIMigrationTab.jsx    ← Migration tab
│   │   │   ├── GapAnalysisTab.jsx    ← Gap analysis tab
│   │   │   └── VerifyTab.jsx         ← Verification tab
│   │   └── ...
│   ├── package.json
│   └── vite.config.js
├── api_server.py                     ← Flask backend server
├── docker-compose.yml                ← Docker setup
├── .env                              ← Environment variables (create this)
└── README.md                         ← This file
```

---

## Architecture: Forge App ↔ Local Scripts

The Forge app runs in Atlassian's cloud (sandboxed Node.js). The Python scripts run on this local machine. They can't talk directly — `api_server.py` is the bridge.

```
User clicks "Migrate to ADO" in Jira (browser)
        │
        ▼
Forge frontend (Atlassian cloud iframe)
        │  invoke('migrate', { jiraFilter, adoProject })
        ▼
Forge backend resolver (Atlassian Lambda — src/index.js)
        │  POST https://<ngrok-id>.ngrok.io/migrate
        │  Header: X-API-Key: <MIGRATION_API_KEY>
        ▼
ngrok tunnel  (port-forwards to localhost:5001)
        │
        ▼
api_server.py  (Flask, localhost:5001)
        │  subprocess.run(['python3', 'worker_jira_to_ado_copy.py', ...])
        ▼
worker_jira_to_ado_copy.py
        ├── reads config/jira_config.json   → calls healthfinch.atlassian.net
        └── reads config/ado_config.json    → creates work items in ADO
```

**Everything runs locally.** ngrok is just a public HTTPS tunnel so Atlassian's servers can reach `localhost:5001`. The actual Jira reads and ADO writes happen from this machine using the credentials in `config/`.

---

## Docker Setup & Environment

### Using Docker Compose (Recommended)

**Build and run the complete stack:**

```bash
docker-compose up --build
```

This starts:
- Backend API server (`api_server.py`, port 5001)
- Frontend React UI (Vite, port 3000)
- Web-UI accessible at: http://localhost:3000

**Environment Variables (in docker-compose.yml):**

```yaml
environment:
  - JIRA_INSTANCE=healthfinch
  - JIRA_EMAIL=${JIRA_EMAIL}
  - JIRA_TOKEN=${JIRA_TOKEN}
  - ADO_ORG=healthcatalyst
  - ADO_PAT=${ADO_PAT}
  - MIGRATION_API_KEY=demo-key-change-me
  - GPT_API_KEY=${GPT_API_KEY}  # For AI-driven field mapping
```

**Set these before running:**

```bash
export JIRA_EMAIL="your.email@healthcatalyst.com"
export JIRA_TOKEN="your-jira-api-token"
export ADO_PAT="your-ado-personal-access-token"
export GPT_API_KEY="your-azure-openai-key"
```

Then:
```bash
docker-compose up --build
```

### Stopping Docker

```bash
docker-compose down
```

### View Logs

```bash
docker-compose logs -f api_server   # Backend
docker-compose logs -f web-ui       # Frontend
```

### Production Deployment

For production, use `docker-compose.prod.yml`:

```bash
docker-compose -f docker-compose.prod.yml up --build
```

---

## Running the API Server

### Prerequisites

```bash
pip install flask
# requests is already in requirements.txt
```

### Start the server

```bash
cd /home/pawan/ferret/repos/jira-to-ado
export MIGRATION_API_KEY=demo-key-change-me
python3 api_server.py
# → Running on http://127.0.0.1:5001
```

**ADO credentials are loaded automatically** from `config/ado_config.json` — no need to export `ADO_ORG` or `ADO_PAT` manually. The config file uses these keys:

```json
{
  "organization": "healthcatalyst",
  "access_token": "<your ADO PAT>",
  "username": "...",
  "project": "..."
}
```

> ⚠️ The fallback reads `organization` (not `organization_url`). If the key name differs, set `ADO_ORG` and `ADO_PAT` as environment variables instead and they will take priority.

### If port 5001 is already in use

```bash
kill $(lsof -ti:5001)
# then restart
```

### Verify endpoints are working

```bash
# Health (no auth)
curl http://localhost:5001/health

# ADO projects (requires API key)
curl -H "X-API-Key: demo-key-change-me" http://localhost:5001/ado-projects

# Connection ping (requires API key)
curl -H "X-API-Key: demo-key-change-me" http://localhost:5001/ping
```

---

## Web UI Flow & Tabs

The React frontend (http://localhost:3000) provides three integrated migration tabs with credential management.

### Connection Setup (Required First)

Before using any tab, fill in the **Credentials Bar** at the top:

```
┌─────────────────────────────────────────────────────────────┐
│ Connection Settings                                          │
├─────────────────────────────────────────────────────────────┤
│ Backend API base URL    │ https://localhost:5001             │
│ API Key                 │ [demo-key-change-me]               │
│                                                               │
│ Jira URL                │ https://healthfinch.atlassian.net   │
│ Jira Email              │ your.email@healthcatalyst.com       │
│ Jira API Token          │ [your-jira-api-token]               │
│                                                               │
│ ADO Organization        │ healthcatalyst                      │
│ ADO Personal Access Tok │ [your-ado-pat]                      │
│ ADO Default Project     │ Embedded Refills Engineering        │
│                         [Test Connection] [Save]             │
└─────────────────────────────────────────────────────────────┘
```

**What gets populated where:**
- **Jira URL + Email + Token** → Used for `/analyze` and `/migrate` endpoints (Jira API calls)
- **ADO Org + PAT** → Used for ADO REST API calls (field discovery, work item creation)
- **API Key** → Authentication for all `/analyze`, `/migrate`, `/gaps`, `/verify` endpoints
- **ADO Default Project** → Cached as default selection in tabs (can be overridden per-tab)

**Credentials are stored in:**
- Browser session storage only (cleared when tab closes)
- Never written to disk
- Never sent to backend (used by frontend to populate dropdowns locally when possible)

---

### Tab 1: AI Migration (Source → Analysis → Migrate)

**Purpose:** Migrate Jira issues to ADO with AI-driven field/state mapping.

**Workflow:**

```
┌──────────────────────────────┐
│  1. SELECT SOURCE            │
├──────────────────────────────┤
│  Jira Board (dropdown)   │   │ ← Fetched from /analyze endpoint
│                          │   │   (calls Jira /rest/api/3/projects)
└──────────────────────────────┘
                │
                ▼
┌──────────────────────────────┐
│  2. CHOOSE SCOPE             │
├──────────────────────────────┤
│  ○ Entire board              │
│  ○ Jira saved filter    [ID] │ ← Enter filter ID (numeric)
│  ○ Specific issues     [CSV] │ ← Enter issue keys comma-separated
└──────────────────────────────┘
                │
                ▼
┌──────────────────────────────┐
│  3. SELECT TARGET            │
├──────────────────────────────┤
│  ADO Project (dropdown)  │   │ ← Fetched from /analyze endpoint
│                          │   │   (calls ADO /projects REST API)
└──────────────────────────────┘
                │
                ▼
        [🔍 Analyze]
                │
                ▼
┌──────────────────────────────────────────┐
│  4. REVIEW ANALYSIS RESULTS              │
├──────────────────────────────────────────┤
│  ✓ Issue Counts                          │
│    • Total Jira issues: 1,256            │
│    • Issues to migrate: 999              │
│                                          │
│  ⚠️  Type Gaps                           │ ← Work item types not in ADO
│    • Jira Epic → No ADO match [dropdown] │ ← AI suggests mapping
│    • Jira Task → User Story [dropdown]   │   (editable, user can override)
│                                          │
│  ⚠️  State Gaps                          │ ← Statuses not in ADO
│    • Jira "Blocked" → [dropdown]         │   (editable)
│                                          │
│  👥 User Gaps                            │ ← Users not found in ADO
│    • swapnali.patil@healthcatalyst.com   │   (ACTION NEEDED)
│    • vikranth.ceakala@healthcatalyst.com │
│                                          │
│  📊 Custom Fields                        │ ← Extra fields in Jira
│    • Story Points (found)                │
│    • Reach (RICE) (not found)            │
│    • Score (RICE) (not found)            │
│                                          │
│  🔗 Attachments: 245 total               │
│  💬 Comments: 523 total                  │
└──────────────────────────────────────────┘
                │
                ▼
        [🚀 Proceed to Migration]
                │
                ▼
        Backend creates work items
        with selected type/state mappings
```

**Dropdowns explained:**
- **Jira Board** → Populated by calling backend `/analyze` → Jira `/rest/api/3/projects`
- **ADO Project** → Populated by backend `/analyze` → ADO `/_apis/projects` REST API
- **Type Mappings** → AI generated (GPT) based on issue type analysis, user can override
- **State Mappings** → AI generated (GPT) based on status analysis, user can override

**Expected outputs from /analyze:**
```json
{
  "type_mappings": [
    { "jira": "Epic", "ado": "Epic", "confidence": 0.95 },
    { "jira": "Story", "ado": "User Story", "confidence": 0.98 }
  ],
  "state_mappings": [
    { "jira": "To Do", "ado": "New", "confidence": 0.90 },
    { "jira": "In Progress", "ado": "Active", "confidence": 0.95 }
  ],
  "type_gaps": [ { "jira_type": "Incident" } ],
  "state_gaps": [ { "jira_state": "Blocked" } ],
  "user_gaps": [
    { "jira_user": "user@example.com", "found_in_ado": false }
  ],
  "custom_fields": [
    { "name": "Story Points", "type": "number" },
    { "name": "Reach (RICE)", "type": "string" }
  ],
  "issue_count": 256,
  "attachment_count": 245,
  "comment_count": 523,
  "jql_used": "project = PROJ ORDER BY created DESC"
}
```

**After migration:**
- Job status shown in **JobStatusPanel** (running → completed/warning/failed)
- Migration log output displayed (last 8000 chars of stdout)
- Errors shown in red (last 3000 chars of stderr)
- Exit code shown (0 = success, non-zero = error)

---

### Tab 2: Gap Analysis (Find Missing Cards)

**Purpose:** Identify cards in Jira that weren't migrated to ADO or are in wrong area paths.

**Workflow:**

```
┌──────────────────────────────┐
│  1. SELECT SOURCE            │
├──────────────────────────────┤
│  ○ Jira Filter ID      [ID]  │ ← Enter saved filter ID
│  ○ Entire Jira Board  [KEY] │ ← Enter Jira project key (e.g., SUST)
└──────────────────────────────┘
                │
                ▼
┌──────────────────────────────┐
│  2. ADO TARGET               │
├──────────────────────────────┤
│  ADO Project      │ Embedded │ ← Text input (matches against existing)
│  ADO Team/Board   │ Team 1   │ ← Team name in ADO
└──────────────────────────────┘
                │
                ▼
        [Run Gap Analysis]
                │
                ▼
┌──────────────────────────────────────┐
│  RESULTS                             │
├──────────────────────────────────────┤
│  ✅  Correctly migrated:     130 (85%)│
│  ❌  Not found anywhere:      12 (8%) │ ← NEEDS MIGRATION
│  ⚠️   Wrong area path:         3 (2%) │ ← NEEDS MANUAL MOVE
│                                      │
│  CSV report: gaps_2026-09-23.csv    │
│  (can be downloaded for remediation)│
└──────────────────────────────────────┘
```

**Expected CSV output:**
```
JiraKey,ExistsInADO,ADOId,AreaPath,MissingAreaPath,NeedsMigration,Notes
SUST-100,Yes,12345,/Team 1,,No,Correctly migrated
SUST-101,Yes,12346,,Yes,Yes,Missing area path
SUST-102,No,,,,Yes,Not migrated to ADO
SUST-103,Yes,12347,/Team 2,,Yes,Wrong area path - should be /Team 1
```

---

### Tab 3: Verify Migration (Validate Field Accuracy)

**Purpose:** Compare Jira cards field-by-field against migrated ADO work items.

**Workflow:**

```
┌──────────────────────────────┐
│  1. SELECT SOURCE MODE       │
├──────────────────────────────┤
│  ○ By Project Key      [KEY] │ ← Verify ALL cards in project
│  ○ Entire Jira Board   [KEY] │ ← Same as above
│  ○ By Specific Keys   [CSV] │ ← Verify specific cards
│  ○ By Jira Filter ID   [ID] │ ← Verify cards matching filter
└──────────────────────────────┘
                │
                ▼
┌──────────────────────────────┐
│  2. ADO TARGET               │
├──────────────────────────────┤
│  ADO Project    │ Embedded   │ ← Text input
└──────────────────────────────┘
                │
                ▼
        [Run Verification]
                │
                ▼
┌──────────────────────────────────────────┐
│  VERIFICATION SUMMARY                    │
├──────────────────────────────────────────┤
│  Total tickets    : 256                  │
│  Found in ADO     : 250                  │
│  Missing          : 6                    │
│  Has failures     : 15                   │
│  Pass rate        : 94%                  │
│                                          │
│  ⚠️  Failures detected (15 cards)       │
│                                          │
│  HTML report: verify_2026-09-23.html    │
│  (detailed field-by-field comparison)   │
└──────────────────────────────────────────┘
```

**HTML report includes:**
- Per-card verification status (✅ PASS / ⚠️ WARNING / ❌ FAIL)
- Field-by-field comparison for each card:
  - Title (exact match check)
  - Description (length/content check)
  - State mapping validation
  - Assignee presence check
  - Attachment count comparison (Jira vs ADO)
  - Comment count comparison
  - Link count comparison
- Attachment gap detection (which files failed to upload)
- Summary statistics

---

### How Dropdowns Get Populated

| Dropdown | Tab | API Call | Result |
|----------|-----|----------|--------|
| **Jira Board** | AI Migration | `/analyze` → Jira `/rest/api/3/projects` | List of Jira projects with id, name, key |
| **ADO Project** | AI Migration | `/analyze` → ADO `/_apis/projects` | List of ADO projects with id, name |
| **Type Mappings** | AI Migration | `/analyze` → GPT Responses API | AI-generated suggestions (Jira type → ADO type) |
| **State Mappings** | AI Migration | `/analyze` → GPT Responses API | AI-generated suggestions (Jira status → ADO state) |

**Note:** Gap Analysis & Verify tabs use **text input** (not dropdowns) because:
- ADO Team/Board name varies per installation
- Jira Project Key is static (e.g., SUST, OP, TM)
- ADO Project name is static (e.g., "Embedded Refills Engineering")

---

### Error Handling in UI

Each tab displays errors in two places:

1. **Connection Error** (top of page) — If credentials invalid
   ```
   Could not fetch Jira projects. Check your connection settings.
   ```

2. **Job Error** (JobStatusPanel) — If migration/analysis fails
   ```
   Migration failed: Backend returned 500 Internal Server Error
   Error details: [last 3000 chars of stderr]
   ```

**Common fixes:**
- Verify credentials are saved (green ✅ in credentials bar)
- Check backend is running (`docker-compose ps`)
- Review logs: `docker-compose logs -f api_server`

### API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | None | Server liveness check |
| `POST` | `/migrate` | X-API-Key | Start a migration job |
| `GET` | `/status/<job_id>` | X-API-Key | Poll job status and output |
| `POST` | `/gaps` | X-API-Key | Run gap analysis |
| `POST` | `/verify` | X-API-Key | Run verification |

#### POST /migrate — Body

```json
{
  "jira_instance": "healthfinch",
  "ado_project": "Embedded Refills Engineering",
  "jira_filter": "11657",
  "jira_keys": "",
  "skip_attachments": false
}
```

Provide `jira_filter` **or** `jira_keys`, not both. Returns `{ job_id, status: "queued" }` immediately (HTTP 202). The migration runs in the background.

#### GET /status/<job_id> — Response

```json
{
  "status": "running",
  "command": "python3 ... --jira-filter 11657",
  "output": "...(last 8000 chars of stdout)...",
  "error": "...(last 3000 chars of stderr)...",
  "return_code": null,
  "started_at": "2026-08-09T10:00:00Z",
  "finished_at": null
}
```

`status` cycles through: `queued` → `running` → `completed` | `failed`

#### POST /gaps — Body

```json
{
  "jira_instance": "healthfinch",
  "jira_filter": "11657",
  "ado_board": "Operations",
  "ado_project": "Embedded Refills Engineering"
}
```

Returns `{ job_id, report: "/abs/path/to/gaps_<ts>.csv" }`. CSV is saved locally in `reports/`.

#### POST /verify — Body

```json
{
  "jira_instance": "healthfinch",
  "ado_project": "Embedded Refills Engineering",
  "project_key": "OP",
  "jira_keys": ""
}
```

Returns `{ job_id, report: "/abs/path/to/verify_<ts>.html" }`. HTML report saved in `reports/`.

### Security Notes

- `MIGRATION_API_KEY` must match on both sides (Flask server env var + Forge app env var)
- The `/health` endpoint has no auth (needed for Forge to verify reachability)
- ngrok free tier generates a new URL each restart — update manifest egress when it changes
- For production: deploy `api_server.py` to a stable host (EC2, Railway, Render) and use a fixed URL

---

## Setup

### Access Tokens

- **Jira API token**: https://id.atlassian.com/manage-profile/security/api-tokens
- **ADO Personal Access Token**: https://dev.azure.com/healthcatalyst/_usersSettings/tokens

Store in `config/jira_config.json` and `config/ado_config.json` (see Configuration section).

## Tool 1: Migration Worker (`worker_jira_to_ado_copy.py`)

**Purpose:** Migrate work items from Jira to ADO using a Jira filter or specific card keys.

### Quick Start

**Migrate by Jira filter ID (all matching cards):**
```bash
python3 jira_ado_copy/scripts/worker_jira_to_ado_copy.py \
  --jira-instance healthfinch \
  --jira-filter YOUR-FILTER-ID \
  --ado-project "Embedded Refills Engineering"
```

**Migrate specific Jira keys:**
```bash
python3 jira_ado_copy/scripts/worker_jira_to_ado_copy.py \
  --jira-instance healthfinch \
  --jira-keys "YOUR-CARD-ID,YOUR-CARD-ID-2" \
  --ado-project "Embedded Refills Engineering"
```

**Interactive mode (prompts for missing arguments):**
```bash
python3 jira_ado_copy/scripts/worker_jira_to_ado_copy.py
```

### Arguments

| Argument | Description |
|---|---|
| `--jira-instance` | Jira instance name (subdomain of `https://<instance>.atlassian.net`) |
| `--jira-filter` | Jira filter ID that returns the issues to copy (e.g., `YOUR-FILTER-ID`) |
| `--jira-keys` | Comma-separated list of specific Jira keys to migrate (e.g., `YOUR-CARD-ID,YOUR-CARD-ID-2`) |
| `--ado-project` | Target Azure DevOps project name |
| `--skip-attachments` | Skip attachment upload (useful for debugging large-file timeouts) |

### What Gets Copied

For each card:
- **Title** (prefixed with Jira key, e.g., `[OP-1480] Feature Name`)
- **Description** (HTML, with embedded image references updated)
- **Assignee & Reporter** (with graceful fallback to description if identity unknown)
- **State** (mapped via `state_config.json`)
- **Work item type** (mapped via `type_config.json`)
- **Custom fields** (mapped via `custom_fields_config.json`)
- **Priority, Due Date, Labels** (as tags)
- **Attachments** (deduplicated by filename, with 3-attempt retry for transient errors)
- **Comments** (with image references updated)
- **Pull request links** (as hyperlinks)
- **Linked issues** (as hyperlinks back to Jira)
- **Jira key tag** (`jiraKey=OP-1480`) for traceability

### Logging

Output is written to `worker_jira_to_ado_copy.log`.

---

## Tool 2: Migration Gap Analysis (`migration_gap_analysis.py`)

**Purpose:** Identify cards in Jira that were not migrated to ADO, or migrated to the wrong area path.

### Quick Start

**Analyze a Jira filter against an ADO board:**
```bash
python3 jira_ado_copy/scripts/migration_gap_analysis.py \
  --jira-instance healthfinch \
  --jira-filter YOUR-FILTER-ID \
  --ado-board "Operations" \
  --ado-project "Embedded Refills Engineering" \
  --csv data_migration_gap_report.csv 2>&1 | tail -50
```

### Arguments

| Argument | Description |
|---|---|
| `--jira-instance` | Jira instance name |
| `--jira-filter` | Jira filter ID to compare (source of truth, e.g., `YOUR-FILTER-ID`) |
| `--ado-board` | ADO board name to check against (e.g., `YOUR-BOARD-NAME`) |
| `--ado-project` | Target Azure DevOps project name |
| `--csv` | Output file path for problem cards (CSV format) |

### Output Classification

Each card is classified into one of **four categories**:

1. **`correct`** — Migrated and in the correct area path
2. **`wrong_area`** — Migrated but in wrong area path (needs manual move)
3. **`no_area_path`** — Migrated but missing area path assignment
4. **`missed`** — Not in ADO at all (needs full re-migration)

### CSV Report

The CSV includes only **problem cards** (not all):

```csv
JiraKey,ExistsInADO,ADOId,AreaPath,MissingAreaPath,NeedsMigration,Notes
YOUR-CARD-ID,Yes,12345,/YOUR-AREA-PATH,,No,"Description of issue"
YOUR-CARD-ID-2,Yes,12346,,,Yes,"Migrated but area path not set"
YOUR-CARD-ID-3,No,,,Yes,Yes,"Not migrated to ADO yet"
```

**Example insights:**
- **Total** cards in Jira filter
- **Correct** (in correct area path)
- **Wrong area** (migrated but to different area path)
- **Missed** (not migrated to ADO)

---

## Tool 3: Verification (`verify_migration.py`)

**Purpose:** Verify field-by-field accuracy of migrated cards. Detect missing attachments, mismatched descriptions, etc.

### Quick Start

**Verify all cards in a Jira project:**
```bash
python3 jira_ado_copy/scripts/verify_migration.py \
  --jira-instance healthfinch \
  --project-key YOUR-PROJECT-KEY \
  --ado-project "Embedded Refills Engineering"
```

**Verify specific cards:**
```bash
python3 jira_ado_copy/scripts/verify_migration.py \
  --jira-instance healthfinch \
  --jira-keys "YOUR-CARD-ID,YOUR-CARD-ID-2,YOUR-CARD-ID-3" \
  --ado-project "Embedded Refills Engineering"
```

**Verify with HTML report output:**
```bash
python3 jira_ado_copy/scripts/verify_migration.py \
  --jira-instance healthfinch \
  --jira-keys "YOUR-CARD-ID,YOUR-CARD-ID-2,YOUR-CARD-ID-3" \
  --ado-project "Embedded Refills Engineering" \
  --html verification_report.html
```

### Arguments

| Argument | Description |
|---|---|
| `--jira-instance` | Jira instance name |
| `--project-key` | Jira project key (e.g., `YOUR-PROJECT-KEY`) — verifies ALL cards in project |
| `--jira-keys` | Comma-separated Jira keys to verify (e.g., `YOUR-CARD-ID,YOUR-CARD-ID-2`) |
| `--ado-project` | Target Azure DevOps project name |
| `--html` | Output file path for HTML report |

### Report Contents

- **Pre-flight card count** — How many Jira cards, how many in ADO
- **Per-field diff reporting** — Side-by-side comparison of Jira vs ADO for:
  - Title, Description, State, Assignee, Due Date
  - Attachment counts (e.g., "Jira: 23, ADO: 22 ⚠️ MISSING 1")
  - Comment counts
  - Link counts
- **Attachment gap detection** — Lists which files failed to migrate
- **HTML report** — Interactive report with pass/fail summary

---

## Complete Migration Workflow

### Step 1: Identify gaps before migration

```bash
# Analyze which cards in Jira filter are not yet in ADO
python3 jira_ado_copy/scripts/migration_gap_analysis.py \
  --jira-instance healthfinch \
  --jira-filter YOUR-FILTER-ID \
  --ado-board "Operations" \
  --ado-project "Embedded Refills Engineering" \
  --csv gaps.csv
```

**Output:** `gaps.csv` shows missed cards + remediation command

### Step 2: Run migration for missed cards

```bash
# From the CSV, extract jira keys that need migration
# and run worker script with those keys
python3 jira_ado_copy/scripts/worker_jira_to_ado_copy.py \
  --jira-instance healthfinch \
  --jira-keys "YOUR-CARD-ID,YOUR-CARD-ID-2,YOUR-CARD-ID-3" \
  --ado-project "Embedded Refills Engineering"
```

### Step 3: Verify success

```bash
# After migration completes, verify the cards
python3 jira_ado_copy/scripts/verify_migration.py \
  --jira-instance healthfinch \
  --jira-keys "YOUR-CARD-ID,YOUR-CARD-ID-2,YOUR-CARD-ID-3" \
  --ado-project "Embedded Refills Engineering"
```

If attachment counts don't match, the worker script's 3-attempt retry already ran; check `worker_jira_to_ado_copy.log` for details on which files failed after 3 retries.

### Step 4: Full validation (post-migration)

```bash
# Re-run gap analysis to confirm all cards migrated
python3 jira_ado_copy/scripts/migration_gap_analysis.py \
  --jira-instance healthfinch \
  --jira-filter YOUR-FILTER-ID \
  --ado-board "Operations" \
  --ado-project "Embedded Refills Engineering" \
  --csv final_report.csv
```

Expected: `final_report.csv` should be empty or contain only area-path misalignments (easily fixed with manual moves).

---

## Configuration Files

All configuration files live in the `config/` directory at the project root.

### Field Mapping Strategy

The worker script uses a **4-step fuzzy matching algorithm** to discover and map custom fields from Jira to ADO:

#### Step 1: Known Field Mappings
Static dictionary of common Jira→ADO field name mappings:
- `story point estimate` → `Story Points` (Microsoft.VSTS.Scheduling.StoryPoints)
- `fix version` → `Fix Versions`
- `release` → `Release`
- `labels` → `Tags`
- `rank` → `Rank`
- `impact (rice)` → `Impact`
- `effort (rice)` → `Effort`
- `reach (rice)` → `Reach`
- `confidence (rice)` → `Confidence`
- `score (rice)` → `Score`

#### Step 2: Exact Name Match
Search ADO fields for exact name match (case-insensitive).

#### Step 3: Contains Match
Look for ADO field names that contain the Jira field name as a substring.

#### Step 4: Token Fuzzy Overlap
Use token-level fuzzy matching with 0.75 similarity threshold:
- Split both names into words (tokens)
- Calculate overlap ratio
- Match if ratio ≥ 0.75

#### Fallback: Description Block
If no ADO field is found after all 4 steps:
- Write field value to work item description in a formatted HTML section
- Log as WARNING for manual review
- Include field name, value, and remediation instructions

### Custom Field Discovery

During migration, the worker script:
1. Extracts all non-standard Jira fields from issues (custom fields, RICE fields, etc.)
2. For each field found, attempts to match it to an ADO field using the 4-step strategy
3. If matched, writes the value to the ADO field
4. If not matched and field is important (RICE, release, versions), attempts to create a new custom field in ADO
5. If creation fails (due to permissions or validation), logs the error and falls back to description block

**Permission Requirements for Custom Field Creation:**
- Your ADO **Organization** must grant you "**Edit process**" permission (not just project admin)
- This is an **organization-level** permission, not a project-level permission
- You can grant it in: Organization Settings → Permissions → "Edit process"
- If permission is missing, field creation will fail with: `VS402356: You do not have the permissions required to perform the attempted operation on this process.`

### ado_config.json

Connection settings for Azure DevOps. See `config/example-ado_config.json`.

```json
{
  "organization_url": "https://dev.azure.com/your-org",
  "username": "your.name@example.com",
  "access_token": "your ADO personal access token",
  "project": "Your Project Name"
}
```

### jira_config.json

Connection settings for Jira. See `config/example-jira_config.json`.

```json
{
  "server": "https://your-instance.atlassian.net",
  "email": "your.name@example.com",
  "access_token": "your Jira API token"
}
```

> Note: When `--jira-instance` is provided, the server URL is built automatically as `https://<instance>.atlassian.net`, overriding the `server` value in the config file. The `email` and `access_token` are still read from the config.

---

## Sprint → Iteration Mapping

The worker script automatically maps Jira sprints to ADO iterations:

### How It Works

1. **Sprint Extraction** — Reads Jira's `customfield_10007` (Sprint field)
2. **Iteration Path Construction** — Builds ADO iteration path: `{ProjectName}\{TeamName}\{SprintName}`
3. **Fallback Logic** — If the sprint-specific iteration path fails (e.g., sprint doesn't exist as an iteration node in ADO):
   - Falls back to team default iteration path
   - Logs warning: `[iteration-path] Failed to set sprint 'SUST Sprint 182': TF401347...`

### Example

**Jira Issue SUST-2259:**
- Sprint: "SUST Sprint 182"
- Team: "Sustaining Engineering Team"
- Project: "Embedded Refills Engineering"

**Attempted ADO iteration path:** 
```
Embedded Refills Engineering\Sustaining Engineering Team\SUST Sprint 182
```

**If that fails (sprint node doesn't exist in ADO):**
```
Embedded Refills Engineering\Sustaining Engineering Team
```

### Creating ADO Iterations

If you want sprint-specific iteration paths to work:
1. Create iterations in ADO that match your Jira sprint names
2. Go to: Azure DevOps Project → Project Settings → Iterations
3. Create under the appropriate team with the exact sprint name
4. Rerun migration

---

## Troubleshooting & Known Issues

### Permission Issues: "VS402356: You do not have permissions to perform operation on this process"

**Symptom:** Migration completes but logs show:
```
VS402356: You do not have the permissions required to perform the attempted operation on this process.
```

This occurs when trying to attach fields to work item types.

**Cause:** Your ADO user lacks **"Edit process"** permission at the **Organization level** (not project level).

**Solution:**
1. Go to Azure DevOps: **Organization Settings** (click your org name top-left)
2. Select **Permissions** from left sidebar
3. Find your user in the list
4. Grant **"Edit process"** permission
5. Wait 5-10 minutes for permission to propagate
6. Retry migration

**Note:** Project admin rights alone are NOT sufficient. Permission must be granted at organization level by an org admin.

### User Gaps: "These users are not in ADO"

**Symptom:** Analysis shows users as missing from ADO even though they're already members.

**Cause (Old):** Previous logic only checked users assigned to existing work items, missing users invited but not yet assigned.

**Fix (New):** Now uses ADO Graph API to check actual organization membership first, then falls back to assigned users.

**Workaround if issue persists:**
1. Verify users are actually members: Organization Settings → Members
2. Re-run analysis — it should now detect them
3. If still missing, check if Graph API is enabled in your PAT scope

### Iteration Path: "TF401347: Invalid tree name given for work item"

**Symptom:** Migration logs show:
```
TF401347: Invalid tree name given for work item 1234567, field 'System.IterationPath'
```

When attempting: `Embedded Refills Engineering\Sustaining Engineering Team\SUST Sprint 182`

**Cause:** The sprint "SUST Sprint 182" doesn't exist as an iteration node in ADO.

**Solution:**
1. Create the iteration in ADO: Project Settings → Iterations
2. Add under the correct team with exact sprint name
3. Retry migration (worker script will auto-detect and use it)

**Fallback (Already Implemented):** If sprint path fails, worker automatically sets team default iteration. Cards will be in backlog by default and can be manually moved.

### Invalid Field Names: "VS402800: The name ... contains invalid characters"

**Symptom:** Field creation fails with:
```
VS402800: The name 'Score (RICE)' is invalid. Names cannot be empty... or contain: '.,;~:/\*|?\"&%$!+=()[]{}<>-์.'
```

**Cause:** ADO rejects special characters including parentheses in field names.

**Solution (Already Implemented):** Field names are sanitized before creation:
- `Score (RICE)` → `Score RICE`
- `Reach (RICE)` → `Reach RICE`
- Other special characters removed or replaced with underscores

### Attachment Upload Failures

**Symptom:** Worker script reports "2 succeeded, 0 failed" but verification finds missing attachments (e.g., Jira: 23, ADO: 22).

**Cause:** Large files (>500MB) or transient network issues trigger 503 timeout on ADO API. The worker script now retries 3 times with exponential backoff (2s, 4s, 8s).

**Solution:**
1. Check `worker_jira_to_ado_copy.log` for failed filenames after retry exhaustion
2. Verify those specific cards: `python3 verify_migration.py --jira-keys "YOUR-CARD-ID"...`
3. If still failing, use `--skip-attachments` flag and upload files manually:
   ```bash
   python3 jira_ado_copy/scripts/worker_jira_to_ado_copy.py \
     --jira-instance healthfinch \
     --jira-keys "YOUR-CARD-ID" \
     --ado-project "Embedded Refills Engineering" \
     --skip-attachments
   ```

### Missing Parent Cards

**Symptom:** Subtask is created but description says "Parent not found in ADO".

**Cause:** Parent card was not migrated or not yet migrated when subtask tried to link.

**Solution:** The migration worker now creates subtasks even with missing parents and falls back to a Jira URL hyperlink. If you want to relink to ADO parent later:
1. Migrate the parent card
2. Manually update the subtask description or link

### Unknown Identity (Display Names)

**Symptom:** Assignee field shows name like "CJ" or "System User", not the actual person.

**Cause:** The identity does not exist in ADO's identity system (display name not recognized).

**Solution:** Worker script falls back to description text. Check `worker_jira_to_ado_copy.log` for "identity fallback" warnings. Manually assign the card in ADO if needed.

### Pagination Capped at 1000

**Symptom:** Gap analysis shows only 1000 items migrated when filter returns 1521.

**Cause:** Old worker script used `maxResults=1000` hard-coded limit without pagination.

**Solution:** Current worker script uses cursor-based pagination (via `nextPageToken`). Migration gap analysis script (`migration_gap_analysis.py`) also uses cursor-based pagination and correctly identifies all cards.

### Area Path Not Set

**Symptom:** Card migrated to ADO but area path is missing.

**Cause:** Worker script doesn't set area path; cards default to root. Gap analysis classifies as `no_area_path`.

**Solution:** Use gap analysis CSV to identify affected cards, then move them in ADO or add area path during migration via custom field mapping.

### Filter ID vs Jira Key

**Jira Filter ID:**
- Numeric ID from saved Jira filter (e.g., `11657`)
- Returns multiple cards (potentially 1000s)
- Use for bulk migration: `--jira-filter 11657`

**Jira Key:**
- Unique identifier for single card (e.g., `OP-1480`)
- Identifies exactly one card
- Use for specific cards: `--jira-keys "OP-1480,OP-824"`

---

## Recent Changes & Enhancements

### Session 2026-09-23: User Detection, Custom Fields & Sprint Mapping

**Major Changes:**
- **User Gap Detection** — Now prioritizes ADO Graph API for accurate organization membership checks (fixes false-positive user gaps when users are invited but not yet assigned to work items)
- **Custom Field Mapping** — Enhanced fuzzy matching with 4-step strategy:
  1. Known field mappings (Story Points, Fix Versions, Effort, Impact, Reach)
  2. Exact name match
  3. Contains-match (field name contains target substring)
  4. Token fuzzy overlap match (0.75 threshold)
- **Sprint → Iteration Mapping** — Extract Jira sprint names and attempt `System.IterationPath` mapping with automatic fallback to team default iteration when sprint path not found
- **Field Validation & Name Sanitization** — Remove special characters (parentheses) from RICE field names before creation (ADO rejects names like "Score (RICE)")
- **Dynamic Field Discovery** — Improved logging for field attachment operations and permission error handling
- **Permission Checks** — Clear errors when ADO "Edit process" permission is missing (VS402356)
- **API Analysis Improvements** — Fixed JQL pagination headers and corrected GPT model name to gpt-5.4-mini

**Files Modified:**
- `api/analysis_engine.py` — User detection logic + Graph API prioritization
- `jira_ado_copy/scripts/worker_jira_to_ado_copy.py` — Sprint extraction + iteration path setting + dynamic field discovery
- `utilities/utils_ado.py` — 4-step fuzzy field matching + known field mappings + field creation/attachment
- `utilities/utils_jira.py` — Custom field extraction for RICE and complex fields
- `api_server.py` — Enhanced status endpoint with default field values
- `docker-compose.yml` / `docker-compose.prod.yml` — Updated environment variables
- `jira_ado_copy/scripts/verify_migration.py` — Enhanced logging

**New Features:**
- **Known Field Mappings** — Hardcoded mappings for common Jira→ADO fields (Story Points, Fix Versions, etc.)
- **RICE Field Support** — Maps Jira RICE scoring fields (Impact, Reach, Confidence, Effort, Score) to ADO
- **Permission Requirement** — Now validates ADO "Edit process" permission at org level (not project level)
- **Fallback Iteration Path** — When sprint-specific iteration path fails, automatically use team default

**Configuration Updates:**
- Removed example config files (using code-based configuration instead)
- Field mappings now in Python code, not JSON files
- ADO credentials read from environment variables or ado_config.json

### Previous Session Changes

**+3,379 insertions, -233 deletions across 9 files**

### New Files
- `migration_gap_analysis.py` (591 lines) — Identify missed/misaligned cards
- Enhanced `verify_migration.py` (890 lines) — Per-field verification with HTML reports

### Enhanced Core Features
- **Graceful parent handling** — Subtasks created with hyperlink fallback
- **Attachment dedup** — Remove duplicate attachments before upload
- **Jira key tagging** — Automatic `jiraKey=<KEY>` tag on all migrated items
- **Identity fallback** — Use description text when assignee not found in ADO
- **Cursor-based pagination** — Fetch all Jira items, not just first 1000
- **3-attempt retry** — Exponential backoff for transient attachment failures (503 timeout)
- **Comprehensive logging** — Ready-to-run `--retry-failed` command in summary

### Configuration Updates
- `state_config.json` — Added Epics, Backlog, Draft, Declined, In Development, etc.
- `type_config.json` — Added Subtask, New Feature mappings

---

## Quick Reference: Common Commands

| Task | Command |
|---|---|
| Migrate by filter | `python3 worker_jira_to_ado_copy.py --jira-instance healthfinch --jira-filter YOUR-FILTER-ID --ado-project "Embedded Refills Engineering"` |
| Migrate specific cards | `python3 worker_jira_to_ado_copy.py --jira-instance healthfinch --jira-keys "YOUR-CARD-ID,YOUR-CARD-ID-2" --ado-project "Embedded Refills Engineering"` |
| Find migration gaps | `python3 migration_gap_analysis.py --jira-instance healthfinch --jira-filter YOUR-FILTER-ID --ado-board "YOUR-BOARD" --ado-project "Embedded Refills Engineering" --csv gaps.csv` |
| Verify all cards in project | `python3 verify_migration.py --jira-instance healthfinch --project-key YOUR-PROJECT-KEY --ado-project "Embedded Refills Engineering"` |
| Verify specific cards | `python3 verify_migration.py --jira-instance healthfinch --jira-keys "YOUR-CARD-ID,YOUR-CARD-ID-2" --ado-project "Embedded Refills Engineering"` |
| Debug attachment issues | `python3 worker_jira_to_ado_copy.py --jira-instance healthfinch --jira-keys "YOUR-CARD-ID" --ado-project "Embedded Refills Engineering" --skip-attachments` |
