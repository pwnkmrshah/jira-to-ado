"""migration_gap_analysis.py — Strict source-of-truth gap analysis: Jira filter → ADO board.

Identifies ALL missed, partial, and misaligned migrations by comparing every card in a
Jira filter against its expected ADO landing zone.

Classification:
  ❌ MISSED      — Key present in Jira filter, NOT found anywhere in ADO
  ⚠️  WRONG AREA — Key found in ADO but NOT under the expected AreaPath (e.g. root or other board)
  ⚠️  NO AREA    — Key found in ADO under expected board but AreaPath is root/blank
  ✅ CORRECT     — Key found in ADO under expected AreaPath

Usage:
  python3 migration_gap_analysis.py \\
    --jira-filter 11525 \\
    --ado-board "Operations" \\
    --ado-project "Embedded Refills Engineering" \\
    --jira-instance healthfinch

  # Also generate CSV
  python3 migration_gap_analysis.py \\
    --jira-filter 11525 \\
    --ado-board "Operations" \\
    --ado-project "Embedded Refills Engineering" \\
    --csv migration_gap_op.csv

Output:
  - Console: 4 clearly separated sections (✅ / ❌ / ⚠️  / ⚠️)
  - CSV:     JiraKey, ExistsInADO, ADOId, AreaPath, MissingAreaPath, NeedsMigration, Notes
"""

import sys
import csv
import re
import time
import argparse
import logging
import requests
from pathlib import Path
from collections import defaultdict
from datetime import datetime

sys.path.append(str(Path(__file__).parent.parent.parent / "utilities"))
from utils_ado import AzureDevOpsClient, load_ado_config
from utils_jira import JiraClient, load_jira_config

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Jira key pattern inside ADO titles, e.g.  [OP-123]
JIRA_KEY_RE = re.compile(r'\[([A-Z][A-Z0-9]*-\d+)\]')
# jiraKey tag pattern inside ADO System.Tags, e.g.  jiraKey=OP-123
JIRA_TAG_RE = re.compile(r'jiraKey=([A-Z][A-Z0-9]*-\d+)', re.IGNORECASE)

PAGE_SIZE = 100          # Jira page size (100 is safe under all instances)
ADO_BATCH  = 200         # ADO detail-fetch batch size (avoids URL-length limits)
RETRY_WAIT = 2           # seconds to wait before retry on transient error
MAX_RETRIES = 3


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — JIRA: paginated filter fetch (source of truth)
# ══════════════════════════════════════════════════════════════════════════════

def _get_filter_jql(jira_client, filter_id: str) -> str:
    """Resolve a Jira filter ID to its JQL string."""
    url = f"{jira_client.server}/rest/api/3/filter/{filter_id}"
    resp = jira_client.jira_api_call("GET", url)
    if not resp or "jql" not in resp:
        raise RuntimeError(f"Cannot resolve Jira filter {filter_id}: {resp}")
    jql = resp["jql"]
    logger.info(f"Filter {filter_id} JQL: {jql}")
    return jql


def fetch_all_jira_keys(jira_client, filter_id: str) -> list[str]:
    """
    Paginate through ALL issues returned by a Jira filter using cursor-based
    nextPageToken pagination.

    The Jira Cloud /rest/api/3/search/jql endpoint switched from startAt-based
    to cursor-based pagination.  The existing get_filter_items() hard-caps at
    maxResults=1000 (one page) and silently drops everything beyond that.
    This function follows nextPageToken until isLast=True.

    Returns sorted list of Jira issue keys.
    """
    jql = _get_filter_jql(jira_client, filter_id)

    keys = []
    page = 1
    # Use the newer /jql endpoint that supports cursor-based pagination
    url = f"{jira_client.server}/rest/api/3/search/jql"
    params = {
        "jql": jql,
        "maxResults": PAGE_SIZE,
        "fields": "key",
    }

    logger.info(f"Paginating Jira filter {filter_id} (cursor-based) …")
    while True:
        resp = None
        for attempt in range(MAX_RETRIES):
            resp = requests.get(
                url,
                auth=(jira_client.email, jira_client.access_token),
                params=params,
            )
            if resp.status_code == 200:
                resp = resp.json()
                break
            logger.warning(f"  Page {page}: HTTP {resp.status_code}, attempt {attempt+1}, retrying …")
            time.sleep(RETRY_WAIT)
            resp = None

        if not resp or "issues" not in resp:
            logger.error(f"  Page {page}: bad response; aborting pagination")
            break

        page_issues = resp["issues"]
        for issue in page_issues:
            keys.append(issue.get("key") or issue.get("id"))

        is_last = resp.get("isLast", True)
        next_token = resp.get("nextPageToken")
        logger.info(f"  Page {page}: {len(page_issues)} cards  cumulative={len(keys)}  isLast={is_last}")

        if is_last or not next_token:
            break

        # Advance cursor
        params["nextPageToken"] = next_token
        page += 1

    logger.info(f"✅ Jira source of truth: {len(keys)} cards from filter {filter_id}")
    return sorted(keys)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2a — ADO: strict board fetch (expected landing zone)
