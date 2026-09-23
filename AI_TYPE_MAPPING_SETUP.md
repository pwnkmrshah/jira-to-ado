# AI-Driven Type Mapping Setup Guide

## Overview

The migration platform now uses **OpenAI's GPT-4o-mini** to intelligently recommend Jira-to-ADO type mappings instead of relying on static JSON configurations.

**Benefits:**
- 📊 **Intelligent Analysis** — AI samples Jira issues and recommends best ADO equivalents
- 🎯 **Accurate Confidence Scores** — Based on actual analysis, not just static JSON (100%)
- 🔄 **Context-Aware** — AI sees issue titles and descriptions for better recommendations
- 📈 **Better Defaults** — Recommendations improve with more data

## How It Works

### Before (Static JSON)
```
Jira Type → type_config.json → ADO Type (100% confidence "because JSON says so")
```

### After (AI-Driven)
```
Jira Types (+ sample issues)
    ↓
OpenAI GPT-4o-mini
    ↓
Intelligent Recommendations + Real Confidence Scores
    ↓
User can override in UI
```

## Setup Instructions

### Step 1: Get OpenAI API Key

1. Go to **https://platform.openai.com/account/api-keys**
2. Click **"Create new secret key"**
3. Copy the key (you won't see it again!)
4. ⚠️ **Keep it secret** — Never commit to git

### Step 2: Add to .env

Open `.env` in your project root:

```env
OPENAI_API_KEY=sk-proj-xxxxxxxxxxxxxxxxxxxx
```

Replace `sk-proj-xxxxxxxxxxxxxxxxxxxx` with your actual key.

### Step 3: Restart Docker

```bash
cd C:\Users\PawanShah\jira-to-ado
docker-compose down
docker-compose up -d
```

### Step 4: Verify It's Working

In the UI, click **"Analyze"** and you should see:

```
Type Mappings (Jira → Azure DevOps)

Jira Type | Count | ADO Type (Editable) | Confidence | Reason
----------|-------|---------------------|------------|--------
Story     | 45    | User Story       ✓  | 94%        | AI-generated recommendation based on...
Bug       | 12    | Bug              ✓  | 98%        | Exact semantic match
Task      | 8     | Task             ✓  | 92%        | AI analysis of issue patterns
```

**Key indicators AI is working:**
- ✅ Confidence scores like **94%, 78%, 85%** (not just 100%)
- ✅ Reason says **"AI-generated recommendation"** or **"AI analysis"**
- ✅ Backend logs show: `[type-mapping-ai] AI mappings generated: ...`

### Step 5: Check Backend Logs

```bash
docker logs -f jira-ado-api | grep -i "type-mapping"
```

You should see:
```
[type-mapping-ai] AI mappings generated: ['Story', 'Bug', 'Task']
[analysis] Successfully generated AI mappings for 3 type(s)
```

## Fallback Behavior

If **OPENAI_API_KEY is not set or OpenAI API fails**:

```
WARNING [type-mapping-ai] OPENAI_API_KEY not set - falling back to semantic matching
```

The system gracefully falls back to semantic matching (original behavior), so migration still works.

## Troubleshooting

### AI mappings not showing

**Check logs:**
```bash
docker logs jira-ado-api | tail -50
```

**Look for:**
- `OPENAI_API_KEY not set` → Add key to .env (Step 2)
- `OpenAI API call failed` → Check API key is valid
- `Empty response from OpenAI` → OpenAI API issue, check status at https://status.openai.com

### Invalid OpenAI Key

Error in logs:
```
OpenAI API call failed: 401 Unauthorized
```

**Solution:**
1. Generate new key at https://platform.openai.com/account/api-keys
2. Update `.env`
3. Restart Docker

### Slow Analysis

OpenAI calls can take 2-5 seconds. This is normal and happens only during `/analyze`, not during migration.

## API Cost

- **Model:** `gpt-4o-mini` (cheapest GPT-4 option)
- **Per analysis:** ~0.01 USD
- **Monthly (~1000 analyses):** ~$10 USD

## Production Deployment

For Render, Azure, or other cloud platforms:

1. Set `OPENAI_API_KEY` environment variable in platform settings
2. Docker-compose will automatically read it
3. No code changes needed

### Render Example

```bash
# Via CLI
render env set OPENAI_API_KEY=sk-proj-xxxxx

# Or in dashboard: Environment → Settings → OPENAI_API_KEY
```

### Azure Example

Store in Azure Key Vault, then in App Service:
```
Configuration → Application settings → New application setting
Name: OPENAI_API_KEY
Value: (your key from Key Vault)
```

## Disable AI Mapping (Revert to Static JSON)

If you want to go back to static `type_config.json`:

1. Remove `OPENAI_API_KEY` from `.env`
2. Restart Docker
3. System will automatically fallback to semantic matching

## Questions?

- OpenAI docs: https://platform.openai.com/docs
- API key management: https://platform.openai.com/account/api-keys
- Pricing: https://openai.com/pricing
