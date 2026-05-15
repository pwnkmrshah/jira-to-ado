"""
fix_missing_attachments.py — Re-upload Jira attachments that are missing from ADO.

For each work item where ADO has fewer AttachedFile relations than Jira has
attachments, download the missing files from Jira and upload them to ADO.

Usage:
  python3 jira_ado_copy/scripts/fix_missing_attachments.py \
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


def _ado_attached_filenames(ado_item: dict) -> set:
    """Return set of filenames already attached in ADO (normalised to lowercase)."""
    return {
        rel['attributes']['name'].lower()
        for rel in (ado_item.get('relations') or [])
        if rel.get('rel') == 'AttachedFile'
    }


def fix_item_attachments(key: str, ado_id: int, jira_attachments: list,
                         ado_client: AzureDevOpsClient, jira_client: JiraClient) -> int:
    """
    Upload any Jira attachments not yet present in the ADO work item.
    Returns the number of attachments added.
    """
    ado_item = ado_client.get_work_item_full(ado_id)
    existing_fnames = _ado_attached_filenames(ado_item)

    added = 0
    for att in jira_attachments:
        filename   = att['filename']
        content_id = att['id']
        created_at = att.get('created', '')
        author_email = (att.get('author') or {}).get('emailAddress', '')

        if filename.lower() in existing_fnames:
            logging.debug(f"  {key}: attachment '{filename}' already present, skipping")
            continue

        # Download from Jira
        jira_content_url = att['content']   # e.g. .../rest/api/3/attachment/content/{id}
        try:
            content = jira_client.get_attachment(jira_content_url)
        except Exception as e:
            logging.warning(f"  {key}: could not download '{filename}' from Jira: {e}")
            continue
        if content is None:
            logging.warning(f"  {key}: Jira returned None for '{filename}'")
            continue

        # Upload to ADO
        try:
            ado_att_resp = ado_client.create_attachment(filename, content)
        except Exception as e:
            logging.warning(f"  {key}: ADO upload failed for '{filename}': {e}")
            continue
        if not ado_att_resp:
            logging.warning(f"  {key}: ADO upload returned None for '{filename}'")
            continue

        ado_url = ado_att_resp.get('url')
        if not ado_url:
            logging.warning(f"  {key}: ADO attachment response missing URL for '{filename}'")
            continue

        # Link to work item
        try:
            ado_client.add_attachment(ado_id, ado_url, author_email, created_at, filename=filename)
            logging.info(f"  {key}: attached '{filename}'")
            added += 1
        except Exception as e:
            logging.warning(f"  {key}: could not link '{filename}' to ADO item: {e}")

    return added


def main():
    parser = argparse.ArgumentParser(description='Re-upload missing Jira attachments to ADO')
    parser.add_argument('--project-key', default='ALL',
                        help='Jira project prefix (e.g. HIVE) or ALL')
    parser.add_argument('--ado-project', required=True)
    args = parser.parse_args()

    ado_client  = AzureDevOpsClient(load_ado_config(), args.ado_project)
    jira_client = JiraClient(load_jira_config('healthfinch'))
    mapping     = load_issue_mapping()

    prefix = args.project_key.upper()
    keys = sorted(k for k in mapping if prefix == 'ALL' or k.startswith(prefix + '-'))
    logging.info(f"Fetching {len(keys)} Jira tickets to find attachment gaps...")

    # Parallel Jira fetch — only items with attachments matter
    def fetch_jira(key):
        try:    return key, jira_client.get_jira_issue(key)
        except: return key, None

    has_attachments = {}   # key → [attachment objects]
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        for key, ticket in ex.map(fetch_jira, keys):
            if ticket:
                atts = ticket.get('fields', {}).get('attachment') or []
                if atts:
                    has_attachments[key] = atts

    logging.info(f"{len(has_attachments)} items have Jira attachments. Checking ADO counts...")

    # Find which items are missing attachments
    def needs_repair(key):
        ado_id = int(mapping[key])
        try:
            ado_item = _ado_call_with_retry(ado_client.get_work_item_full, ado_id)
            existing = _ado_attached_filenames(ado_item)
            jira_fnames = {a['filename'].lower() for a in has_attachments[key]}
            missing = jira_fnames - existing
            return key, missing
        except Exception as e:
            logging.warning(f"Could not check ADO item for {key}: {e}")
            return key, set()

    items_to_fix = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        for key, missing in ex.map(needs_repair, list(has_attachments)):
            if missing:
                items_to_fix[key] = missing

    logging.info(f"{len(items_to_fix)} items need attachment repair. Uploading...")

    total_added = 0
    for i, (key, missing_fnames) in enumerate(sorted(items_to_fix.items()), 1):
        ado_id = int(mapping[key])
        logging.info(f"[{i}/{len(items_to_fix)}] {key} (ADO #{ado_id}) — "
                     f"{len(missing_fnames)} missing: {', '.join(sorted(missing_fnames))}")

        # Filter jira attachments to only those missing in ADO
        jira_atts_missing = [
            a for a in has_attachments[key]
            if a['filename'].lower() in missing_fnames
        ]
        added = fix_item_attachments(key, ado_id, jira_atts_missing, ado_client, jira_client)
        total_added += added

    print(f"\nDone. Added {total_added} attachment(s) across {len(items_to_fix)} item(s).")


if __name__ == '__main__':
    main()
