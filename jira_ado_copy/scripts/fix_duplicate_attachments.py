#!/usr/bin/env python3
"""
fix_duplicate_attachments.py

Scans ADO work items (for a given Jira project key) and removes excess
AttachedFile relations caused by the migration worker running more than once.

Root cause: ADO attachment URLs are GUID-based and carry no filename in the URL,
so the original filename-in-URL dedup check in sync_attachments never matched.
Each migration re-run uploaded every attachment again under a fresh GUID.

Detection strategy:
  - Fetch the Jira issue's attachment list (ground truth count) in batches via JQL.
  - Fetch all ADO work items with relations in batches (200 per request).
  - If ADO AttachedFile count > Jira attachment count, the excess are duplicates.

Fix strategy:
  - Keep the FIRST N AttachedFile relations (oldest upload = original run).
  - Remove the rest (added by subsequent re-runs) in descending index order
    so earlier indices are not shifted during removal.

Batching:
  - Jira: JQL `issue in (KEY-1, KEY-2, ...)` fetches 100 issues per page.
  - ADO:  `/wit/workitems?ids=1,2,3,...&$expand=relations` fetches 200 per call.
  Combined this reduces API calls from O(N) to O(N/100), making all-board scans
  complete in seconds instead of 30+ minutes.

Usage:
  python3 jira_ado_copy/scripts/fix_duplicate_attachments.py \
      --project-key CSQA \
      --ado-project "Embedded Refills Engineering" \
      [--dry-run]

  Use --project-key ALL to scan every migrated project.
  --dry-run  Report what would be removed without making any changes.
"""

import sys
import json
import logging
import argparse
import requests
from pathlib import Path
from math import ceil
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from utilities.utils_ado import AzureDevOpsClient, load_ado_config
from utilities.utils_mapping import load_issue_mapping

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Batch fetchers
# ---------------------------------------------------------------------------

JIRA_THREADS = 20  # parallel per-issue Jira fetches
ADO_BATCH    = 200  # max work item IDs per ADO batch call


def _fetch_one_jira(jira_key: str, jira_base: str, jira_auth: tuple) -> tuple[str, int | None]:
    """Fetch attachment count for a single Jira issue. Returns (key, count|None)."""
    url = f"{jira_base}/rest/api/3/issue/{jira_key}?fields=attachment"
    try:
        r = requests.get(url, auth=jira_auth, timeout=15)
        r.raise_for_status()
        return jira_key, len(r.json().get("fields", {}).get("attachment") or [])
    except Exception as e:
        logger.error(f"  [{jira_key}] Jira fetch failed: {e}")
        return jira_key, None


def _fetch_jira_counts_parallel(jira_keys: list[str], jira_base: str,
                                 jira_auth: tuple) -> dict[str, int]:
    """
    Return {jira_key: attachment_count} using JIRA_THREADS parallel threads.
    Per-issue endpoint works for both active and archived Jira projects
    (unlike the JQL /search endpoint which returns 410 for archived projects).
    """
    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=JIRA_THREADS) as pool:
        futures = {
            pool.submit(_fetch_one_jira, key, jira_base, jira_auth): key
            for key in jira_keys
        }
        for future in as_completed(futures):
            key, count = future.result()
            if count is not None:
                counts[key] = count
    return counts


def _fetch_ado_items_batch(ado_ids: list[int], ado_client: AzureDevOpsClient) -> dict[int, dict]:
    """
    Return {ado_id: work_item} for all given IDs using ADO batch endpoint.
    Fetches relations expanded. Items that 404 are omitted.
    """
    items: dict[int, dict] = {}
    for start in range(0, len(ado_ids), ADO_BATCH):
        batch = ado_ids[start:start + ADO_BATCH]
        ids_str = ",".join(str(i) for i in batch)
        url = (
            f"{ado_client.organization_url}/_apis/wit/workitems"
            f"?ids={ids_str}&$expand=relations&api-version={ado_client.api_version}"
        )
        try:
            resp = ado_client.ado_api_call("GET", url)
            for item in (resp or {}).get("value", []):
                items[item["id"]] = item
        except Exception as e:
            logger.error(f"ADO batch fetch failed for batch starting {batch[0]}: {e}")
    return items


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _attached_file_indices(relations: list) -> list[int]:
    """Return the indices (in the full relations list) of AttachedFile relations."""
    return [i for i, r in enumerate(relations) if r.get("rel") == "AttachedFile"]


