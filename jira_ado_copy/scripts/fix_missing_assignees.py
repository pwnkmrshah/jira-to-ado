#!/usr/bin/env python3
"""
fix_missing_assignees.py

Repairs ADO work items where the Jira assignee was not written to the ADO
description during migration.

Root cause: The migration worker only wrote the assignee when `emailAddress`
was present in the Jira response.  Archived Jira projects do not return
`emailAddress`, so the entire assignee block was skipped.

Fix strategy:
  - For each mapped issue in the target project:
      1. Fetch the Jira ticket (individual-issue endpoint works for archived projects).
      2. Check if the Jira assignee name or email already appears in the ADO description.
      3. If not, append  <b>Assignee:</b> {displayName}  (or include email when
         available) to the ADO description.
  - When a valid email is available, also attempt to set System.AssignedTo.

Usage:
  python3 jira_ado_copy/scripts/fix_missing_assignees.py \\
      --project-key CSQA \\
      --ado-project "Embedded Refills Engineering" \\
      [--dry-run]

  Use --project-key ALL to repair every migrated project.
"""

import sys
import logging
import argparse
import concurrent.futures

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

sys.path.insert(0, ".")

from utilities.utils_ado import AzureDevOpsClient, load_ado_config
from utilities.utils_mapping import load_issue_mapping
from utilities.utils_jira import JiraClient, load_jira_config

ADO_BATCH = 200


# ---------------------------------------------------------------------------
# ADO batch fetch helper
# ---------------------------------------------------------------------------

def _fetch_ado_items_batch(ado_ids: list[int], ado_client: AzureDevOpsClient) -> dict[int, dict]:
    """Return {ado_id: work_item} using the ADO batch work-items endpoint."""
    items: dict[int, dict] = {}
    for start in range(0, len(ado_ids), ADO_BATCH):
        batch   = ado_ids[start : start + ADO_BATCH]
        ids_str = ",".join(str(i) for i in batch)
        url     = (
            f"{ado_client.organization_url}/_apis/wit/workitems"
            f"?ids={ids_str}&api-version={ado_client.api_version}"
        )
        try:
            resp = ado_client.ado_api_call("GET", url)
            for item in (resp or {}).get("value", []):
                items[item["id"]] = item
        except Exception as exc:
            logger.error(f"ADO batch fetch failed (starting {batch[0]}): {exc}")
    return items


# ---------------------------------------------------------------------------
# Core repair logic
# ---------------------------------------------------------------------------

def _needs_repair(jira_name: str, jira_email: str, desc: str, ado_assigned: str) -> bool:
    """Return True if the assignee is missing from both ADO description and AssignedTo field."""
    if not jira_name:
        return False  # no Jira assignee → nothing to write

    name_in_desc  = jira_name.lower() in (desc or "").lower()
    email_in_desc = jira_email.lower() in (desc or "").lower() if jira_email else False

    if ado_assigned:
        # Pass if email matches AssignedTo OR name/email already in description
        return not (
            (jira_email and jira_email.lower() == ado_assigned.lower())
            or name_in_desc
            or email_in_desc
        )
    else:
        return not (name_in_desc or email_in_desc)


