"""
api/analysis_engine.py
Phase 1: deterministic analysis — Jira + ADO API calls, no LLM required.

Given a natural-language intent, Jira credentials, and an ADO project, this
module samples Jira issues and compares them against the ADO project's work
item type catalogue to produce a structured analysis report for the UI.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re

import requests

_CONFIG_DIR = pathlib.Path(__file__).parent.parent / 'config'


def _load_type_config() -> dict:
    try:
        with open(_CONFIG_DIR / 'type_config.json') as f:
            return json.load(f)
    except Exception:
        return {}

logger = logging.getLogger(__name__)

# Max issues to fetch for analysis — enough for field/type discovery without
# being slow over a wide-area network.
SAMPLE_SIZE = 200


# ---------------------------------------------------------------------------
# Intent → JQL
# ---------------------------------------------------------------------------

def _intent_to_jql(intent: str) -> str | None:
    """
    Best-effort natural-language → JQL for Phase 1.
    Phase 3 will replace this with an LLM call.

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
    url = f"{jira_url.rstrip('/')}/rest/api/3/search/jql"
    body = {
        "jql": jql,
        "maxResults": SAMPLE_SIZE,
        "fields": ["issuetype", "status", "assignee", "attachment", "comment", "priority"],
    }
    try:
        r = requests.post(url, auth=(email, token), json=body, timeout=15)
        r.raise_for_status()
        return r.json().get("issues", [])
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
        r = requests.get(url, auth=("", ado_pat), timeout=12)
        r.raise_for_status()
        return [t["name"] for t in r.json().get("value", [])]
    except Exception as exc:
        logger.error("[analysis] ADO work item types fetch failed: %s", exc)
        return []


def _fetch_ado_users(ado_org: str, ado_pat: str) -> set[str]:
    """
    Returns a set of lowercased ADO member email addresses.
    Uses the VSSPS Graph API — may be empty if the PAT lacks the Graph scope;
    that is non-fatal (user gap detection is skipped).
    """
    url = f"https://vssps.dev.azure.com/{ado_org}/_apis/graph/users?api-version=7.1-preview.1"
    try:
        r = requests.get(url, auth=("", ado_pat), timeout=12)
        if r.status_code in (403, 401):
            logger.info("[analysis] ADO Graph API not in PAT scope — skipping user gap check")
            return set()
        r.raise_for_status()
        return {
            u.get("mailAddress", "").lower()
            for u in r.json().get("value", [])
            if u.get("mailAddress")
        }
    except Exception as exc:
        logger.warning("[analysis] ADO user fetch failed (non-fatal): %s", exc)
        return set()


# ---------------------------------------------------------------------------
# Gap computation
# ---------------------------------------------------------------------------

# Semantic equivalents used when Jira type name ≠ ADO type name exactly.
_TYPE_EQUIVALENTS: dict[str, set[str]] = {
    "story":    {"user story", "product backlog item", "requirement"},
    "epic":     {"epic", "feature"},
    "bug":      {"bug", "defect"},
    "task":     {"task"},
    "sub-task": {"task", "child task"},
    "subtask":  {"task", "child task"},
    "test":     {"test case", "test plan", "test suite"},
}


def _has_ado_match(jira_type: str, ado_types_lower: set[str], type_config: dict | None = None) -> bool:
    if type_config:
        mapped = type_config.get(jira_type) or type_config.get(jira_type.lower())
        if mapped and mapped.lower() in ado_types_lower:
            return True
    jl = jira_type.lower()
    if jl in ado_types_lower:
        return True
    for equiv in _TYPE_EQUIVALENTS.get(jl, set()):
        if equiv in ado_types_lower:
            return True
    return False


# ---------------------------------------------------------------------------
# Type mapping engine  (replaces the hardcoded frontend buildMappings table)
# ---------------------------------------------------------------------------

