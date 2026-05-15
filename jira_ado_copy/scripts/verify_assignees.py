#!/usr/bin/env python3
"""
verify_assignees.py

Verifies assignee correctness for migrated ADO work items.

Rules verified:
  1. Jira has assignee AND email is in ADO System.AssignedTo  → PASS (assignee field set)
  2. Jira has assignee AND name/email appears in ADO description → PASS (description fallback)
  3. Jira has assignee BUT neither condition above is true     → FAIL (missing assignee)
  4. Jira has no assignee                                      → SKIP (nothing expected)

Usage:
  python3 jira_ado_copy/scripts/verify_assignees.py --project-key CSQA
  python3 jira_ado_copy/scripts/verify_assignees.py --project-key ALL
"""

import sys
import logging
import argparse
import concurrent.futures
from pathlib import Path
from html.parser import HTMLParser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from utilities.utils_ado import AzureDevOpsClient, load_ado_config
from utilities.utils_mapping import load_issue_mapping
from utilities.utils_jira import JiraClient, load_jira_config

ADO_BATCH = 200
JIRA_WORKERS = 12


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []

    def handle_data(self, data):
        self._parts.append(data)

    def get_text(self):
        return " ".join(self._parts)


def _strip_html(html: str) -> str:
    """Return plain-text content of an HTML string."""
    if not html:
        return ""
    s = _HTMLStripper()
    try:
        s.feed(html)
        return s.get_text()
    except Exception:
        return html


def _fetch_ado_items_batch(ado_ids: list[int], ado_client: AzureDevOpsClient) -> dict[int, dict]:
    """Return {ado_id: work_item} via ADO batch endpoint."""
    items: dict[int, dict] = {}
    for start in range(0, len(ado_ids), ADO_BATCH):
        batch = ado_ids[start: start + ADO_BATCH]
        ids_str = ",".join(str(i) for i in batch)
        url = (
            f"{ado_client.organization_url}/_apis/wit/workitems"
            f"?ids={ids_str}&api-version={ado_client.api_version}"
        )
        try:
            resp = ado_client.ado_api_call("GET", url)
            for item in (resp or {}).get("value", []):
                items[item["id"]] = item
        except Exception as exc:
            logger.error(f"ADO batch fetch failed (start {batch[0]}): {exc}")
    return items


def _ado_assigned_to(ado_item: dict) -> str:
    """Return the uniqueName or displayName from System.AssignedTo, lowercased."""
    val = (ado_item.get("fields") or {}).get("System.AssignedTo") or ""
    if isinstance(val, dict):
        return (val.get("uniqueName") or val.get("displayName") or "").lower()
    return str(val).lower()


def _ado_description(ado_item: dict) -> str:
    """Return plain-text ADO description (lowercased) for substring checks."""
    raw = (ado_item.get("fields") or {}).get("System.Description") or ""
    return _strip_html(raw).lower()


# ---------------------------------------------------------------------------
# Per-item check
# ---------------------------------------------------------------------------

def check_item(
    key: str,
    ado_id: int,
    jira_client: JiraClient,
    ado_item: dict,
) -> dict:
    """
    Returns a result dict:
      status: 'pass' | 'fail' | 'skip' | 'error'
      reason: human-readable
    """
    try:
        ticket = jira_client.get_jira_issue(key)
    except Exception as e:
        return {"key": key, "ado_id": ado_id, "status": "error", "reason": f"Jira fetch error: {e}"}

    fields = (ticket.get("fields") or {})
    assignee_field = (fields.get("assignee") or {})
    jira_name = (assignee_field.get("displayName") or "").strip()
    jira_email = (assignee_field.get("emailAddress") or "").strip()

    # Rule 4: no Jira assignee → skip
    if not jira_name:
        return {"key": key, "ado_id": ado_id, "status": "skip", "reason": "No Jira assignee", "jira_name": "", "jira_email": ""}

    ado_assigned = _ado_assigned_to(ado_item)
    desc = _ado_description(ado_item)

    name_in_desc = jira_name.lower() in desc
    email_in_desc = bool(jira_email) and jira_email.lower() in desc
    email_in_assigned = bool(jira_email) and jira_email.lower() == ado_assigned

    # Rule 1: email matches System.AssignedTo
    if email_in_assigned:
        return {
            "key": key, "ado_id": ado_id, "status": "pass",
            "reason": f"Assignee '{jira_name}' set in System.AssignedTo",
            "jira_name": jira_name, "jira_email": jira_email,
            "ado_assigned": ado_assigned,
        }

    # Rule 2: name or email in description
    if name_in_desc or email_in_desc:
        return {
            "key": key, "ado_id": ado_id, "status": "pass",
            "reason": f"Assignee '{jira_name}' found in ADO description",
            "jira_name": jira_name, "jira_email": jira_email,
            "ado_assigned": ado_assigned,
        }

    # Rule 3: missing
    return {
        "key": key, "ado_id": ado_id, "status": "fail",
        "reason": (
            f"Assignee '{jira_name}'"
            + (f" <{jira_email}>" if jira_email else "")
            + " missing from both System.AssignedTo and description"
        ),
        "jira_name": jira_name, "jira_email": jira_email,
        "ado_assigned": ado_assigned,
    }


