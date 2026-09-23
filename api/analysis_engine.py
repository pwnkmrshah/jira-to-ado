"""
api/analysis_engine.py
Fetches Jira + ADO data and produces an AI-driven analysis report for the UI.

Given a natural-language intent, Jira credentials, and an ADO project, this
module samples Jira issues and compares them against the ADO project's work
item type/state catalogue. Type and state mapping suggestions are generated
by GPT-mini (see _ai_map_types / _ai_map_states) — there is no static config
file or hardcoded equivalents table; the UI lets a human override any
suggestion afterwards.
"""

from __future__ import annotations

import json
import logging
import re

import requests

logger = logging.getLogger(__name__)

# Max issues to fetch for analysis — enough for field/type discovery without
# being slow over a wide-area network.
SAMPLE_SIZE = 200


# ---------------------------------------------------------------------------
# Intent → JQL
# ---------------------------------------------------------------------------

def _intent_to_jql(intent: str) -> str | None:
    """
    Best-effort natural-language → JQL.

    Returns:
        JQL string, or None to signal "use filter API".
    """
    stripped = intent.strip()

    # Pure number → caller should use the filter API.
    if re.fullmatch(r"\d+", stripped):
        return None

    # Already looks like JQL (contains operators or IS/IN/AND/OR keywords)
    if re.search(r"\b(AND|OR|NOT|IS|IN|WAS|CHANGED|ORDER)\b", stripped, re.IGNORECASE) or "=" in stripped:
        return stripped

    parts: list[str] = []

    # Extract a Jira project key (2-6 uppercase letters)
    project_match = re.search(r"\b([A-Z]{2,6})\b", stripped)
    if project_match:
        parts.append(f"project = {project_match.group(1)}")

    # Status hints from plain English
    intent_lower = stripped.lower()
    if "active" in intent_lower or "sprint" in intent_lower or "in progress" in intent_lower:
        parts.append("statusCategory in ('In Progress', 'To Do')")
    elif "done" in intent_lower or "closed" in intent_lower or "completed" in intent_lower:
        parts.append("statusCategory = Done")
    elif "won't do" in intent_lower or "wont do" in intent_lower:
        parts.append("status != \"Won't Do\"")

    jql_base = " AND ".join(parts) if parts else "created >= -365d"
    return (jql_base + " ORDER BY created DESC").strip()


# ---------------------------------------------------------------------------
# Jira API helpers
# ---------------------------------------------------------------------------