def _remove_relations(ado_client: AzureDevOpsClient, ado_id: int, indices: list[int]) -> bool:
    """
    Remove relations at the given indices via a single PATCH.
    Indices MUST be in descending order so earlier indices stay stable.
    """
    url = (
        f"{ado_client.organization_url}/{ado_client.project}"
        f"/_apis/wit/workitems/{ado_id}?api-version={ado_client.api_version}"
    )
    payload = [{"op": "remove", "path": f"/relations/{idx}"} for idx in indices]
    try:
        ado_client.ado_api_call("PATCH", url, payload)
        return True
    except Exception as e:
        logger.error(f"  [ADO {ado_id}] Failed to remove relations: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fix duplicate ADO attachments from migration re-runs")
    parser.add_argument("--project-key", required=True,
                        help="Jira project key (e.g. CSQA). Use ALL for all migrated items.")
    parser.add_argument("--ado-project", required=True,
                        help="ADO project name (e.g. 'Embedded Refills Engineering')")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report duplicates without removing anything.")
    args = parser.parse_args()

    # Load configs
    ado_config = load_ado_config()
    ado_client = AzureDevOpsClient(ado_config, args.ado_project)
    mapping    = load_issue_mapping()

    jira_cfg = json.loads(
        (Path(__file__).parent.parent.parent / "config" / "jira_config.json").read_text()
    )
    jira_base = jira_cfg["server"].rstrip("/")
    jira_auth = (jira_cfg["email"], jira_cfg["access_token"])

    # Scope
    if args.project_key.upper() == "ALL":
        scoped = mapping
    else:
        prefix = args.project_key.upper() + "-"
        scoped = {k: v for k, v in mapping.items() if k.startswith(prefix)}

    logger.info(
        f"Scanning {len(scoped)} items for project '{args.project_key}'"
        + (" [DRY RUN]" if args.dry_run else "")
    )

    jira_keys = sorted(scoped.keys())
    ado_ids   = [int(v) for v in scoped.values()]

    # --- Batch fetch ---
    logger.info(f"Fetching Jira attachment counts ({JIRA_THREADS} parallel threads)...")
    jira_counts = _fetch_jira_counts_parallel(jira_keys, jira_base, jira_auth)
    logger.info(f"  Got counts for {len(jira_counts)}/{len(jira_keys)} Jira issues")

    logger.info(f"Fetching ADO work items with relations in batches of {ADO_BATCH}...")
    ado_items = _fetch_ado_items_batch(ado_ids, ado_client)
    logger.info(f"  Got {len(ado_items)}/{len(ado_ids)} ADO items")

    # --- Analyse ---
    total_removed = 0
    fixed_items   = 0
    problem_items = []
    skipped       = 0

    for jira_key in jira_keys:
        ado_id = int(scoped[jira_key])

        jira_count = jira_counts.get(jira_key)
        if jira_count is None:
            logger.warning(f"  [{jira_key}] Jira count missing — skipping")
            skipped += 1
            continue

        if jira_count == 0:
            logger.debug(f"  [{jira_key}] No Jira attachments — skipping")
            continue

        item = ado_items.get(ado_id)
        if item is None:
            logger.warning(f"  [{jira_key}] ADO item {ado_id} not found — skipping")
            skipped += 1
            continue

        relations    = item.get("relations") or []
        attached_idx = _attached_file_indices(relations)
        ado_count    = len(attached_idx)

        if ado_count <= jira_count:
            logger.debug(f"  [{jira_key}] ADO {ado_id}: {ado_count}/{jira_count} — OK")
            continue

        excess_count = ado_count - jira_count
        to_remove    = sorted(attached_idx[jira_count:], reverse=True)

        logger.info(
            f"  [{jira_key}] ADO {ado_id}: {ado_count} attachments, "
            f"Jira has {jira_count} — removing {excess_count} duplicate(s)"
        )
        total_removed += excess_count

        if args.dry_run:
            problem_items.append({
                "jira_key": jira_key, "ado_id": ado_id,
                "jira_count": jira_count, "ado_count": ado_count,
                "to_remove_indices": to_remove,
            })
            continue

        ok = _remove_relations(ado_client, ado_id, to_remove)
        if ok:
            logger.info(f"  [{jira_key}] ADO {ado_id}: fixed ✓")
            fixed_items += 1
        else:
            problem_items.append({
                "jira_key": jira_key, "ado_id": ado_id,
                "error": "API call failed — see logs above",
            })

    # --- Summary ---
    print("\n" + "=" * 60)
    if args.dry_run:
        print(f"DRY RUN — project: {args.project_key}")
        print(f"  Items with excess attachments : {len(problem_items)}")
        print(f"  Total excess relations        : {total_removed}")
        if problem_items:
            print("\n  Would remove:")
            for p in problem_items:
                print(f"    {p['jira_key']} (ADO {p['ado_id']}): "
                      f"Jira={p['jira_count']}, ADO={p['ado_count']}, "
                      f"remove indices {p['to_remove_indices']}")
    else:
        print(f"FIX SUMMARY — project: {args.project_key}")
        print(f"  Items scanned            : {len(scoped)}")
        print(f"  Items fixed              : {fixed_items}")
        print(f"  Total attachments removed: {total_removed}")
        print(f"  Skipped (fetch errors)   : {skipped}")
        if problem_items:
            print(f"\n  FAILURES ({len(problem_items)}):")
            for p in problem_items:
                print(f"    {p['jira_key']} (ADO {p['ado_id']}): {p.get('error', 'unknown')}")
    print("=" * 60)


if __name__ == "__main__":
    main()