# ---------------------------------------------------------------------------
# Per-project verification
# ---------------------------------------------------------------------------

def verify_project(
    project_key: str,
    ado_project: str,
    jira_instance: str,
) -> dict:
    mapping = load_issue_mapping()
    ado_config = load_ado_config()
    jira_config = load_jira_config(jira_instance)

    ado_client = AzureDevOpsClient(
        config=ado_config,
        project=ado_project,
    )
    jira_client = JiraClient(
        config=jira_config,
    )

    # Filter mapping to this project
    keys = [k for k in mapping if project_key == "ALL" or k.startswith(f"{project_key}-")]
    if not keys:
        logger.warning(f"No mapped keys for project {project_key}")
        return {}

    # Batch-fetch ADO items
    ado_ids = [int(mapping[k]) for k in keys]
    logger.info(f"[{project_key}] Fetching {len(ado_ids)} ADO items...")
    ado_items = _fetch_ado_items_batch(ado_ids, ado_client)

    # Parallel Jira fetches + checks
    logger.info(f"[{project_key}] Checking assignees for {len(keys)} items...")
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=JIRA_WORKERS) as pool:
        futures = {
            pool.submit(
                check_item,
                key,
                int(mapping[key]),
                jira_client,
                ado_items.get(int(mapping[key]), {}),
            ): key
            for key in keys
        }
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            res = fut.result()
            results.append(res)
            if i % 50 == 0:
                logger.info(f"  {i}/{len(keys)} checked...")

    return {r["key"]: r for r in results}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(all_results: dict, project_key: str):
    # Determine projects present
    projects = {}
    for key, res in all_results.items():
        pk = key.split("-")[0]
        projects.setdefault(pk, []).append(res)

    total_pass = total_fail = total_skip = total_error = 0
    fails_by_project = {}

    for pk, items in sorted(projects.items()):
        n_pass = sum(1 for r in items if r["status"] == "pass")
        n_fail = sum(1 for r in items if r["status"] == "fail")
        n_skip = sum(1 for r in items if r["status"] == "skip")
        n_err  = sum(1 for r in items if r["status"] == "error")
        total_pass += n_pass; total_fail += n_fail
        total_skip += n_skip; total_error += n_err
        fails_by_project[pk] = [r for r in items if r["status"] == "fail"]

        status_icon = "✅" if n_fail == 0 else "❌"
        logger.info(
            f"  {status_icon} {pk}: {n_pass} pass | {n_fail} fail | {n_skip} skip (no assignee) | {n_err} error"
        )

    logger.info("")
    logger.info(f"TOTAL: {total_pass} pass | {total_fail} fail | {total_skip} skip | {total_error} error")

    if total_fail > 0:
        logger.info("")
        logger.info("=== FAILING ITEMS ===")
        for pk, fails in sorted(fails_by_project.items()):
            if not fails:
                continue
            logger.info(f"\n--- {pk} ({len(fails)} failures) ---")
            for r in sorted(fails, key=lambda x: x["key"]):
                logger.info(
                    f"  ❌ {r['key']} (ADO #{r['ado_id']}): {r['reason']}"
                )
    else:
        logger.info("All items with Jira assignees have correct assignee data in ADO ✅")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Verify assignees for migrated ADO work items")
    parser.add_argument("--project-key", default="CSQA",
                        help="Jira project key to verify (or ALL for all projects)")
    parser.add_argument("--ado-project", default="Embedded Refills Engineering",
                        help="ADO project name")
    parser.add_argument("--jira-instance", default="healthfinch",
                        help="Jira instance key in jira_config.json")
    args = parser.parse_args()

    logger.info(f"Verifying assignees for: {args.project_key}")

    if args.project_key == "ALL":
        all_project_keys = ["CGTM", "CSQA", "HIVE", "ICE", "PD", "PM", "RRR", "XPM"]
    else:
        all_project_keys = [args.project_key]

    all_results = {}
    for pk in all_project_keys:
        results = verify_project(pk, args.ado_project, args.jira_instance)
        all_results.update(results)

    logger.info("")
    logger.info("=== ASSIGNEE VERIFICATION REPORT ===")
    print_report(all_results, args.project_key)


if __name__ == "__main__":
    main()