# ══════════════════════════════════════════════════════════════════════════════

def _batch_fetch_ado_details(ado_client, ids: list[int], fields: str) -> list[dict]:
    """Fetch ADO work item details in ADO_BATCH-sized chunks. Returns list of item dicts."""
    all_items = []
    for i in range(0, len(ids), ADO_BATCH):
        batch = ids[i:i + ADO_BATCH]
        ids_str = ",".join(str(x) for x in batch)
        url = (
            f"{ado_client.organization_url}/_apis/wit/workitems"
            f"?ids={ids_str}&fields={fields}&api-version={ado_client.api_version}"
        )
        for attempt in range(MAX_RETRIES):
            resp = ado_client.ado_api_call("GET", url)
            if resp is not None:
                break
            logger.warning(f"  ADO batch {i//ADO_BATCH+1}: attempt {attempt+1} failed, retrying …")
            time.sleep(RETRY_WAIT)
        if resp and "value" in resp:
            all_items.extend(resp["value"])
        else:
            logger.warning(f"  ADO batch {i//ADO_BATCH+1}: no data returned")
    return all_items


def _extract_jira_key_from_item(item: dict) -> str | None:
    """
    Extract the canonical Jira key from an ADO work item.
    Priority: last [KEY-XX] token in title → jiraKey=… tag
    Uses last-token logic for hierarchical titles like '[PARENT-1] [CHILD-2] Summary'.
    """
    fields = item.get("fields", {})
    title = fields.get("System.Title", "") or ""
    tags  = fields.get("System.Tags", "")  or ""

    # Title extraction (last token wins for hierarchy)
    tokens = JIRA_KEY_RE.findall(title)
    if tokens:
        return tokens[-1]

    # Fallback: tag extraction
    tag_matches = JIRA_TAG_RE.findall(tags)
    if tag_matches:
        return tag_matches[-1]

    return None


def fetch_ado_board_items(ado_client, ado_project: str, board_name: str) -> dict:
    """
    Strictly fetch all ADO work items that live UNDER the board's AreaPath.
    No cross-board leakage — scoped to:  '<project>\\<board_name>'

    Returns dict:  jira_key -> {"ado_id": int, "area_path": str, "title": str}
    """
    full_area = f"{ado_project}\\{board_name}"
    wiql = (
        f"SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.AreaPath] UNDER '{full_area}' "
        f"ORDER BY [System.Id]"
    )
    url = f"{ado_client.organization_url}/{ado_client.project}/_apis/wit/wiql?api-version=7.0"

    logger.info(f"Fetching ALL items under AreaPath '{full_area}' …")
    resp = ado_client.ado_api_call("POST", url, {"query": wiql})
    if not resp or "workItems" not in resp:
        logger.error("ADO board WIQL returned no data")
        return {}

    item_refs = resp["workItems"]
    logger.info(f"  WIQL returned {len(item_refs)} items; fetching details …")
    ids = [w["id"] for w in item_refs]

    fields = "System.Id,System.Title,System.AreaPath,System.Tags"
    raw_items = _batch_fetch_ado_details(ado_client, ids, fields)

    result = {}
    for item in raw_items:
        jira_key = _extract_jira_key_from_item(item)
        if not jira_key:
            continue  # ADO item has no Jira key — skip (not a migrated card)
        f = item.get("fields", {})
        result[jira_key] = {
            "ado_id":    item["id"],
            "area_path": f.get("System.AreaPath", ""),
            "title":     f.get("System.Title", ""),
        }

    logger.info(f"✅ ADO board '{board_name}': {len(result)} unique Jira keys extracted")
    return result


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2b — ADO: prefix-wide search (catch misplaced cards)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_ado_prefix_items(ado_client, prefix: str) -> dict:
    """
    Search ALL ADO work items whose title contains '[<PREFIX>-' regardless of area path.
    Used to detect cards that were migrated but landed in the wrong board/area.

    Returns dict:  jira_key -> {"ado_id": int, "area_path": str, "title": str}
    """
    wiql = (
        f"SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.Title] CONTAINS '[{prefix}-'"
    )
    url = f"{ado_client.organization_url}/{ado_client.project}/_apis/wit/wiql?api-version=7.0"

    logger.info(f"Scanning entire ADO project for '[{prefix}-' prefix in titles …")
    resp = ado_client.ado_api_call("POST", url, {"query": wiql})
    if not resp or "workItems" not in resp:
        logger.warning("ADO prefix WIQL returned no data")
        return {}

    item_refs = resp["workItems"]
    logger.info(f"  Prefix scan found {len(item_refs)} candidate items; fetching details …")
    ids = [w["id"] for w in item_refs]

    fields = "System.Id,System.Title,System.AreaPath,System.Tags"
    raw_items = _batch_fetch_ado_details(ado_client, ids, fields)

    result = {}
    for item in raw_items:
        jira_key = _extract_jira_key_from_item(item)
        if not jira_key:
            continue
        f = item.get("fields", {})
        result[jira_key] = {
            "ado_id":    item["id"],
            "area_path": f.get("System.AreaPath", ""),
            "title":     f.get("System.Title", ""),
        }

    logger.info(f"✅ Prefix '{prefix}': {len(result)} unique Jira keys found across ALL areas")
    return result


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — STRICT COMPARISON
# ══════════════════════════════════════════════════════════════════════════════