# Curated semantic equivalents with ADO display names (title-cased).
_SEMANTIC_EQUIVALENTS: dict[str, list[tuple[str, int, str]]] = {
    # jira_key: [(ado_type_lower, confidence, reason), ...]  ordered by preference
    "story":         [("user story", 96, "Industry-standard direct equivalent"),
                      ("product backlog item", 92, "Scrum equivalent of a User Story"),
                      ("requirement", 88, "Requirement maps to Story semantics")],
    "user story":    [("user story", 99, "Exact name match"),
                      ("product backlog item", 92, "Scrum equivalent")],
    "epic":          [("epic", 99, "Exact name match"),
                      ("feature", 88, "Feature is the closest ADO equivalent to Epic")],
    "feature":       [("feature", 99, "Exact name match"),
                      ("epic", 82, "Epic is the closest ADO equivalent to Feature")],
    "bug":           [("bug", 99, "Exact name match"),
                      ("defect", 97, "Exact semantic match — different display name")],
    "defect":        [("bug", 97, "Bug is the standard ADO name for a defect"),
                      ("defect", 99, "Exact name match")],
    "task":          [("task", 99, "Exact name match")],
    "sub-task":      [("task", 88, "Subtasks map to child Tasks in ADO"),
                      ("child task", 90, "Direct equivalent")],
    "subtask":       [("task", 88, "Subtasks map to child Tasks in ADO"),
                      ("child task", 90, "Direct equivalent")],
    "improvement":   [("user story", 79, "Improvements are user-facing enhancements — User Story is closest"),
                      ("task", 74, "No direct ADO equivalent; Task is most general")],
    "new feature":   [("feature", 90, "Direct semantic match"),
                      ("user story", 82, "Feature request maps well to User Story")],
    "technical debt":[("task", 72, "No direct ADO equivalent. Code-quality items map closest to Task"),
                      ("user story", 60, "Could be framed as a quality improvement story")],
    "test":          [("test case", 95, "Direct equivalent"),
                      ("task", 70, "No test type in this project — Task is the fallback")],
    "test case":     [("test case", 99, "Exact name match"),
                      ("task", 70, "No test type — Task is the fallback")],
    "spike":         [("task", 82, "Research spikes are typically tracked as Tasks in ADO"),
                      ("user story", 70, "Can also be framed as a time-boxed User Story")],
    "change request":[("user story", 80, "Change requests represent new scope — User Story is closest"),
                      ("task", 74, "Task if it is a discrete implementation unit")],
}


def _word_overlap_score(a: str, b: str) -> float:
    """0.0–1.0 Jaccard similarity on word sets."""
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _map_single_type(jira_type: str, count: int, ado_types: list[str], type_config: dict | None = None) -> dict:
    """Return the best ADO mapping for one Jira issue type with confidence + reason."""
    ado_lower_to_display = {t.lower(): t for t in ado_types}
    jira_key = jira_type.lower().strip()

    # 1. type_config.json — authoritative project-specific mapping
    if type_config:
        mapped = type_config.get(jira_type) or type_config.get(jira_key)
        if mapped and mapped.lower() in ado_lower_to_display:
            return {
                "jira": jira_type, "count": count,
                "ado": ado_lower_to_display[mapped.lower()],
                "confidence": 100,
                "reason": f"Configured mapping in type_config.json: {jira_type} → {mapped}",
            }

    # 2. Curated semantic table — fallback
    for ado_lower, confidence, reason in _SEMANTIC_EQUIVALENTS.get(jira_key, []):
        if ado_lower in ado_lower_to_display:
            return {
                "jira": jira_type, "count": count,
                "ado": ado_lower_to_display[ado_lower],
                "confidence": confidence, "reason": reason,
            }

    # 2. Exact case-insensitive match
    if jira_key in ado_lower_to_display:
        return {
            "jira": jira_type, "count": count,
            "ado": ado_lower_to_display[jira_key],
            "confidence": 99, "reason": "Exact name match",
        }

    # 3. Substring containment
    for ado_lower, ado_display in ado_lower_to_display.items():
        if jira_key in ado_lower or ado_lower in jira_key:
            score = 80 + round(10 * _word_overlap_score(jira_key, ado_lower))
            return {
                "jira": jira_type, "count": count,
                "ado": ado_display,
                "confidence": min(score, 89),
                "reason": f'ADO type "{ado_display}" shares key words with "{jira_type}"',
            }

    # 4. Word-overlap fuzzy match
    best_ado, best_score = max(
        ((d, _word_overlap_score(jira_key, l)) for l, d in ado_lower_to_display.items()),
        key=lambda x: x[1],
    )
    if best_score > 0.2:
        confidence = 55 + round(25 * best_score)
        return {
            "jira": jira_type, "count": count,
            "ado": best_ado,
            "confidence": min(confidence, 79),
            "reason": f'Partial name overlap with "{best_ado}" — review recommended',
        }

    # 5. Fallback to the most general available type
    fallback = next(
        (ado_lower_to_display[k] for k in ("task", "user story", "product backlog item") if k in ado_lower_to_display),
        ado_types[0] if ado_types else "Task",
    )
    return {
        "jira": jira_type, "count": count,
        "ado": fallback,
        "confidence": 55,
        "reason": (
            f'No direct ADO equivalent found for "{jira_type}". '
            f'"{fallback}" is the most general available type — human review required.'
        ),
    }


