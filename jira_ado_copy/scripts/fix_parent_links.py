"""
fix_parent_links.py — Add missing parent links for migrated Jira items.

For each Jira item that has a parent field but no corresponding ADO relation:
  - If parent was migrated → add System.LinkTypes.Hierarchy-Reverse (real ADO link)
  - If parent not migrated → add Jira browse URL as a Hyperlink fallback

Usage:
  python3 jira_ado_copy/scripts/fix_parent_links.py \
    --project-key ALL \
    --ado-project "Embedded Refills Engineering"
"""

import sys
import time
import logging
import argparse
import concurrent.futures
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from utilities.utils_ado import AzureDevOpsClient, load_ado_config
from utilities.utils_mapping import load_issue_mapping
from utilities.utils_jira import JiraClient, load_jira_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

JIRA_INSTANCE = 'healthfinch'
ADO_PARENT_REL = 'System.LinkTypes.Hierarchy-Reverse'


def _ado_call_with_retry(fn, *args, retries=3, **kwargs):
    """Call fn(*args, **kwargs) with exponential backoff on transient errors."""
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1 and ('503' in str(e) or '429' in str(e)):
                wait = 2 ** attempt * 5
                logging.warning(f"Transient error ({e}), retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise


def _has_parent_link(ado_item: dict, parent_key: str, parent_ado_id: int | None) -> bool:
    """Return True if the ADO item already has a parent link (real or fallback)."""
    rels = ado_item.get('relations') or []
    for r in rels:
        rel_type = r.get('rel', '')
        url = r.get('url', '')
        if 'Hierarchy-Reverse' in rel_type:
            return True
        if r.get('rel') == 'Hyperlink' and parent_key in url:
            return True
        if parent_ado_id and str(parent_ado_id) in url:
            return True
    return False


def fix_item(key: str, ado_id: int, jira_fields: dict,
             ado_client: AzureDevOpsClient, mapping: dict) -> str:
    """
    Add a parent link for one work item if missing.
    Returns: 'real', 'fallback', 'skipped', or 'error'
    """
    parent_info = jira_fields.get('parent') or {}
    parent_key  = parent_info.get('key')
    if not parent_key:
        return 'skipped'

    ado_item = None
    try:
        ado_item = _ado_call_with_retry(ado_client.get_work_item_full, ado_id)
    except Exception as e:
        logging.warning(f"  {key}: could not fetch ADO item ({e}), skipping")
        return 'error'
    if ado_item is None:
        logging.warning(f"  {key}: ADO item returned None, skipping")
        return 'error'
    parent_ado_id = int(mapping[parent_key]) if parent_key in mapping else None

    if _has_parent_link(ado_item, parent_key, parent_ado_id):
        return 'skipped'

    if parent_ado_id:
        # Real ADO hierarchy link
        try:
            ado_client.add_work_item_link(ado_id, parent_ado_id, ADO_PARENT_REL)
            logging.info(f"  {key}: parent link → ADO #{parent_ado_id} ({parent_key})")
            return 'real'
        except Exception as e:
            logging.warning(f"  {key}: could not add real parent link: {e}")

    # Fallback: Jira hyperlink
    jira_url = f'https://{JIRA_INSTANCE}.atlassian.net/browse/{parent_key}'
    try:
        ado_client.add_hyperlink(ado_id, jira_url)
        logging.info(f"  {key}: parent fallback hyperlink → {jira_url}")
        return 'fallback'
    except Exception as e:
        logging.warning(f"  {key}: could not add fallback hyperlink: {e}")
        return 'error'


def main():
    parser = argparse.ArgumentParser(description='Fix missing parent links in ADO')
    parser.add_argument('--project-key', default='ALL',
                        help='Jira project prefix (e.g. HIVE) or ALL')
    parser.add_argument('--ado-project', required=True)
    args = parser.parse_args()

    ado_client  = AzureDevOpsClient(load_ado_config(), args.ado_project)
    jira_client = JiraClient(load_jira_config(JIRA_INSTANCE))
    mapping     = load_issue_mapping()

    prefix = args.project_key.upper()
    keys = sorted(k for k in mapping if prefix == 'ALL' or k.startswith(prefix + '-'))
    logging.info(f"Fetching {len(keys)} Jira tickets in parallel...")

    # Parallel Jira fetch
    def fetch_jira(key):
        try:    return key, jira_client.get_jira_issue(key)
        except: return key, None

    jira_fields_map = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        for key, ticket in ex.map(fetch_jira, keys):
            if ticket:
                jira_fields_map[key] = ticket.get('fields', {})

    # Filter to items that have a parent field
    with_parent = [k for k in keys if (jira_fields_map.get(k) or {}).get('parent')]
    logging.info(f"{len(with_parent)} items have a Jira parent. Checking ADO links...")

    counts = {'real': 0, 'fallback': 0, 'skipped': 0, 'error': 0}
    for i, key in enumerate(with_parent, 1):
        ado_id = int(mapping[key])
        logging.info(f"[{i}/{len(with_parent)}] {key} (ADO #{ado_id})")
        result = fix_item(key, ado_id, jira_fields_map[key], ado_client, mapping)
        counts[result] += 1

    print(f"\nDone.")
    print(f"  Real ADO links added : {counts['real']}")
    print(f"  Fallback hyperlinks  : {counts['fallback']}")
    print(f"  Already correct      : {counts['skipped']}")
    print(f"  Errors               : {counts['error']}")


if __name__ == '__main__':
    main()
