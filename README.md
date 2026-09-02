# Jira to ADO Copy

Complete migration suite for copying work items from Jira to Azure DevOps with validation, gap analysis, and verification.

**Three main tools:**
1. **`worker_jira_to_ado_copy.py`** — Migrate cards by filter ID or specific keys
2. **`migration_gap_analysis.py`** — Identify missing or misaligned cards after migration
3. **`verify_migration.py`** — Verify field-by-field accuracy of migrated items

**API bridge (Forge integration):**
- **`api_server.py`** — Flask HTTP server that wraps the three tools above. Used when the Jira Forge app (`jira-ado-migrator-forge-app`) triggers migrations from the browser UI instead of the CLI.

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

### type_config.json

Maps Jira issue types to ADO work item types. The `Default` key is used when no match is found.

```json
{
  "Default": "Issue",
  "Task": "Task",
  "Bug": "Bug",
  "Story": "User Story",
  "Epic": "Epic"
}
```

### state_config.json

Maps Jira statuses to ADO states. The `Default` key is used when no match is found.

```json
{
  "Default": "New",
  "In Progress": "Active",
  "Done": "Completed"
}
```

### custom_fields_config.json

Defines custom field mappings between Jira and ADO. Each entry specifies the ADO field path, the Jira custom field ID, and the sub-field name to extract from the Jira value.

```json
[
  {
    "ado_field": "/fields/System.Tags",
    "jira_field": "customfield_10007",
    "jira_field_name": "name"
  }
]
```



## Troubleshooting & Known Issues

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

## Recent Changes (Current vs Production)

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