def classify_cards(
    jira_keys:    list[str],
    board_items:  dict,   # keys strictly under expected AreaPath
    prefix_items: dict,   # keys found anywhere in ADO
    expected_area: str,   # e.g. "Embedded Refills Engineering\Operations"
) -> dict:
    """
    Classify every Jira card into one of four states.

    Returns dict with keys: correct, wrong_area, no_area_path, missed
    Each value is a list of record dicts.
    """
    correct       = []
    wrong_area    = []
    no_area_path  = []
    missed        = []

    for key in jira_keys:
        in_board  = key in board_items
        in_prefix = key in prefix_items

        if in_board:
            item = board_items[key]
            area = item["area_path"]
            if area.strip().lower() == expected_area.lower():
                correct.append({**item, "jira_key": key, "note": "Correctly migrated"})
            else:
                # Under board area path but not exactly the expected area (sub-path or root)
                no_area_path.append({**item, "jira_key": key,
                                     "note": f"Under board but area='{area}'"})
        elif in_prefix:
            item = prefix_items[key]
            area = item["area_path"]
            wrong_area.append({**item, "jira_key": key,
                                "note": f"Migrated but in wrong area: '{area}'"})
        else:
            missed.append({"jira_key": key, "ado_id": None, "area_path": None,
                           "title": None, "note": "Not found anywhere in ADO"})

    return {
        "correct":      correct,
        "wrong_area":   wrong_area,
        "no_area_path": no_area_path,
        "missed":       missed,
    }


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

W = 110  # console width

def _section(title: str):
    print(f"\n{'─'*W}")
    print(f"  {title}")
    print(f"{'─'*W}")