def repair_project(
    project_key: str,
    ado_project: str,
    jira_instance: str,
    dry_run: bool,
) -> tuple[int, int]:
    """Repair all mapped issues for *project_key*. Returns (checked, repaired)."""

    ado_client  = AzureDevOpsClient(load_ado_config(), ado_project)
    mapping     = load_issue_mapping()
    jira_cfg    = load_jira_config(jira_instance)
    jira_client = JiraClient(jira_cfg)

    keys = sorted(k for k in mapping if k.startswith(f"{project_key}-"))
    if not keys:
        logger.warning(f"No mapping entries found for project {project_key}")
        return 0, 0

    logger.info(f"Project {project_key}: {len(keys)} mapped issues")

    # Parallel Jira fetch (individual-issue endpoint — works for archived projects)
    def _fetch(key):
        try:
            return key, jira_client.get_jira_issue(key)
        except Exception as exc:
            logger.warning(f"  Jira fetch failed for {key}: {exc}")
            return key, None

    logger.info("  Fetching Jira tickets in parallel …")
    jira_data: dict[str, dict | None] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        for key, ticket in pool.map(_fetch, keys):
            jira_data[key] = ticket

    # ADO batch fetch
    ado_ids  = [int(mapping[k]) for k in keys]
    logger.info(f"  Fetching {len(ado_ids)} ADO items in batches …")
    ado_items = _fetch_ado_items_batch(ado_ids, ado_client)

    # Evaluate and repair
    checked  = 0
    repaired = 0

    for key in keys:
        ticket = jira_data.get(key)
        if ticket is None:
            continue

        assignee_field = (ticket.get("fields") or {}).get("assignee") or {}
        jira_name  = assignee_field.get("displayName", "")
        jira_email = assignee_field.get("emailAddress", "")

        if not jira_name:
            continue  # no Jira assignee → skip

        checked += 1
        ado_id   = int(mapping[key])
        ado_item = ado_items.get(ado_id, {})
        desc     = (ado_item.get("fields") or {}).get("System.Description") or ""
        assigned = (ado_item.get("fields") or {}).get("System.AssignedTo") or ""
        if isinstance(assigned, dict):
            assigned = assigned.get("uniqueName", "") or assigned.get("displayName", "")

        if not _needs_repair(jira_name, jira_email, desc, assigned):
            logger.debug(f"  {key}: OK (assignee already present)")
            continue

        # Build the line to append
        assignee_line = (
            f"<b>Assignee:</b> {jira_name} ({jira_email})"
            if jira_email
            else f"<b>Assignee:</b> {jira_name}"
        )

        logger.info(f"  {key} (ADO #{ado_id}): {'' if dry_run else 'repairing '}missing assignee → {jira_name!r}")

        if not dry_run:
            try:
                ado_client.append_description(ado_id, assignee_line)
                repaired += 1
            except Exception as exc:
                logger.error(f"  {key}: failed to append description: {exc}")
                continue

            # Also attempt System.AssignedTo when email is available
            if jira_email:
                try:
                    ado_client.update_field(ado_id, "/fields/System.AssignedTo", jira_email)
                    logger.info(f"    {key}: also set System.AssignedTo = {jira_email}")
                except Exception:
                    logger.info(f"    {key}: System.AssignedTo not set (user not in ADO) — description fallback used")
        else:
            repaired += 1  # count as "would repair" in dry-run

    return checked, repaired


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Repair missing assignees in ADO work items")
    parser.add_argument("--project-key",  required=True,
                        help="Jira project key (e.g. CSQA) or ALL")
    parser.add_argument("--ado-project",  default="Embedded Refills Engineering",
                        help="ADO project name")
    parser.add_argument("--jira-instance", default="healthfinch",
                        help="Jira instance name (used to build the server URL)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be changed without making any API calls")
    args = parser.parse_args()

    if args.dry_run:
        logger.info("=== DRY RUN — no changes will be made ===")

    if args.project_key.upper() == "ALL":
        mapping = load_issue_mapping()
        project_keys = sorted({k.split("-")[0] for k in mapping})
        logger.info(f"Scanning all projects: {project_keys}")
    else:
        project_keys = [args.project_key.upper()]

    total_checked  = 0
    total_repaired = 0

    for pk in project_keys:
        checked, repaired = repair_project(pk, args.ado_project, args.jira_instance, args.dry_run)
        total_checked  += checked
        total_repaired += repaired
        verb = "would repair" if args.dry_run else "repaired"
        logger.info(f"  {pk}: {checked} assignees checked, {repaired} {verb}")

    logger.info("=" * 60)
    verb = "would be repaired" if args.dry_run else "repaired"
    logger.info(f"Total: {total_checked} cards with Jira assignees — {total_repaired} {verb}")


if __name__ == "__main__":
    main()
