"""
fix_broken_images.py — Replace Jira attachment content URLs in ADO comments with ADO URLs.

In migrated comments, <img> tags pointing to
  https://healthfinch.atlassian.net/rest/api/3/attachment/content/{id}
are broken because Jira requires authentication. This script rewrites those
src attributes to the corresponding ADO attachment download URL.

Usage:
  python3 jira_ado_copy/scripts/fix_broken_images.py \
    --project-key ALL \
    --ado-project "Embedded Refills Engineering"
"""

import sys
import re
import json
import time
import requests
import logging
import argparse
import concurrent.futures
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from utilities.utils_ado import AzureDevOpsClient, load_ado_config
from utilities.utils_mapping import load_issue_mapping
from utilities.utils_jira import JiraClient, load_jira_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

JIRA_CONTENT_PATTERN = re.compile(
    r'https://healthfinch\.atlassian\.net/rest/api/3/attachment/content/(\d+)'
)


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


def _patch_comment(patch_url: str, new_text: str, auth: tuple) -> int:
    """PATCH an ADO comment with plain application/json (not json-patch).
    Returns the HTTP status code."""
    r = requests.patch(
        patch_url, auth=auth,
        headers={'Content-Type': 'application/json'},
        data=json.dumps({"text": new_text})
    )
    if r.status_code not in (200, 201):
        logging.debug(f"    PATCH status={r.status_code}, body={r.text[:200]}")
    return r.status_code


def _post_corrective_comment(base_url: str, new_text: str, auth: tuple) -> bool:
    """Post a new comment with fixed content.
    Used as fallback when the original comment owner cannot be impersonated."""
    post_url = f"{base_url}?api-version=7.0-preview.3"
    header_note = (
        "<p><em>[Auto-fix: original comment contained broken image links. "
        "Fixed content below.]</em></p>"
    )
    r = requests.post(
        post_url, auth=auth,
        headers={'Content-Type': 'application/json'},
        data=json.dumps({"text": header_note + new_text})
    )
    if r.status_code not in (200, 201):
        logging.debug(f"    POST status={r.status_code}, body={r.text[:200]}")
        return False
    return True


def fix_item(key: str, ado_id: int, ado_client: AzureDevOpsClient,
             jira_client: JiraClient, auth: tuple) -> int:
    """Fix broken Jira image URLs in comments for one work item. Returns count fixed."""
    # Build Jira content ID → ADO attachment URL map
    ticket = jira_client.get_jira_issue(key)
    if not ticket:
        return 0

    jira_attachments = ticket.get('fields', {}).get('attachment') or []
    if not jira_attachments:
        return 0

    item = None
    try:
        item = _ado_call_with_retry(ado_client.get_work_item_full, ado_id)
    except Exception as e:
        logging.warning(f"  {key}: could not fetch ADO item ({e}), skipping")
        return 0
    ado_fname_to_url = {
        rel['attributes']['name']: rel['url']
        for rel in (item.get('relations') or [])
        if rel.get('rel') == 'AttachedFile'
    }

    # content_id → ADO URL
    content_id_to_ado = {
        str(a['id']): ado_fname_to_url[a['filename']]
        for a in jira_attachments
        if a['filename'] in ado_fname_to_url
    }
    if not content_id_to_ado:
        return 0

    # Fetch comments
    comments_url = (
        f"{ado_client.organization_url}/{ado_client.project}"
        f"/_apis/wit/workItems/{ado_id}/comments?api-version=7.0-preview.3"
    )
    try:
        resp = _ado_call_with_retry(ado_client.ado_api_call, "GET", comments_url) or {}
    except Exception as e:
        logging.warning(f"  {key}: could not fetch comments ({e}), skipping")
        return 0

    fixed = 0
    for c in resp.get('comments', []):
        text = c.get('text', '') or ''
        if 'atlassian.net/rest/api/3/attachment/content' not in text:
            continue

        new_text = JIRA_CONTENT_PATTERN.sub(
            lambda m: content_id_to_ado.get(m.group(1), m.group(0)),
            text
        )
        if new_text == text:
            continue

        comment_base_url = (
            f"{ado_client.organization_url}/{ado_client.project}"
            f"/_apis/wit/workItems/{ado_id}/comments"
        )
        patch_url = f"{comment_base_url}/{c['id']}?api-version=7.0-preview.3"
        status = _patch_comment(patch_url, new_text, auth)
        if status in (200, 201):
            logging.info(f"  {key} comment {c['id']}: images fixed")
            fixed += 1
        elif status == 400:
            # Not the comment owner — post a corrective comment with fixed content
            if _post_corrective_comment(comment_base_url, new_text, auth):
                logging.info(f"  {key} comment {c['id']}: images fixed (new comment posted)")
                fixed += 1
            else:
                logging.warning(f"  {key} comment {c['id']}: corrective post failed")
        else:
            logging.warning(f"  {key} comment {c['id']}: PATCH failed (status={status})")

    return fixed


def main():
    parser = argparse.ArgumentParser(description='Fix broken Jira image URLs in ADO comments')
    parser.add_argument('--project-key', default='ALL',
                        help='Jira project prefix (e.g. HIVE) or ALL')
    parser.add_argument('--ado-project', required=True)
    args = parser.parse_args()

    ado_client  = AzureDevOpsClient(load_ado_config(), args.ado_project)
    jira_client = JiraClient(load_jira_config('healthfinch'))
    mapping     = load_issue_mapping()
    auth        = (ado_client.username, ado_client.access_token)

    prefix = args.project_key.upper()
    keys = sorted(k for k in mapping if prefix == 'ALL' or k.startswith(prefix + '-'))
    logging.info(f"Scanning {len(keys)} items for broken images...")

    # Fast scan — parallel comment fetch to find items that need fixing
    def has_broken(key):
        ado_id = int(mapping[key])
        try:
            url = (f"{ado_client.organization_url}/{ado_client.project}"
                   f"/_apis/wit/workItems/{ado_id}/comments?api-version=7.0-preview.3")
            resp = ado_client.ado_api_call("GET", url) or {}
            return key, any(
                'atlassian.net/rest/api/3/attachment/content' in (c.get('text') or '')
                for c in resp.get('comments', [])
            )
        except Exception:
            return key, False

    needs_fix = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        for key, broken in ex.map(has_broken, keys):
            if broken:
                needs_fix.append(key)

    logging.info(f"Found {len(needs_fix)} items with broken images. Fixing...")

    total_comments_fixed = 0
    for i, key in enumerate(sorted(needs_fix), 1):
        ado_id = int(mapping[key])
        logging.info(f"[{i}/{len(needs_fix)}] {key} (ADO #{ado_id})")
        total_comments_fixed += fix_item(key, ado_id, ado_client, jira_client, auth)

    print(f"\nDone. Fixed {total_comments_fixed} comment(s) across {len(needs_fix)} item(s).")


if __name__ == '__main__':
    main()
