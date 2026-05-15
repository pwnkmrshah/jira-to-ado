#!/usr/bin/env python3
"""
fix_missing_dates.py

Backfills date fields in ADO work items that were migrated before date-field
mapping was added to the worker.

Field mapping
─────────────────────────────────────────────────────────────────────────────
ADO field                                  ← Jira source
─────────────────────────────────────────────────────────────────────────────
Microsoft.VSTS.Scheduling.StartDate        ← fields.created  (date only)
Microsoft.VSTS.Scheduling.TargetDate       ← fields.duedate  (date only, if set)
Custom.ActualCompletionDate                ← fields.resolutiondate (UTC datetime)
Custom.ActualStartDate                     ← fields.created  (UTC datetime)
─────────────────────────────────────────────────────────────────────────────

Skips:
  - Cards where StartDate already matches the Jira created date (idempotent).
  - Custom.ActualStartDate update is silently ignored when the ADO template
    does not include that field.

Usage:
  python3 jira_ado_copy/scripts/fix_missing_dates.py \\
      --project-key CSQA \\
      --ado-project "Embedded Refills Engineering" \\
      [--dry-run]

  Use --project-key ALL to repair every migrated project.
"""

import sys
import logging
import argparse
import concurrent.futures
from datetime import timezone
from dateutil.parser import parse as _parse_dt

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
# Date helpers
# ---------------------------------------------------------------------------

def _to_ado_dt(jira_ts: str) -> str:
    """Convert Jira ISO timestamp to ADO UTC ISO string."""
    return _parse_dt(jira_ts).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _to_ado_date(jira_ts: str) -> str:
    """Convert Jira ISO timestamp to date-only string (YYYY-MM-DD)."""
    return _parse_dt(jira_ts).strftime('%Y-%m-%d')


# ---------------------------------------------------------------------------
# ADO batch fetch helper
# ---------------------------------------------------------------------------

def _fetch_ado_items_batch(ado_ids: list[int], ado_client: AzureDevOpsClient) -> dict[int, dict]:
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

def _set_field(ado_client, ado_id, field_path, value, key, dry_run, silent_fail=False):
    """Update a single ADO field. Returns True on success."""
    if dry_run:
        return True
    try:
        ado_client.update_field(ado_id, field_path, value)
        return True
    except Exception as exc:
        if silent_fail:
            logger.debug(f"  {key}: {field_path} not available — {exc}")
        else:
            logger.warning(f"  {key}: failed to set {field_path}: {exc}")
        return False


def repair_project(
    project_key: str,
    ado_project: str,
    jira_instance: str,
    dry_run: bool,
) -> tuple[int, int]:
    ado_client  = AzureDevOpsClient(load_ado_config(), ado_project)
    mapping     = load_issue_mapping()
    jira_cfg    = load_jira_config(jira_instance)
    jira_client = JiraClient(jira_cfg)

    keys = sorted(k for k in mapping if k.startswith(f"{project_key}-"))
    if not keys:
        logger.warning(f"No mapping entries for project {project_key}")
        return 0, 0

    logger.info(f"Project {project_key}: {len(keys)} mapped issues")

    # Parallel Jira fetch
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
    ado_ids   = [int(mapping[k]) for k in keys]
    logger.info(f"  Fetching {len(ado_ids)} ADO items in batches …")
    ado_items = _fetch_ado_items_batch(ado_ids, ado_client)

    updated = 0

    for key in keys:
        ticket = jira_data.get(key)
        if ticket is None:
            continue

        jira_fields = ticket.get('fields') or {}
        ado_id      = int(mapping[key])
        ado_item    = ado_items.get(ado_id, {})
        ado_fields  = ado_item.get('fields') or {}
        changed     = False

        # 1. Current Start Date ← Jira created
        if created_ts := jira_fields.get('created'):
            ado_start = _to_ado_date(created_ts)
            current   = (ado_fields.get('Microsoft.VSTS.Scheduling.StartDate') or '')[:10]
            if current != ado_start:
                if dry_run:
                    logger.info(f"  {key}: would set StartDate = {ado_start} (was {current or 'empty'})")
                else:
                    if _set_field(ado_client, ado_id, '/fields/Microsoft.VSTS.Scheduling.StartDate',
                                  ado_start, key, dry_run):
                        logger.info(f"  {key}: StartDate = {ado_start}")
                        changed = True

        # 2. Target Date ← Jira duedate (only if not already set)
        if due := jira_fields.get('duedate'):
            current = (ado_fields.get('Microsoft.VSTS.Scheduling.TargetDate') or '')[:10]
            if current != due:
                if dry_run:
                    logger.info(f"  {key}: would set TargetDate = {due} (was {current or 'empty'})")
                else:
                    if _set_field(ado_client, ado_id, '/fields/Microsoft.VSTS.Scheduling.TargetDate',
                                  due, key, dry_run):
                        logger.info(f"  {key}: TargetDate = {due}")
                        changed = True

        # 3. Actual Completion Date ← Jira resolutiondate
        if res_ts := jira_fields.get('resolutiondate'):
            ado_res_dt  = _to_ado_dt(res_ts)
            current_raw = ado_fields.get('Custom.ActualCompletionDate') or ''
            # Compare date portion only to avoid sub-second drift
            if current_raw[:10] != ado_res_dt[:10]:
                if dry_run:
                    logger.info(f"  {key}: would set ActualCompletionDate = {ado_res_dt} (was {current_raw[:10] or 'empty'})")
                else:
                    if _set_field(ado_client, ado_id, '/fields/Custom.ActualCompletionDate',
                                  ado_res_dt, key, dry_run):
                        logger.info(f"  {key}: ActualCompletionDate = {ado_res_dt}")
                        changed = True

        # 4. Actual Start Date ← Jira created (silent-fail if field not in ADO template)
        if created_ts := jira_fields.get('created'):
            ado_created_dt = _to_ado_dt(created_ts)
            current_raw    = ado_fields.get('Custom.ActualStartDate') or ''
            if current_raw[:10] != ado_created_dt[:10]:
                if dry_run:
                    logger.info(f"  {key}: would set ActualStartDate = {ado_created_dt}")
                else:
                    _set_field(ado_client, ado_id, '/fields/Custom.ActualStartDate',
                               ado_created_dt, key, dry_run, silent_fail=True)

        if changed:
            updated += 1

    return len(keys), updated


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Backfill date fields in ADO work items from Jira")
    parser.add_argument("--project-key",   required=True,
                        help="Jira project key (e.g. CSQA) or ALL")
    parser.add_argument("--ado-project",   default="Embedded Refills Engineering")
    parser.add_argument("--jira-instance", default="healthfinch")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without making API calls")
    args = parser.parse_args()

    if args.dry_run:
        logger.info("=== DRY RUN — no changes will be made ===")

    if args.project_key.upper() == "ALL":
        mapping      = load_issue_mapping()
        project_keys = sorted({k.split("-")[0] for k in mapping})
        logger.info(f"Scanning all projects: {project_keys}")
    else:
        project_keys = [args.project_key.upper()]

    total_keys    = 0
    total_updated = 0

    for pk in project_keys:
        keys, updated = repair_project(pk, args.ado_project, args.jira_instance, args.dry_run)
        total_keys    += keys
        total_updated += updated
        verb = "would update" if args.dry_run else "updated"
        logger.info(f"  {pk}: {keys} items checked, {updated} {verb}")

    logger.info("=" * 60)
    verb = "would be updated" if args.dry_run else "updated"
    logger.info(f"Total: {total_keys} items — {total_updated} {verb}")


if __name__ == "__main__":
    main()