def _jira_get(jira_url: str, email: str, token: str, path: str, params: dict | None = None) -> dict | list | None:
    url = f"{jira_url.rstrip('/')}{path}"
    try:
        r = requests.get(url, auth=(email, token), params=params or {}, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        logger.error("[analysis] Jira GET %s failed: %s", path, exc)
        return None


def _fetch_filter_jql(jira_url: str, email: str, token: str, filter_id: str) -> str | None:
    data = _jira_get(jira_url, email, token, f"/rest/api/3/filter/{filter_id}")
    if not data:
        return None
    return data.get("jql")


def _fetch_issues(jira_url: str, email: str, token: str, jql: str) -> list[dict]:
    """
    Fetch ALL issues matching the JQL using nextPageToken pagination.
    Uses POST /rest/api/3/search/jql endpoint (supports unlimited results, no 1000-item cap).
    """
    url = f"{jira_url.rstrip('/')}/rest/api/3/search/jql"
    all_issues = []
    page_token = None
    
    logger.info(f"[analysis] Fetching issues from {url} with JQL: {jql}")
    
    try:
        while True:
            body = {
                "jql": jql,
                "maxResults": 200,
                "fields": ["key", "issuetype", "status", "assignee", "attachment", "comment", "priority"],
            }
            if page_token:
                body["nextPageToken"] = page_token
            
            logger.debug(f"[analysis] _fetch_issues: Request body = {json.dumps(body, default=str)}")
            
            r = requests.post(
                url,
                auth=(email, token),
                json=body,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=15
            )
            
            # Log response details before raising for status
            logger.debug(f"[analysis] _fetch_issues: Response status {r.status_code}")
            if r.status_code >= 400:
                logger.error(f"[analysis] _fetch_issues: Error response body = {r.text}")
            
            r.raise_for_status()
            
            data = r.json()
            issues = data.get("issues", [])
            all_issues.extend(issues)
            
            # Check for next page
            page_token = data.get("nextPageToken")
            if not page_token:
                # No more pages
                break
        
        logger.info(f"[analysis] Fetched {len(all_issues)} total issues (pagination complete)")
        return all_issues
    except Exception as exc:
        logger.error("[analysis] Jira search/jql POST failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# ADO API helpers
# ---------------------------------------------------------------------------

def _fetch_ado_work_item_types(ado_org: str, ado_project: str, ado_pat: str) -> list[str]:
    from urllib.parse import quote
    url = (
        f"https://dev.azure.com/{ado_org}/{quote(ado_project, safe='')}/"
        "_apis/wit/workitemtypes?api-version=7.0"
    )
    try:
        r = requests.get(url, auth=("", ado_pat), timeout=30)
        r.raise_for_status()
        return [t["name"] for t in r.json().get("value", [])]
    except Exception as exc:
        logger.error("[analysis] ADO work item types fetch failed: %s", exc)
        return []


def _fetch_ado_states(ado_org: str, ado_project: str, ado_pat: str, work_item_types: list[str]) -> list[str]:
    """Union of state names across a project's work item types (order preserved, deduped).

    ADO states are scoped per work item type, so this samples a few of the project's
    types (states are usually shared across a process template) and merges the names.
    """
    from urllib.parse import quote
    seen: dict[str, None] = {}
    for wit in work_item_types[:6]:
        url = (
            f"https://dev.azure.com/{ado_org}/{quote(ado_project, safe='')}/"
            f"_apis/wit/workitemtypes/{quote(wit, safe='')}/states?api-version=7.0"
        )
        try:
            r = requests.get(url, auth=("", ado_pat), timeout=30)
            r.raise_for_status()
            for s in r.json().get("value", []):
                name = s.get("name")
                if name and name not in seen:
                    seen[name] = None
        except Exception as exc:
            logger.warning("[analysis] ADO states fetch failed for type %r: %s", wit, exc)
    return list(seen.keys())


def _fetch_ado_users(ado_org: str, ado_project: str, ado_pat: str) -> set[str]:
    """
    Returns a set of lowercased ADO user email addresses.
    
    Prioritizes Graph API to get all organization members (not just assigned users).
    Falls back to work items if Graph API is unavailable.
    """
    # First, try Graph API to get ALL ADO members (most accurate for membership check)
    ado_users = _fetch_ado_users_from_graph(ado_org, ado_pat)
    if ado_users:
        logger.info(f"[analysis] Found {len(ado_users)} unique users via ADO Graph API")
        return ado_users
    
    # Fallback to work items if Graph API not available
    logger.info("[analysis] Graph API returned no results, falling back to work items")
    ado_users = _fetch_ado_users_from_workitems(ado_org, ado_project, ado_pat)
    if ado_users:
        logger.info(f"[analysis] Found {len(ado_users)} unique users in ADO project work items")
        return ado_users
    
    logger.warning("[analysis] Could not fetch ADO users via any method")
    return set()


def _fetch_ado_users_from_workitems(ado_org: str, ado_project: str, ado_pat: str) -> set[str]:
    """
    Fetches users by querying work items with assignees in the ADO project.
    Returns a set of lowercased email addresses of users assigned to work items.
    """
    # WIQL query with @project variable (ADO will substitute it)
    wiql_query = "SELECT [System.Id], [System.AssignedTo] FROM WorkItems WHERE [Team Project] = @project AND [System.AssignedTo] <> ''"
    
    url = f"https://dev.azure.com/{ado_org}/{ado_project}/_apis/wit/wiql?api-version=7.0"
    
    try:
        # Execute WIQL query with variables parameter
        r = requests.post(
            url,
            auth=("", ado_pat),
            json={
                "query": wiql_query,
                "variables": {}  # ADO automatically substitutes @project
            },
            timeout=30
        )
        r.raise_for_status()
        
        workitem_refs = r.json().get("workItems", [])
        logger.info(f"[analysis] Found {len(workitem_refs)} work items with assignees in project")
        
        if not workitem_refs:
            logger.warning("[analysis] WIQL returned no work items - users from this project cannot be fetched")
            return set()
        
        # Now fetch the actual work items to get assignee details
        # Get up to 200 work items (IDs only from WIQL)
        ids = [str(wi["id"]) for wi in workitem_refs[:200]]
        if not ids:
            return set()
        
        # Batch fetch work items with assignee info
        ids_param = ",".join(ids)
        details_url = f"https://dev.azure.com/{ado_org}/{ado_project}/_apis/wit/workitems?ids={ids_param}&fields=System.AssignedTo&api-version=7.0"
        
        r = requests.get(details_url, auth=("", ado_pat), timeout=30)
        r.raise_for_status()
        
        # Extract email addresses from assignees
        users = set()
        for item in r.json().get("value", []):
            assignee = item.get("fields", {}).get("System.AssignedTo", {})
            if assignee and isinstance(assignee, dict):
                email = assignee.get("uniqueName", "").lower()
                if email and "@" in email:
                    users.add(email)
        
        logger.info(f"[analysis] Extracted {len(users)} unique users from work items: {users}")
        return users
        
    except Exception as exc:
        logger.warning("[analysis] WIQL work item fetch failed: %s", exc)
        return set()


def _fetch_ado_users_from_graph(ado_org: str, ado_pat: str) -> set[str]:
    """
    Fallback to fetch ADO users via Graph API.
    Returns a set of lowercased ADO member email addresses.
    """
    url = f"https://vssps.dev.azure.com/{ado_org}/_apis/graph/users?api-version=7.1-preview.1"
    try:
        r = requests.get(url, auth=("", ado_pat), timeout=30)
        if r.status_code in (403, 401):
            logger.info("[analysis] ADO Graph API not in PAT scope — cannot verify user availability")
            return set()
        r.raise_for_status()
        return {
            u.get("mailAddress", "").lower()
            for u in r.json().get("value", [])
            if u.get("mailAddress")
        }
    except Exception as exc:
        logger.warning("[analysis] ADO Graph API fetch failed: %s", exc)
        return set()


# ---------------------------------------------------------------------------
# AI-driven type & state mapping — this is the ONLY mapping engine. There is
# no hardcoded semantic-equivalents table and no static config-file mapping;
# every Jira→ADO type/state suggestion comes from an LLM call, with the UI
# left to let a human override any suggestion afterwards.
# ---------------------------------------------------------------------------

_AI_ENDPOINT = 'https://crvdev-cus-dev-foundry.services.ai.azure.com/openai/v1/responses'
_AI_MODEL = 'gpt-5.4-mini'


def _call_llm(prompt: str, max_output_tokens: int = 800) -> str | None:
    """Call the GPT-mini Responses API. Returns the raw text output, or None
    if GPT_API_KEY isn't configured or the call fails for any reason.
    """
    import os
    api_key = os.environ.get('GPT_API_KEY', '')
    if not api_key:
        logger.warning('[ai-mapping] GPT_API_KEY not set — cannot use AI mapping')
        return None
    try:
        resp = requests.post(
            _AI_ENDPOINT,
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'model': _AI_MODEL, 'input': prompt, 'max_output_tokens': max_output_tokens},
            timeout=20,
        )
        resp.raise_for_status()
        body = resp.json()
        text = body.get('output_text', '').strip()
        if not text:
            for item in body.get('output', []):
                if item.get('type') != 'message':
                    continue
                for c in item.get('content', []):
                    if c.get('type') == 'output_text' and c.get('text'):
                        text = c['text'].strip()
                        break
                if text:
                    break
        return text or None
    except Exception as exc:
        logger.warning('[ai-mapping] LLM call failed: %s', exc)
        return None


def _extract_json(text: str):
    """Pull the first JSON array/object out of a possibly markdown-fenced LLM reply."""
    match = re.search(r'\[.*\]|\{.*\}', text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception as exc:
        logger.warning('[ai-mapping] Could not parse JSON from LLM reply: %s', exc)
        return None


def _ai_map_types(by_type: list[dict], ado_types: list[str]) -> list[dict] | None:
    """Ask GPT-mini to map every Jira issue type to the best available ADO work
    item type. Returns None (caller falls back) if the AI call fails or
    returns no usable mapping.
    """
    if not by_type or not ado_types:
        return None
    jira_list = '\n'.join(f'- "{t["name"]}" ({t["count"]} issues)' for t in by_type)
    ado_list = ', '.join(f'"{t}"' for t in ado_types)
    prompt = (
        'You are an expert in migrating Jira issues to Azure DevOps (ADO) work items. '
        'For each Jira issue type below, choose the single best-matching ADO work item type '
        'from the AVAILABLE ADO TYPES list (you must pick one from that exact list — never invent a new name). '
        'Respond with ONLY a JSON array, no prose, no markdown fences, shaped like:\n'
        '[{"jira": "<jira type>", "ado": "<one of the available ADO types>", '
        '"confidence": <integer 0-100>, "reason": "<one short sentence>"}, ...]\n\n'
        f'JIRA ISSUE TYPES:\n{jira_list}\n\n'
        f'AVAILABLE ADO TYPES: {ado_list}\n'
    )
    text = _call_llm(prompt)
    if not text:
        return None
    parsed = _extract_json(text)
    if not isinstance(parsed, list):
        return None

    ado_lower_to_display = {t.lower(): t for t in ado_types}
    counts = {t['name']: t['count'] for t in by_type}
    results = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        jira_name = entry.get('jira')
        if jira_name not in counts:
            continue
        ado_choice = str(entry.get('ado', '')).strip()
        ado_display = ado_lower_to_display.get(ado_choice.lower())
        if not ado_display:
            # AI hallucinated a type not in the list — fall back to the first available
            # ADO type for this entry rather than dropping it silently.
            ado_display = ado_types[0]
            confidence = 50
            reason = f'AI suggested "{ado_choice}" which isn\'t in this project — defaulted to "{ado_display}".'
        else:
            confidence = int(entry.get('confidence', 60))
            reason = str(entry.get('reason', 'AI-suggested mapping'))
        results.append({
            'jira': jira_name, 'count': counts[jira_name],
            'ado': ado_display, 'confidence': max(0, min(confidence, 100)),
            'reason': reason,
        })

    # Make sure every Jira type got a mapping — fill in any the AI skipped.
    mapped_names = {r['jira'] for r in results}
    for t in by_type:
        if t['name'] not in mapped_names:
            results.append({
                'jira': t['name'], 'count': t['count'],
                'ado': ado_types[0], 'confidence': 50,
                'reason': 'AI did not return a mapping for this type — defaulted to the first available ADO type.',
            })
    return results or None


def build_type_mappings(by_type: list[dict], ado_types: list[str], type_config: dict | None = None) -> list[dict]:
    """Generate type mappings using AI (GPT-mini). Falls back to an exact-name-match
    (or first-available-type) safety net only when the AI call itself is unavailable.
    """
    ai_result = _ai_map_types(by_type, ado_types)
    if ai_result is not None:
        return ai_result

    logger.warning('[ai-mapping] AI type mapping unavailable — using exact-match fallback only')
    ado_lower_to_display = {t.lower(): t for t in ado_types}
    fallback_type = ado_types[0] if ado_types else 'Task'
    out = []
    for t in by_type:
        jira_key = t['name'].lower().strip()
        if jira_key in ado_lower_to_display:
            out.append({
                'jira': t['name'], 'count': t['count'],
                'ado': ado_lower_to_display[jira_key], 'confidence': 90,
                'reason': 'Exact name match (AI mapping unavailable)',
            })
        else:
            out.append({
                'jira': t['name'], 'count': t['count'],
                'ado': fallback_type, 'confidence': 40,
                'reason': f'No AI mapping available and no exact match for "{t["name"]}" — human review required.',
            })
    return out


def _ai_map_states(by_status: list[dict], ado_states: list[str]) -> list[dict] | None:
    """Ask GPT-mini to map every Jira status to the best available ADO state."""
    if not by_status or not ado_states:
        return None
    jira_list = '\n'.join(f'- "{s["name"]}" ({s["count"]} issues)' for s in by_status)
    ado_list = ', '.join(f'"{s}"' for s in ado_states)
    prompt = (
        'You are an expert in migrating Jira issues to Azure DevOps (ADO) work items. '
        'For each Jira status below, choose the single best-matching ADO state '
        'from the AVAILABLE ADO STATES list (you must pick one from that exact list — never invent a new name). '
        'Respond with ONLY a JSON array, no prose, no markdown fences, shaped like:\n'
        '[{"jira": "<jira status>", "ado": "<one of the available ADO states>", '
        '"confidence": <integer 0-100>, "reason": "<one short sentence>"}, ...]\n\n'
        f'JIRA STATUSES:\n{jira_list}\n\n'
        f'AVAILABLE ADO STATES: {ado_list}\n'
    )
    text = _call_llm(prompt)
    if not text:
        return None
    parsed = _extract_json(text)
    if not isinstance(parsed, list):
        return None

    ado_lower_to_display = {s.lower(): s for s in ado_states}
    counts = {s['name']: s['count'] for s in by_status}
    results = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        jira_name = entry.get('jira')
        if jira_name not in counts:
            continue
        ado_choice = str(entry.get('ado', '')).strip()
        ado_display = ado_lower_to_display.get(ado_choice.lower())
        if not ado_display:
            ado_display = ado_states[0]
            confidence = 50
            reason = f'AI suggested "{ado_choice}" which isn\'t in this project — defaulted to "{ado_display}".'
        else:
            confidence = int(entry.get('confidence', 60))
            reason = str(entry.get('reason', 'AI-suggested mapping'))
        results.append({
            'jira': jira_name, 'count': counts[jira_name],
            'ado': ado_display, 'confidence': max(0, min(confidence, 100)),
            'reason': reason,
        })

    mapped_names = {r['jira'] for r in results}
    for s in by_status:
        if s['name'] not in mapped_names:
            results.append({
                'jira': s['name'], 'count': s['count'],
                'ado': ado_states[0], 'confidence': 50,
                'reason': 'AI did not return a mapping for this status — defaulted to the first available ADO state.',
            })
    return results or None


def build_state_mappings(by_status: list[dict], ado_states: list[str], state_config: dict | None = None) -> list[dict]:
    """Generate state mappings using AI (GPT-mini). Falls back to an exact-name-match
    (or first-available-state) safety net only when the AI call itself is unavailable.
    """
    ai_result = _ai_map_states(by_status, ado_states)
    if ai_result is not None:
        return ai_result

    logger.warning('[ai-mapping] AI state mapping unavailable — using exact-match fallback only')
    ado_lower_to_display = {s.lower(): s for s in ado_states}
    fallback_state = ado_states[0] if ado_states else 'New'
    out = []
    for s in by_status:
        jira_key = s['name'].lower().strip()
        if jira_key in ado_lower_to_display:
            out.append({
                'jira': s['name'], 'count': s['count'],
                'ado': ado_lower_to_display[jira_key], 'confidence': 90,
                'reason': 'Exact name match (AI mapping unavailable)',
            })
        else:
            out.append({
                'jira': s['name'], 'count': s['count'],
                'ado': fallback_state, 'confidence': 40,
                'reason': f'No AI mapping available and no exact match for "{s["name"]}" — human review required.',
            })
    return out




# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_analysis(
    ado_project: str,
    jira_url: str,
    jira_email: str,
    jira_token: str,
    ado_org: str,
    ado_pat: str,
    jira_project_key: str = '',
    jira_filter_id: str = '',  # NEW: optional filter ID
    jira_keys: list[str] | None = None,  # NEW: optional specific keys
    status_filter: list[str] | None = None,  # empty list = all statuses
    field_filter: list[str] | None = None,   # empty list = all fields
) -> dict:
    """
    Perform a pre-migration analysis and return a structured result dict.
    
    Scope can be specified by one of:
      - jira_filter_id: Analyze issues in a specific Jira filter
      - jira_keys: Analyze specific issue keys
      - jira_project_key: Analyze entire project (default)

    Returns dict with keys:
        total_issues, by_type, by_status, ado_available_types,
        type_gaps, user_gaps, attachment_count, comment_count,
        selected_statuses, selected_fields
    """
    if jira_keys is None:
        jira_keys = []
    
    # Determine JQL based on scope
    if jira_filter_id:
        # Use Jira filter: Let Jira resolve the filter ID server-side
        # Using 'filter = ID' in JQL is simpler and more reliable than fetching filter details
        jql = f'filter = {jira_filter_id}'
        scope_desc = f"filter {jira_filter_id}"
        logger.info(f"[analysis] Using Jira filter {jira_filter_id}")
    elif jira_keys:
        # Use specific keys
        quoted_keys = ', '.join(f'"{k}"' for k in jira_keys)
        jql = f'key in ({quoted_keys})'
        scope_desc = f"keys {jira_keys}"
        logger.info(f"[analysis] Analyzing scope: {scope_desc}")
    else:
        # Use project key (default)
        if not jira_project_key:
            return {"error": "Select a Jira board to analyze."}
        parts = [f'project = "{jira_project_key}"']
        if status_filter:
            quoted = ', '.join(f'"{s}"' for s in status_filter)
            parts.append(f'status in ({quoted})')
        jql = ' AND '.join(parts) + ' ORDER BY created DESC'
        scope_desc = f"project {jira_project_key}"
        logger.info(f"[analysis] Analyzing scope: {scope_desc}")

    logger.info("[analysis] JQL: %s", jql)

    # 2. Fetch Jira issues
    issues = _fetch_issues(jira_url, jira_email, jira_token, jql)
    if not issues:
        return {
            "error": "No issues found for the selected scope. Try selecting different filters/keys.",
            "jql_used": jql,
        }

    # 3. Aggregate counts
    type_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    jira_emails: set[str] = set()
    attachment_total = 0
    comment_total = 0

    for issue in issues:
        fields = issue.get("fields", {})

        itype = ((fields.get("issuetype") or {}).get("name") or "Unknown")
        type_counts[itype] = type_counts.get(itype, 0) + 1

        status = ((fields.get("status") or {}).get("name") or "Unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

        # Extract BOTH assignee and reporter emails
        assignee_email = ((fields.get("assignee") or {}).get("emailAddress") or "").lower()
        if assignee_email:
            jira_emails.add(assignee_email)
        
        reporter_email = ((fields.get("reporter") or {}).get("emailAddress") or "").lower()
        if reporter_email:
            jira_emails.add(reporter_email)

        attachment_total += len(fields.get("attachment") or [])
        comment_total += (fields.get("comment") or {}).get("total", 0)

    logger.info(f"[analysis] Extracted {len(jira_emails)} unique emails from Jira (assignees + reporters): {jira_emails}")

    # 4. Fetch ADO data (run both; they're independent)
    ado_types = _fetch_ado_work_item_types(ado_org, ado_project, ado_pat)
    ado_users = _fetch_ado_users(ado_org, ado_project, ado_pat)
    ado_states = _fetch_ado_states(ado_org, ado_project, ado_pat, ado_types)

    # Only report user gaps when we successfully fetched ADO users
    if ado_users:
        logger.info(f"[analysis] ADO users found: {len(ado_users)} unique emails: {ado_users}")
        user_gaps = [
            {"jira_user": email, "found_in_ado": False}
            for email in sorted(jira_emails)
            if email not in ado_users
        ]
        if user_gaps:
            logger.warning(f"[analysis] User gaps detected: {[g['jira_user'] for g in user_gaps]}")
        else:
            logger.info(f"[analysis] All Jira users found in ADO")
    else:
        logger.warning("[analysis] No ADO users could be fetched - user gap detection disabled")
        user_gaps = []

    by_type_list = [
        {"name": t, "count": c}
        for t, c in sorted(type_counts.items(), key=lambda x: -x[1])
    ]
    by_status_list = [
        {"name": s, "count": c}
        for s, c in sorted(status_counts.items(), key=lambda x: -x[1])
    ]

    # 5. Mapping — AI-driven (GPT-mini); no static config or hardcoded table.
    type_mappings = build_type_mappings(by_type_list, ado_types)
    state_mappings = build_state_mappings(by_status_list, ado_states)

    # Gaps derive directly from the AI's own confidence rather than a separate
    # static equivalents table — anything the AI wasn't confident about needs review.
    _GAP_CONFIDENCE_THRESHOLD = 70
    type_gaps = [
        {"jira_type": m["jira"], "has_ado_match": False}
        for m in type_mappings
        if m["confidence"] < _GAP_CONFIDENCE_THRESHOLD
    ]

    return {
        "total_issues": len(issues),
        "jql_used": jql,
        "by_type": by_type_list,
        "by_status": by_status_list,
        "ado_available_types": ado_types,
        "type_mappings": type_mappings,
        "ado_available_states": ado_states,
        "state_mappings": state_mappings,
        "type_gaps": type_gaps,
        "user_gaps": user_gaps,
        "attachment_count": attachment_total,
        "comment_count": comment_total,
        "selected_statuses": status_filter or [],
        "selected_fields": field_filter or [],
    }