def build_type_mappings(by_type: list[dict], ado_types: list[str], type_config: dict | None = None) -> list[dict]:
    """Generate type mappings, preferring type_config.json over hardcoded semantic table."""
    return [_map_single_type(t["name"], t["count"], ado_types, type_config) for t in by_type]


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
    status_filter: list[str] | None = None,  # empty list = all statuses
    field_filter: list[str] | None = None,   # empty list = all fields
) -> dict:
    """
    Perform a pre-migration analysis and return a structured result dict.

    Returns dict with keys:
        total_issues, by_type, by_status, ado_available_types,
        type_gaps, user_gaps, attachment_count, comment_count,
        selected_statuses, selected_fields
    """
    if not jira_project_key:
        return {"error": "Select a Jira board to analyze."}

    # 1. Build JQL from project key + selected statuses
    parts = [f'project = "{jira_project_key}"']
    if status_filter:
        quoted = ', '.join(f'"{s}"' for s in status_filter)
        parts.append(f'status in ({quoted})')
    jql = ' AND '.join(parts) + ' ORDER BY created DESC'
    logger.info("[analysis] JQL: %s", jql)

    # 2. Fetch Jira issues
    issues = _fetch_issues(jira_url, jira_email, jira_token, jql)
    if not issues:
        return {
            "error": "No issues found for the selected board/statuses. Try selecting different statuses.",
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

        assignee_email = ((fields.get("assignee") or {}).get("emailAddress") or "").lower()
        if assignee_email:
            jira_emails.add(assignee_email)

        attachment_total += len(fields.get("attachment") or [])
        comment_total += (fields.get("comment") or {}).get("total", 0)

    # 4. Fetch ADO data (run both; they're independent)
    ado_types = _fetch_ado_work_item_types(ado_org, ado_project, ado_pat)
    ado_users = _fetch_ado_users(ado_org, ado_pat)
    type_config = _load_type_config()

    # 5. Compute gaps
    ado_types_lower = {t.lower() for t in ado_types}

    type_gaps = [
        {"jira_type": t, "has_ado_match": False}
        for t in type_counts
        if not _has_ado_match(t, ado_types_lower, type_config)
    ]

    # Only report user gaps when we successfully fetched ADO users
    user_gaps = (
        [
            {"jira_user": email, "found_in_ado": False}
            for email in sorted(jira_emails)
            if email not in ado_users
        ]
        if ado_users
        else []
    )

    by_type_list = [
        {"name": t, "count": c}
        for t, c in sorted(type_counts.items(), key=lambda x: -x[1])
    ]

    return {
        "total_issues": len(issues),
        "jql_used": jql,
        "by_type": by_type_list,
        "by_status": [
            {"name": s, "count": c}
            for s, c in sorted(status_counts.items(), key=lambda x: -x[1])
        ],
        "ado_available_types": ado_types,
        "type_mappings": build_type_mappings(by_type_list, ado_types, type_config),
        "type_gaps": type_gaps,
        "user_gaps": user_gaps,
        "attachment_count": attachment_total,
        "comment_count": comment_total,
        "selected_statuses": status_filter or [],
        "selected_fields": field_filter or [],
    }