def print_report(
    jira_keys:     list[str],
    prefix_items:  dict,
    board_items:   dict,
    classified:    dict,
    filter_id:     str,
    board_name:    str,
    ado_project:   str,
    expected_area: str,
):
    total_jira   = len(jira_keys)
    total_ado_board  = len(board_items)
    total_ado_prefix = len(prefix_items)
    n_correct    = len(classified["correct"])
    n_wrong      = len(classified["wrong_area"])
    n_no_area    = len(classified["no_area_path"])
    n_missed     = len(classified["missed"])

    print(f"\n{'═'*W}")
    print(f"  MIGRATION GAP ANALYSIS")
    print(f"  Jira Filter : {filter_id}")
    print(f"  ADO Board   : {board_name}  ({ado_project})")
    print(f"  Expected AP : {expected_area}")
    print(f"  Generated   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*W}")

    # ── Overview ──────────────────────────────────────────────────────────
    _section("📊  OVERVIEW")
    print(f"  {'Jira filter cards (source of truth):':<45} {total_jira:>6}")
    print(f"  {'ADO cards under expected board area:':<45} {total_ado_board:>6}")
    print(f"  {'ADO cards with OP- prefix (any area):':<45} {total_ado_prefix:>6}")
    print()
    pct = lambda n: f"{100*n/total_jira:.1f}%" if total_jira else "—"
    print(f"  {'✅  Correctly migrated:':<45} {n_correct:>6}  ({pct(n_correct)})")
    print(f"  {'⚠️   In ADO but wrong/sub area path:':<45} {n_no_area:>6}  ({pct(n_no_area)})")
    print(f"  {'⚠️   In ADO but completely wrong board:':<45} {n_wrong:>6}  ({pct(n_wrong)})")
    print(f"  {'❌  Not found anywhere in ADO:':<45} {n_missed:>6}  ({pct(n_missed)})")

    # ── Missed ────────────────────────────────────────────────────────────
    if classified["missed"]:
        _section(f"❌  MISSED MIGRATION  ({n_missed} cards — need full migration)")
        print(f"  {'Jira Key':<15}  {'Note'}")
        print(f"  {'-'*100}")
        for r in sorted(classified["missed"], key=lambda x: x["jira_key"]):
            print(f"  {r['jira_key']:<15}  {r['note']}")

    # ── Wrong area ────────────────────────────────────────────────────────
    if classified["wrong_area"]:
        _section(f"⚠️   WRONG BOARD / AREA  ({n_wrong} cards — migrated but misrouted)")
        print(f"  {'Jira Key':<15}  {'ADO ID':<10}  {'Current Area Path':<60}  {'Note'}")
        print(f"  {'-'*100}")
        for r in sorted(classified["wrong_area"], key=lambda x: x["jira_key"]):
            area = (r["area_path"] or "")[:57]
            print(f"  {r['jira_key']:<15}  {r['ado_id']:<10}  {area:<60}  {r['note'][:30]}")

    # ── No/sub area path ─────────────────────────────────────────────────
    if classified["no_area_path"]:
        _section(f"⚠️   PARTIAL MIGRATION — WRONG/MISSING AREA PATH  ({n_no_area} cards)")
        print(f"  {'Jira Key':<15}  {'ADO ID':<10}  {'Current Area Path'}")
        print(f"  {'-'*100}")
        for r in sorted(classified["no_area_path"], key=lambda x: x["jira_key"]):
            print(f"  {r['jira_key']:<15}  {r['ado_id']:<10}  {r['area_path']}")

    # ── Correct ───────────────────────────────────────────────────────────
    _section(f"✅  CORRECTLY MIGRATED  ({n_correct} cards)")

    # Area-path distribution
    area_dist: dict[str, int] = defaultdict(int)
    for r in classified["correct"]:
        area_dist[r["area_path"]] += 1
    print(f"  {'Area Path':<70}  {'Count':>6}  {'%':>6}")
    print(f"  {'-'*90}")
    for ap, cnt in sorted(area_dist.items(), key=lambda x: -x[1]):
        pct_ap = f"{100*cnt/n_correct:.1f}%" if n_correct else "—"
        print(f"  {ap:<70}  {cnt:>6}  {pct_ap:>6}")

    print(f"\n{'═'*W}\n")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — CSV export
# ══════════════════════════════════════════════════════════════════════════════

def write_csv(classified: dict, path: str):
    """
    Write only problem cards to CSV (missed, wrong area, missing area path).
    Correctly migrated cards are excluded — this file is an action list only.
    Columns: JiraKey, ExistsInADO, ADOId, AreaPath, MissingAreaPath, NeedsMigration, Notes
    """
    rows = []

    # "correct" cards are intentionally excluded — only issues are logged

    for r in classified["no_area_path"]:
        rows.append({
            "JiraKey": r["jira_key"],
            "ExistsInADO": "Yes",
            "ADOId": r["ado_id"],
            "AreaPath": r["area_path"],
            "MissingAreaPath": "Yes",
            "NeedsMigration": "No",
            "Notes": r["note"],
        })

    for r in classified["wrong_area"]:
        rows.append({
            "JiraKey": r["jira_key"],
            "ExistsInADO": "Yes",
            "ADOId": r["ado_id"],
            "AreaPath": r["area_path"],
            "MissingAreaPath": "Yes",
            "NeedsMigration": "No",
            "Notes": r["note"],
        })

    for r in classified["missed"]:
        rows.append({
            "JiraKey": r["jira_key"],
            "ExistsInADO": "No",
            "ADOId": "",
            "AreaPath": "",
            "MissingAreaPath": "Yes",
            "NeedsMigration": "Yes",
            "Notes": r["note"],
        })

    # Sort by key
    rows.sort(key=lambda x: x["JiraKey"])

    fieldnames = ["JiraKey", "ExistsInADO", "ADOId", "AreaPath",
                  "MissingAreaPath", "NeedsMigration", "Notes"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    if rows:
        logger.info(f"CSV written → {path}  ({len(rows)} problem cards logged)")
    else:
        logger.info(f"CSV written → {path}  (0 rows — all cards correctly migrated ✅)")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 6 — Re-migration suggestions
# ══════════════════════════════════════════════════════════════════════════════

def print_remediation(classified: dict, board_name: str, ado_project: str):
    missed  = [r["jira_key"] for r in classified["missed"]]
    wrong   = [r["jira_key"] for r in classified["wrong_area"]]
    no_area = [r["jira_key"] for r in classified["no_area_path"]]

    print(f"{'═'*W}")
    print("  REMEDIATION COMMANDS")
    print(f"{'═'*W}")

    if missed:
        keys_arg = ",".join(missed)
        print(f"\n# ❌ Migrate {len(missed)} completely missing cards:")
        print(f"  python3 worker_jira_to_ado_copy.py \\")
        print(f"    --jira-keys \"{keys_arg}\" \\")
        print(f"    --ado-board \"{board_name}\" \\")
        print(f"    --ado-project \"{ado_project}\"")

    if wrong or no_area:
        keys_arg = ",".join(sorted(set(wrong + no_area)))
        print(f"\n# ⚠️  Re-migrate {len(wrong)+len(no_area)} cards with incorrect area path:")
        print(f"  python3 worker_jira_to_ado_copy.py \\")
        print(f"    --jira-keys \"{keys_arg}\" \\")
        print(f"    --ado-board \"{board_name}\" \\")
        print(f"    --ado-project \"{ado_project}\" \\")
        print(f"    --force-create")

    if not missed and not wrong and not no_area:
        print("\n  ✅ No remediation needed — all cards correctly migrated!")

    print(f"\n{'═'*W}\n")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Strict migration gap analysis: Jira filter vs ADO board"
    )
    parser.add_argument("--jira-filter", required=True,
                        help="Jira filter ID (e.g. 11525)")
    parser.add_argument("--ado-board", required=True,
                        help='ADO board/area name (e.g. "Operations")')
    parser.add_argument("--ado-project", required=True,
                        help='ADO project (e.g. "Embedded Refills Engineering")')
    parser.add_argument("--jira-instance", default="healthfinch",
                        help="Jira instance slug (default: healthfinch)")
    parser.add_argument("--prefix",
                        help="Jira key prefix to use for ADO-wide scan (auto-detected from filter if omitted)")
    parser.add_argument("--csv", metavar="FILE",
                        help="Optional CSV output path (e.g. gap_report.csv)")
    args = parser.parse_args()

    ado_project   = args.ado_project
    board_name    = args.ado_board
    expected_area = f"{ado_project}\\{board_name}"

    # ── Load clients ──────────────────────────────────────────────────────
    ado_config   = load_ado_config()
    ado_client   = AzureDevOpsClient(ado_config, ado_project)
    jira_config  = load_jira_config(args.jira_instance)
    jira_client  = JiraClient(jira_config)

    # ── Step 1: Jira source of truth (paginated) ──────────────────────────
    jira_keys = fetch_all_jira_keys(jira_client, args.jira_filter)
    if not jira_keys:
        logger.error("No Jira cards returned — check filter ID and credentials")
        sys.exit(1)

    # Auto-detect prefix from first key if not supplied
    prefix = args.prefix
    if not prefix:
        first_key = jira_keys[0]
        prefix = first_key.split("-")[0]
        logger.info(f"Auto-detected Jira key prefix: {prefix}")

    # ── Step 2a: ADO board — strict AreaPath fetch ────────────────────────
    board_items = fetch_ado_board_items(ado_client, ado_project, board_name)

    # ── Step 2b: ADO-wide prefix scan (catch misplaced cards) ─────────────
    prefix_items = fetch_ado_prefix_items(ado_client, prefix)

    # ── Step 3: Classify ──────────────────────────────────────────────────
    classified = classify_cards(jira_keys, board_items, prefix_items, expected_area)

    # ── Step 4: Console report ────────────────────────────────────────────
    print_report(
        jira_keys, prefix_items, board_items, classified,
        args.jira_filter, board_name, ado_project, expected_area,
    )

    # ── Step 5: CSV ───────────────────────────────────────────────────────
    if args.csv:
        write_csv(classified, args.csv)

    # ── Step 6: Remediation ───────────────────────────────────────────────
    print_remediation(classified, board_name, ado_project)


if __name__ == "__main__":
    main()
