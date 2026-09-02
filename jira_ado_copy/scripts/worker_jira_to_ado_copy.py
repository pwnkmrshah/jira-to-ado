import sys
import json
import argparse
import re
import logging
from urllib.parse import urlparse, parse_qs, unquote
from datetime import timezone
from dateutil.parser import parse as _parse_dt

from pathlib import Path

# Add the utilities directory to the path to import utils_ado
sys.path.append(str(Path(__file__).parent.parent.parent / "utilities"))
from utils_ado import AzureDevOpsClient, load_ado_config, WorkItemTypeDisabledError
from utils_jira import JiraClient, load_jira_config
from utils_mapping import load_issue_mapping, save_issue_mapping

# Pattern to detect Jira issue keys (e.g., DATA-1011, SUST-123)
JIRA_KEY_PATTERN = re.compile(r'^[A-Z][A-Z0-9]*-\d+$')

logging.basicConfig(
    filename='worker_jira_to_ado_copy.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Also emit to stdout so progress is visible in the terminal
logging.getLogger().addHandler(logging.StreamHandler())


def is_jira_key(value: str) -> bool:
    """Check if value matches Jira issue key pattern (e.g., DATA-1011, SUST-123).
    
    Returns True if value looks like a Jira key, False otherwise.
    """
    if not value:
        return False
    return bool(JIRA_KEY_PATTERN.match(value.upper().strip()))

# Matches Jira attachment REST URLs so we can rewrite them to ADO URLs in comments
JIRA_CONTENT_PATTERN = re.compile(
    r'https://healthfinch\.atlassian\.net/rest/api/3/attachment/content/(\d+)'
)
JIRA_COMMENT_MARKER_PATTERN = re.compile(r'JiraCommentId:(\d+)')

# Fallback type order when the primary type is disabled in the target ADO project (VS403074).
# The worker will try each in turn until one succeeds.
_TYPE_FALLBACKS = ['User Story', 'Bug', 'Feature', 'Epic', 'Product Request', 'Problem']

# ---------------------------------------------------------------------------
# Failed-issue tracking
# ---------------------------------------------------------------------------

FAILED_PATH = Path(__file__).parent.parent.parent / 'config' / 'failed_issues.json'


def load_failed_issues() -> dict:
    """Return {jira_key: reason} from the failed issues file."""
    if FAILED_PATH.exists():
        try:
            return json.loads(FAILED_PATH.read_text())
        except Exception:
            pass
    return {}


def save_failed_issue(jira_key: str, reason: str, ado_id: int = None):
    """Append/update a failed issue entry and persist to disk."""
    from datetime import datetime
    data = load_failed_issues()
    entry: dict = {
        "reason": reason,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if ado_id:
        entry["ado_id"] = ado_id
    # Preserve an existing ado_id when overwriting an in-progress marker
    existing = data.get(jira_key)
    if isinstance(existing, dict) and existing.get("ado_id") and not ado_id:
        entry["ado_id"] = existing["ado_id"]
    data[jira_key] = entry
    FAILED_PATH.parent.mkdir(parents=True, exist_ok=True)
    FAILED_PATH.write_text(json.dumps(data, indent=2))
    logging.info(f"[failed_issues] Recorded failure for {jira_key}: {reason}")


def mark_issue_started(jira_key: str):
    """Write an 'in-progress' marker to failed_issues.json before processing.

    If the process is killed before clear_failed_issue() is called, this entry
    remains on disk so the item can be detected and retried with --retry-failed.
    """
    save_failed_issue(jira_key, "interrupted — process killed before completion")


def clear_failed_issue(jira_key: str):
    """Remove a jira_key from the failed issues file once it succeeds."""
    data = load_failed_issues()
    if jira_key in data:
        del data[jira_key]
        FAILED_PATH.write_text(json.dumps(data, indent=2))


def print_run_summary(succeeded: list, failed_items: list, base_cmd: str):
    """Print a human-readable migration summary and a ready-to-use retry command."""
    total = len(succeeded) + len(failed_items)
    print("\n" + "=" * 60)
    print(f"[summary] Run complete — {total} processed: "
          f"{len(succeeded)} succeeded, {len(failed_items)} failed/missed")
    if failed_items:
        keys_str = ','.join(failed_items)
        script_rel = Path(__file__).name
        print(f"[summary] Missed/failed keys: {keys_str}")
        print(f"[summary] To retry, run:\n"
              f"  python3 jira_ado_copy/scripts/{script_rel} "
              f"{base_cmd} --jira-keys \"{keys_str}\"")
        print(f"[summary] Or replay all entries from {FAILED_PATH.name}:\n"
              f"  python3 jira_ado_copy/scripts/{script_rel} "
              f"{base_cmd} --retry-failed")
    else:
        print("[summary] ✅ All items migrated successfully!")
    print("[summary] ℹ️  PARENT/CHILD LINKS: Items were created with Jira URL hyperlinks")
    print("[summary]    for any parents not yet in ADO. To add real ADO parent/child links")
    print("[summary]    after all items are created, re-run this command with --jira-keys")
    print("[summary]    for any subtasks that had missing parents.")
    print("=" * 60 + "\n")

# Maps (Jira link type name, direction) -> ADO relation type
# direction is 'outward' (this item acts on the linked one) or 'inward' (linked acts on this)
JIRA_TO_ADO_LINK_TYPES = {
    ('Blocks',    'outward'): 'System.LinkTypes.Dependency-Forward',   # this item blocks linked
    ('Blocks',    'inward'):  'System.LinkTypes.Dependency-Reverse',   # this item is blocked by linked
    ('Cloners',   'outward'): 'System.LinkTypes.Duplicate-Forward',
    ('Cloners',   'inward'):  'System.LinkTypes.Duplicate-Reverse',
    ('Duplicate', 'outward'): 'System.LinkTypes.Duplicate-Forward',
    ('Duplicate', 'inward'):  'System.LinkTypes.Duplicate-Reverse',
    ('Relates',   'outward'): 'System.LinkTypes.Related',
    ('Relates',   'inward'):  'System.LinkTypes.Related',
}

# Cache: tracks project names already set up this run so we only call
# ensure_area_path / ensure_team / configure_team_area once per project.
_BOARD_SETUP_DONE: set = set()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config(filename: str):
    path = Path(__file__).parent.parent.parent / "config" / filename
    try:
        return json.loads(path.read_text())
    except Exception as e:
        logging.error(f"[load_config] Could not read {filename}: {e}")
        return None


# ---------------------------------------------------------------------------
# Idempotency: find existing ADO work item for a Jira key
# ---------------------------------------------------------------------------

def find_existing_ado_work_item(jira_key: str, ado_client: AzureDevOpsClient, mapping: dict):
    """Return an existing ADO work item ID for *jira_key*, or None.

    Steps:
    1. Check local persistent mapping.
    2. If mapped item no longer exists in ADO, fall through to tag search.
    3. Search ADO via title if tag search returns nothing.
    """
    # Step 1: local mapping
    if jira_key in mapping:
        ado_id = mapping[jira_key]
        if ado_id is None:
            # Sentinel: pre-marked as not yet migrated — skip all ADO searches
            return None
        logging.debug(f"[find_existing_ado_work_item] {jira_key} in local mapping -> {ado_id}")
        if ado_client.item_exists(ado_id):
            logging.info(f"[DUPLICATE PREVENTED] Existing mapping found for {jira_key} -> {ado_id}")
            return ado_id
        logging.warning(
            f"[find_existing_ado_work_item] Mapped item {ado_id} for {jira_key} no longer exists "
            f"-- falling back to tag search"
        )

    # Step 2: ADO tag search
    ado_id = ado_client.find_work_item_by_jira_key(jira_key)
    if ado_id:
        save_issue_mapping(jira_key, ado_id)
        mapping[jira_key] = ado_id
        logging.info(f"[DUPLICATE PREVENTED] Found existing ADO item via tag search: {jira_key} -> {ado_id}")
        return ado_id

    # Step 3: Title search fallback (catches items that predate JiraKey tagging)
    result = ado_client.bulk_fetch_jira_key_mapping([jira_key])
    if jira_key in result:
        ado_id = result[jira_key]
        save_issue_mapping(jira_key, ado_id)
        mapping[jira_key] = ado_id
        logging.info(f"[DUPLICATE PREVENTED] Found existing ADO item via title search: {jira_key} -> {ado_id}")
        return ado_id

    return None


# ---------------------------------------------------------------------------
# Field parsing
# ---------------------------------------------------------------------------

def build_parsed_fields(jira_ticket: dict, type_config: dict, state_config: dict):
    """Parse a Jira ticket dict and return the core ADO field values."""
    jira_key = jira_ticket['key']

    parent_key = jira_ticket['fields'].get('parent', {}).get('key')
    if parent_key:
        title = f'[{parent_key}] [{jira_key}] {jira_ticket["fields"]["summary"]}'
    else:
        title = f'[{jira_key}] {jira_ticket["fields"]["summary"]}'

    assignee_field = jira_ticket['fields'].get('assignee') or {}
    assignee_email = assignee_field.get('emailAddress', '')
    assignee_name  = assignee_field.get('displayName', '')
    # Use email when available; fall back to display name for archived projects
    assignee = assignee_email or assignee_name or None

    state     = (jira_ticket['fields'].get('status') or {}).get('name', '')
    jira_type = (jira_ticket['fields'].get('issuetype') or {}).get('name', '')
    description = (jira_ticket.get('renderedFields') or {}).get('description') or ''

    # Append customer info to description when present
    customers = jira_ticket['fields'].get('customfield_10907')
    if customers:
        try:
            description += '<p><b>Customers:</b></p>'
            customers_content = jira_ticket['fields']['customfield_10907']['content'][0]['content']
            for customer in customers_content:
                description += f'{customer["text"]}<br />'
        except Exception:
            pass

    if (ado_type := type_config.get(jira_type)) is None:
        ado_type = type_config.get('Default')

    if (ado_state := state_config.get(state)) is None:
        ado_state = state_config.get('Default')

    return title, assignee, ado_type, ado_state, description


# ---------------------------------------------------------------------------
# Sync helpers  (all idempotent -- safe to call on both create and update paths)
# ---------------------------------------------------------------------------

def sync_custom_fields(ado_id: int, jira_ticket: dict, ado_client: AzureDevOpsClient,
                       custom_fields: list):
    """Apply custom field updates to an ADO work item.

    Custom field PATCH ops use 'add' which overwrites the field, so repeated
    calls are idempotent for scalar fields.
    Each field update is isolated so one failure doesn't abort the others.
    """
    for y in custom_fields:
        if y['jira_field'] not in jira_ticket['fields']:
            continue
        field = jira_ticket['fields'][y['jira_field']]
        if field is None:
            continue  # skip null values — ADO rejects them with 400

        try:
            if isinstance(field, (list, dict, tuple)):
                for z in field:
                    if y['jira_field'] == 'customfield_10845':   # Issue Environment
                        match z[y['jira_field_name']]:
                            case 'Staging':       environment = 'STAGING'
                            case 'Customer Test': environment = 'CERT/TEST'
                            case 'Production':    environment = 'PROD'
                            case _:               environment = z[y['jira_field_name']]
                        ado_client.update_field(ado_id, y['ado_field'], environment)

                    elif y['jira_field'] == 'customfield_10003':  # Impediment (Blocked)
                        if z[y['jira_field_name']] is not None:
                            ado_client.update_field(ado_id, y['ado_field'], 'Yes')

                    else:
                        val = z[y['jira_field_name']] if y['jira_field_name'] else z
                        if val is not None:
                            ado_client.update_field(ado_id, y['ado_field'], val)
            else:
                ado_client.update_field(ado_id, y['ado_field'], field)
        except Exception as e:
            logging.warning(
                f"[sync_custom_fields] Skipping field {y['ado_field']} on {ado_id}: {e}"
            )


def sync_attachments(ado_id: int, jira_ticket: dict, jira_client: JiraClient,
                     ado_client: AzureDevOpsClient, skip_attachments: bool = False) -> dict:
    """Upload Jira attachments and link only new ones to the ADO work item.

    Deduplication: checks the 'name' attribute stored on each existing
    AttachedFile relation.  Falls back to a URL substring check for relations
    created before the 'name' attribute was introduced.

    If skip_attachments=True, skips all attachment uploads (useful for debugging timeout issues).
    Returns an attachment_dict mapping filename -> ADO attachment URL.
    """
    if skip_attachments:
        logging.info(f"[sync_attachments] Skipping all attachment uploads (--skip-attachments flag)")
        return {}
    def _attachment_name_from_relation(rel: dict) -> str:
        attrs = rel.get('attributes') or {}
        if attrs.get('name'):
            return attrs['name']
        rel_url = rel.get('url', '')
        try:
            parsed = urlparse(rel_url)
            qs = parse_qs(parsed.query)
            if qs.get('fileName'):
                return unquote(qs['fileName'][0])
            basename = unquote(parsed.path.rsplit('/', 1)[-1])
            return basename if basename and basename != 'attachments' else ''
        except Exception:
            return ''

    existing_item = ado_client.get_work_item_full(ado_id)
    existing_rels = (existing_item or {}).get('relations') or []
    existing_attached = [r for r in existing_rels if r.get('rel') == 'AttachedFile']

    existing_by_name = {}
    for rel in existing_attached:
        name = _attachment_name_from_relation(rel)
        if name and name not in existing_by_name:
            existing_by_name[name] = rel.get('url', '')

    existing_names = set(existing_by_name.keys())
    attachment_dict = dict(existing_by_name)

    for i in jira_ticket['fields']['attachment']:
        filename   = i['filename']
        # If this filename is already linked, reuse its URL for image rewrite and skip upload.
        if filename in existing_names:
            existing_url = existing_by_name.get(filename, '')
            if existing_url:
                attachment_dict[filename] = existing_url
                attachment_dict[str(i['id'])] = existing_url
            logging.debug(f"[sync_attachments] Skipping duplicate attachment upload/link: {filename}")
            continue

        try:
            attachment = jira_client.get_attachment(i['content'])
            response = ado_client.create_attachment(filename, attachment)
            if response is None:
                logging.warning(f"[sync_attachments] create_attachment returned None for {filename}")
                continue
        except Exception as e:
            logging.warning(f"[sync_attachments] Failed to upload attachment '{filename}': {str(e)} — skipping this file")
            continue

        ado_url = response['url']
        attachment_dict[filename] = ado_url
        # Also index by Jira content ID so sync_comments can rewrite inline image URLs
        attachment_dict[str(i['id'])] = ado_url

        author_email = (
            i['author']['emailAddress']
            if i.get('author') and 'emailAddress' in i['author']
            else None
        )
        ado_client.add_attachment(ado_id, ado_url, author_email, i['created'], filename=filename)

    return attachment_dict


def sync_comments(ado_id: int, jira_ticket_id: str, jira_client: JiraClient,
                  ado_client: AzureDevOpsClient, attachment_dict: dict):
    """Add only new comments to an ADO work item.

    Deduplication: comments are identified by the "{author} ({formatted_date}):"
    prefix.  If any existing comment text contains this prefix, the comment is
    skipped.
    """
    existing_texts = ado_client.get_work_item_comment_texts(ado_id)
    existing_markers = set()
    for text in existing_texts:
        for m in JIRA_COMMENT_MARKER_PATTERN.findall(text or ''):
            existing_markers.add(m)

    response = jira_client.get_comments(jira_ticket_id)
    if not response or 'comments' not in response:
        return

    url = (
        f'{ado_client.organization_url}/{ado_client.project}'
        f'/_apis/wit/workItems/{ado_id}/comments?api-version={ado_client.api_version}'
    )
    sorted_comments = sorted(response['comments'], key=lambda d: d['created'])

    for x in sorted_comments:
        jira_comment_id = str(x.get('id', ''))
        if jira_comment_id and jira_comment_id in existing_markers:
            logging.debug(f"[sync_comments] Skipping duplicate comment by JiraCommentId={jira_comment_id}")
            continue

        author  = x['author']['displayName']
        created = ado_client.format_date(x['created'])
        prefix = f"{author} ({created}):"

        if any(prefix in existing for existing in existing_texts):
            logging.debug(f"[sync_comments] Skipping duplicate comment by prefix {author} at {created}")
            continue

        description = x.get('renderedBody') or ''

        # Replace Jira image links with ADO attachment links.
        # Strategy 1: rewrite by Jira content ID (most reliable — works even without alt attr)
        if attachment_dict and 'atlassian.net/rest/api/3/attachment/content/' in description:
            description = JIRA_CONTENT_PATTERN.sub(
                lambda m: attachment_dict.get(m.group(1), m.group(0)),
                description
            )

        # Strategy 2: rewrite by filename from alt attribute (legacy fallback)
        if '<img src=' in description.lower():
            img_links = re.findall('<img src="([^"]+)" alt="([^"]+)"', description)
            for a in img_links:
                if a[1] in attachment_dict:
                    description = description.replace(a[0], attachment_dict[a[1]])

        # Strategy 3: rewrite by filename from data-attachment-name (jira-attachment-thumbnail)
        if '<jira-attachment-thumbnail' in description.lower():
            thumbnail_links = re.findall(
                '<img src="([^"]+)" data-attachment-name="([^"]+)"', description
            )
            for a in thumbnail_links:
                if a[1] in attachment_dict:
                    description = description.replace(a[0], attachment_dict[a[1]])

        marker = f"[JiraCommentId:{jira_comment_id}] " if jira_comment_id else ''
        full_text = f'{marker}{author} ({created}): {description}'
        legacy_text = f'{author} ({created}): {description}'

        if full_text in existing_texts or legacy_text in existing_texts:
            logging.debug(f"[sync_comments] Skipping exact duplicate comment for JiraCommentId={jira_comment_id}")
            continue

        payload = {'text': full_text}
        ado_client.ado_api_call('POST', url, payload)


def _build_description_with_metadata(base_description: str, reporter_line: str = None,
                                     assignee_line: str = None) -> str:
    """Upsert Reporter/Assignee lines in description exactly once.

    reporter_line and assignee_line are optional — pass None when the value
    has been set on a proper ADO field instead of the description.
    This prevents repeated appends across reruns and ensures metadata fields
    remain present after any description rewrite.
    """
    desc = base_description or ''

    # Remove any existing Reporter/Assignee lines to avoid duplication.
    desc = re.sub(r'<br\s*/?>\s*<b>Reporter:</b>[^<]*(?=<br\s*/?>|$)', '', desc, flags=re.IGNORECASE)
    desc = re.sub(r'<br\s*/?>\s*<b>Assignee:</b>[^<]*(?=<br\s*/?>|$)', '', desc, flags=re.IGNORECASE)
    desc = re.sub(r'^\s*<b>Reporter:</b>[^<]*(?=<br\s*/?>|$)', '', desc, flags=re.IGNORECASE)
    desc = re.sub(r'^\s*<b>Assignee:</b>[^<]*(?=<br\s*/?>|$)', '', desc, flags=re.IGNORECASE)

    lines = [desc.strip()] if desc.strip() else []
    if assignee_line:
        lines.append(assignee_line)
    if reporter_line:
        lines.append(reporter_line)
    return '<br />'.join(lines)


def sync_links(ado_id: int, jira_ticket: dict, jira_client: JiraClient,
               jira_id: str, jira_instance: str, ado_client: AzureDevOpsClient,
               mapping: dict):
    """Add only new links to the ADO work item.

    For each linked Jira issue:
      - If the linked issue is already in ADO (via mapping or WIQL tag search),
        create a real ADO work item relationship (blocks, blocked-by, related, parent, child).
      - Otherwise fall back to adding the Jira URL as a hyperlink.

    Deduplication: checks existing relation URLs before adding anything.
    """
    existing_urls = ado_client.get_work_item_relation_urls(ado_id)

    # ---- PR / branch links (always hyperlinks) ----
    pr_response = jira_client.get_pull_request(jira_id)
    if pr_response and pr_response.get('detail'):
        detail = pr_response['detail'][0]
        if detail.get('pullRequests'):
            pr = detail['pullRequests'][0]
            if pr['url'] not in existing_urls:
                ado_client.add_hyperlink(ado_id, pr['url'], pr['author']['name'], pr['lastUpdate'])
        elif detail.get('branches'):
            branch = detail['branches'][0]
            if branch['url'] not in existing_urls:
                ado_client.add_hyperlink(
                    ado_id, branch['url'],
                    branch['lastCommit']['author']['name'],
                    branch['lastCommit']['authorTimestamp']
                )

    def _add_real_or_fallback(linked_key: str, ado_relation_type: str):
        """Add a real ADO link if the linked issue is migrated, else add Jira URL hyperlink."""
        # Look up ADO ID from in-memory mapping first, then WIQL tag search
        linked_ado_id = mapping.get(linked_key) or ado_client.find_work_item_by_jira_key(linked_key)
        if linked_ado_id:
            target_url = f'{ado_client.organization_url}/_apis/wit/workitems/{linked_ado_id}'
            if target_url not in existing_urls:
                try:
                    ado_client.add_work_item_link(ado_id, linked_ado_id, ado_relation_type)
                    logging.info(
                        f"[sync_links] ADO link {ado_id} --[{ado_relation_type}]--> "
                        f"{linked_ado_id} ({linked_key})"
                    )
                except Exception as e:
                    logging.warning(f"[sync_links] Could not add ADO link for {linked_key}: {e}")
        else:
            # Linked issue not yet migrated — store Jira URL as fallback hyperlink
            jira_url = f'https://{jira_instance}.atlassian.net/browse/{linked_key}'
            if jira_url not in existing_urls:
                ado_client.add_hyperlink(ado_id, jira_url)

    # ---- Parent relationship (from Jira parent field) ----
    # Use .get() to guard against the key being present but set to None
    if jira_ticket['fields'].get('parent'):
        parent_key = jira_ticket['fields']['parent']['key']
        _add_real_or_fallback(parent_key, 'System.LinkTypes.Hierarchy-Reverse')

    # ---- Inward / outward linked issues ----
    for a in jira_ticket['fields'].get('issuelinks', []):
        link_type_name = a.get('type', {}).get('name', '')
        for direction, issue_obj in [
            ('outward', a.get('outwardIssue')),
            ('inward',  a.get('inwardIssue')),
        ]:
            if issue_obj is None:
                continue
            linked_key = issue_obj['key']
            ado_relation_type = JIRA_TO_ADO_LINK_TYPES.get(
                (link_type_name, direction),
                'System.LinkTypes.Related'  # default for unrecognised types
            )
            _add_real_or_fallback(linked_key, ado_relation_type)


# ---------------------------------------------------------------------------
# Centralized create-or-update orchestration
# ---------------------------------------------------------------------------

def create_or_update_work_item(jira_ticket: dict, ado_client: AzureDevOpsClient,
                                jira_client: JiraClient, jira_instance: str,
                                mapping: dict, type_config: dict,
                                state_config: dict, custom_fields: list,
                                force_create: bool = False, detected_team_name: str = None,
                                skip_attachments: bool = False) -> int:
    """Find an existing ADO work item for the Jira ticket or create a new one,
    then sync all fields, comments, attachments, and links.

    Args:
        detected_team_name: If provided, ALL items are placed under this ADO team
                           (auto-detected from the Jira board/project being migrated).
                           This overrides per-ticket project-based team assignment.

    Returns the ADO work item ID, or None on failure.
    
    NOTE: If a subtask's parent is not yet migrated to ADO, the subtask is still created
    (with a hyperlink to the Jira parent). Parent/child links are added in a later pass
    once all items have been created.
    """
    jira_key = jira_ticket['key']
    jira_id  = jira_ticket['id']

    # Note: Parent validation has been removed. Items are now created even if parent
    # is missing. The sync_links function will add parent links where possible and
    # fall back to Jira URL hyperlinks for missing parents.

    title, assignee, ado_type, ado_state, description = build_parsed_fields(
        jira_ticket, type_config, state_config
    )

    # ---- Find or create ----
    ado_id = None if force_create else find_existing_ado_work_item(jira_key, ado_client, mapping)

    if ado_id:
        logging.info(f"[UPDATE] Updating existing ADO item {ado_id} for Jira {jira_key}")
        ado_client.update_item_core_fields(ado_id, title, ado_state, None, description)
        # Always enforce the correct type — bypassRules=true required; idempotent if already correct
        try:
            ado_client.change_work_item_type(ado_id, ado_type)
        except Exception as e:
            logging.warning(f"[type-change] Could not set type '{ado_type}' for {jira_key}: {e}")
    else:
        # Try the mapped type; if disabled in this project, fall back through _TYPE_FALLBACKS
        tried_types = [ado_type] + [t for t in _TYPE_FALLBACKS if t != ado_type]
        work_item = None
        used_type = ado_type
        for candidate_type in tried_types:
            try:
                work_item = ado_client.create_item(candidate_type, None, title, ado_state, None, description)
                used_type = candidate_type
                break
            except WorkItemTypeDisabledError as exc:
                logging.warning(f"[create_or_update_work_item] {exc} — trying next fallback type for {jira_key}")
                continue
        if work_item is None:
            logging.error(f"[create_or_update_work_item] Failed to create ADO item for {jira_key}")
            return None
        if used_type != ado_type:
            logging.warning(f"[create_or_update_work_item] Used fallback type '{used_type}' (mapped type '{ado_type}' is disabled) for {jira_key}")
        ado_id = work_item['id']
        save_issue_mapping(jira_key, ado_id)
        mapping[jira_key] = ado_id   # keep in-memory mapping current
        logging.info(f"[CREATE] Created ADO item {ado_id} for Jira {jira_key}")

    # ---- Area path — set immediately so later exceptions cannot skip it ----
    if detected_team_name:
        _ap_team = detected_team_name.strip()
    else:
        jira_project = jira_ticket['fields'].get('project') or {}
        jira_project_key = jira_project.get('key', '')
        if jira_project_key:
            raw_project_name = jira_project.get('name', '') or jira_project_key
            _ap_team = raw_project_name.replace('&', 'and')
            _ap_team = re.sub(r'[\\/<>|:?*"]+', '-', _ap_team).strip(' -')
            _ap_team = _ap_team if _ap_team else jira_project_key
        else:
            _ap_team = None
    if _ap_team:
        _ap_path = f'{ado_client.project}\\{_ap_team}'
        logging.info(f"[area-path] {jira_key}: team='{_ap_team}' → area_path='{_ap_path}'")
        if _ap_team not in _BOARD_SETUP_DONE:
            team_exists = ado_client.ensure_team(_ap_team)
            if team_exists:
                print(f'[board-setup] Team "{_ap_team}" already exists in ADO — reusing it.')
            ok_area = ado_client.ensure_area_path(_ap_team)
            ok_cfg  = ado_client.configure_team_area(_ap_team, _ap_path)
            ok_iter = ado_client.configure_team_iteration(_ap_team)
            if ok_area and ok_cfg and ok_iter:
                org_name = ado_client.organization_url.rstrip('/').split('/')[-1]
                print(f'[board-setup] \u2705 Team "{_ap_team}" ready — '
                      f'https://dev.azure.com/{org_name}/{ado_client.project.replace(" ", "%20")}'
                      f'/_boards/board/t/{_ap_team.replace(" ", "%20")}/Stories%20Risks%20and%20Insights')
            else:
                print(f'[board-setup] \u26a0\ufe0f  Some board setup steps failed for "{_ap_team}"')
            _BOARD_SETUP_DONE.add(_ap_team)
        # Retry up to 3x — re-ensure area node on each retry so it's truly mandatory
        for _attempt in range(3):
            try:
                ado_client.update_field(ado_id, '/fields/System.AreaPath', _ap_path)
                logging.info(f"[area-path] \u2705 {jira_key} → '{_ap_path}'")
                break
            except Exception as _e:
                if _attempt < 2:
                    logging.debug(f"[area-path] retry {_attempt + 1} for {jira_key}: {_e}")
                    ado_client.ensure_area_path(_ap_team)  # re-create node if it vanished
                else:
                    logging.warning(f"[area-path] \u274c Could not set '{_ap_path}' for {jira_key} after 3 attempts: {_e}")

    # ---- Assignee: set System.AssignedTo in ADO when possible ----
    # Try email first (exact match), then display name (ADO fuzzy resolve).
    # Only fall back to description if both attempts fail.
    assignee_set_in_ado = False
    assignee_line = None
    if assignee:
        display_name = jira_ticket['fields']['assignee']['displayName']
        # Build candidate list: email (if different from display name) then display name
        candidates = []
        if assignee != display_name:
            candidates.append(assignee)
        candidates.append(display_name)
        for candidate in candidates:
            try:
                ado_client.update_field(ado_id, '/fields/System.AssignedTo', candidate)
                assignee_set_in_ado = True
                break
            except Exception:
                continue
        if not assignee_set_in_ado:
            logging.info(f"[assignee] {display_name} not in ADO for {jira_key} — stored in description")
            assignee_line = (
                f'<b>Assignee:</b> {display_name} ({assignee})'
                if assignee != display_name
                else f'<b>Assignee:</b> {display_name}'
            )
    else:
        # No Jira assignee — explicitly clear System.AssignedTo so ADO does not
        # auto-assign to the API token owner (the request creator).
        try:
            ado_client.update_field(ado_id, '/fields/System.AssignedTo', '')
        except Exception:
            pass

    # ---- Reporter: set Custom.RequestedBy in ADO when possible ----
    # Try email first, then display name. Fall back to description only if both fail.
    reporter_info = jira_ticket['fields'].get('reporter') or {}
    reporter_display = reporter_info.get('displayName', 'Unknown')
    reporter_email   = reporter_info.get('emailAddress', '')
    reporter_set_in_ado = False
    reporter_line = None
    reporter_candidates = [c for c in [reporter_email, reporter_display] if c]
    for candidate in reporter_candidates:
        try:
            ado_client.update_field(ado_id, '/fields/Custom.RequestedBy', candidate)
            reporter_set_in_ado = True
            break
        except Exception:
            continue
    if not reporter_set_in_ado:
        logging.info(f"[reporter] {reporter_display} not in ADO for {jira_key} — stored in description")
        reporter_line = (
            f'<b>Reporter:</b> {reporter_display} ({reporter_email})'
            if reporter_email else
            f'<b>Reporter:</b> {reporter_display}'
        )
    try:
        live_item = ado_client.get_work_item_full(ado_id)
        current_desc = (live_item or {}).get('fields', {}).get('System.Description') or description
        updated_desc = _build_description_with_metadata(current_desc, reporter_line, assignee_line)
        if updated_desc != current_desc:
            ado_client.update_field(ado_id, '/fields/System.Description', updated_desc)
    except Exception as e:
        logging.warning(f"[create_or_update_work_item] Could not upsert reporter/assignee lines for {jira_key}: {e}")

    # ---- Custom fields ----
    logging.debug(f"[main] Updating custom fields for: {ado_id}")
    sync_custom_fields(ado_id, jira_ticket, ado_client, custom_fields)
    logging.debug(f"[main] Finished updating custom fields for: {ado_id}")

    # ---- Priority ----
    _jira_prio = (jira_ticket['fields'].get('priority') or {}).get('name') or ''
    match _jira_prio:
        case 'Highest': _ado_prio = '1-Critical'
        case 'High':    _ado_prio = '2-High'
        case 'Medium':  _ado_prio = '3-Medium'
        case 'Low':     _ado_prio = '4-Low'
        case 'Lowest':  _ado_prio = '4-Low'
        case _:         _ado_prio = '3-Medium'  # default when Jira priority is null
    try:
        ado_client.update_field(ado_id, '/fields/Custom.PriorityLevel', _ado_prio)
        logging.info(f"[priority] ✅ {jira_key}: '{_jira_prio or '(none)'}' → '{_ado_prio}'")
    except Exception as e:
        logging.warning(f"[priority] ❌ {jira_key}: could not set '{_ado_prio}': {e}")

    # ---- Date fields ----
    # ONLY Actual Start Date is populated (from Jira created).
    # All other date fields are explicitly cleared to null.
    def _to_ado_dt(jira_ts: str) -> str:
        # Use the LOCAL date from the Jira timestamp's own timezone offset.
        # Storing noon UTC of that local date ensures ADO renders the correct
        # calendar date in any US timezone (UTC-4 through UTC-8), because
        # ADO Date fields strip the time to midnight UTC which shifts the
        # display by -1 day in western timezones when we naively use UTC date.
        dt = _parse_dt(jira_ts)          # preserves original tz offset from Jira
        local_date = dt.strftime('%Y-%m-%d')   # date as shown in Jira's UI
        return f'{local_date}T12:00:00.000Z'   # noon UTC → renders correctly in all US timezones

    jira_fields = jira_ticket['fields']

    # Actual Start Date ← Jira created
    if ts := jira_fields.get('created'):
        try:
            ado_client.update_field(ado_id, '/fields/Custom.ActualStartDate', _to_ado_dt(ts))
        except Exception as e:
            logging.debug(f"[dates] ActualStartDate not available for {jira_key}: {e}")

    # Explicitly null out TargetDate only; ActualCompletionDate will be set
    # from the Jira resolution date if one exists (otherwise cleared).
    _clear_url = (
        f'{ado_client.organization_url}/{ado_client.project}'
        f'/_apis/wit/workitems/{ado_id}?api-version=7.0-preview.3&bypassRules=true'
    )
    _clear_payload = [
        {"op": "remove", "path": "/fields/Microsoft.VSTS.Scheduling.TargetDate"},
    ]
    try:
        import requests as _req
        _req.patch(_clear_url,
                   auth=(ado_client.username, ado_client.access_token),
                   headers={'Content-Type': 'application/json-patch+json'},
                   json=_clear_payload)
    except Exception:
        pass

    # Set ActualCompletionDate from Jira resolution date if present, otherwise clear it.
    jira_resolution = jira_fields.get('resolutiondate')
    try:
        if jira_resolution:
            try:
                formatted_date = _to_ado_dt(jira_resolution)
                ado_client.update_field(ado_id, '/fields/Custom.ActualCompletionDate', formatted_date)
                logging.info(f"[dates] Set ActualCompletionDate for {jira_key}: {jira_resolution} → {formatted_date}")
            except Exception as e:
                logging.warning(f"[dates] Could not set ActualCompletionDate for {jira_key}: {e}")
        else:
            # Remove the field to ensure it's null in ADO
            logging.debug(f"[dates] No resolution date for {jira_key} — clearing ActualCompletionDate in ADO")
            try:
                _req = __import__('requests')
                _req.patch(_clear_url,
                           auth=(ado_client.username, ado_client.access_token),
                           headers={'Content-Type': 'application/json-patch+json'},
                           json=[{"op": "remove", "path": "/fields/Custom.ActualCompletionDate"}])
            except Exception:
                pass
    except Exception as e:
        # Defensive: do not fail the whole worker if date logic fails
        logging.warning(f"[dates] Unexpected error handling resolution date for {jira_key}: {e}")

    # ---- Labels / tags (merge, do not overwrite existing tags) ----
    labels = jira_ticket['fields'].get('labels', [])
    sprint_fields = jira_ticket['fields'].get('customfield_10007') or []
    sprint_names = [s.get('name', '') for s in sprint_fields if isinstance(s, dict) and s.get('name')]
    all_new_tags = labels + sprint_names
    if all_new_tags:
        item = ado_client.get_work_item_full(ado_id)
        existing_tags = (item.get('fields', {}).get('System.Tags') or '').strip() if item else ''
        tag_parts = [t.strip() for t in existing_tags.split(';') if t.strip()]
        tag_parts_lower = {t.lower() for t in tag_parts}
        for tag in all_new_tags:
            if tag and tag.lower() not in tag_parts_lower:
                tag_parts.append(tag)
                tag_parts_lower.add(tag.lower())
        ado_client.update_field(ado_id, '/fields/System.Tags', '; '.join(tag_parts))

    # ---- Attachments ----
    attachment_dict = sync_attachments(ado_id, jira_ticket, jira_client, ado_client,
                                       skip_attachments=skip_attachments)
    try:
        removed = ado_client.remove_duplicate_attachment_relations(ado_id)
        if removed:
            logging.info(f"[sync_attachments] Removed {removed} duplicate attachment relation(s) on {ado_id}")
    except Exception as e:
        logging.warning(f"[sync_attachments] Could not remove duplicate attachment relations on {ado_id}: {e}")

    # ---- Update description image links ----
    # Fetch the live ADO description (reporter/assignee already appended)
    # so those lines are preserved when replacing Jira image URLs.
    if '<img src=' in description.lower() and attachment_dict:
        img_links = re.findall('<img src="([^"]+)" alt="([^"]+)"', description)
        live_item = ado_client.get_work_item_full(ado_id)
        current_desc = (live_item or {}).get('fields', {}).get('System.Description') or description
        updated_desc = current_desc
        for src, alt in img_links:
            if alt in attachment_dict:
                updated_desc = updated_desc.replace(src, attachment_dict[alt])
        if updated_desc != current_desc:
            ado_client.update_field(ado_id, '/fields/System.Description', updated_desc)

    # ---- Comments ----
    sync_comments(ado_id, jira_id, jira_client, ado_client, attachment_dict)

    # ---- Hyperlinks / linked issues ----
    sync_links(ado_id, jira_ticket, jira_client, jira_id, jira_instance, ado_client, mapping)

    # ---- JiraKey tag — applied last so other updates cannot remove it ----
    ado_client.ensure_jira_key_tag(ado_id, jira_key)

    return ado_id


# ---------------------------------------------------------------------------
# Jira board detection
# ---------------------------------------------------------------------------

def detect_jira_board_from_filter(jira_client: JiraClient, filter_id: str) -> str:
    """Detect the Jira board/project name from a filter ID.
    
    Parses the filter's JQL to extract the project name or key.
    Returns the project name/key, or None if not found.
    """
    try:
        filter_info = jira_client.jira_api_call(
            'GET', f'{jira_client.server}/rest/api/3/filter/{filter_id}'
        )
        if not filter_info:
            return None
        
        jql = filter_info.get('jql', '')
        if not jql:
            return None
        
        # Try to extract project from JQL
        # Pattern 1: project = "Project Name" or project = 'Project Name'
        m = re.search(r'project\s*=\s*["\']([^"\']+)["\']', jql, re.IGNORECASE)
        if m:
            return m.group(1)
        
        # Pattern 2: project = KEY or project in (KEY)
        m = re.search(r'project\s*(?:=|in\s*\()\s*([A-Z][A-Z0-9]+)', jql)
        if m:
            return m.group(1)
        
        logging.warning(f"[detect_jira_board] Could not parse project from filter JQL: {jql}")
        return None
    except Exception as e:
        logging.warning(f"[detect_jira_board] Error detecting board from filter {filter_id}: {e}")
        return None


def detect_jira_board_from_keys(jira_tickets: dict) -> str:
    """Detect the Jira board/project name from a set of tickets.
    
    Returns the most common project name, or the first one if all are unique.
    """
    projects = {}
    for ticket in jira_tickets.values():
        jira_project = ticket['fields'].get('project') or {}
        project_name = jira_project.get('name') or jira_project.get('key')
        if project_name:
            projects[project_name] = projects.get(project_name, 0) + 1
    
    if not projects:
        return None
    
    # Return the most common project, or the first if only one
    most_common = max(projects, key=projects.get)
    if len(projects) > 1:
        logging.warning(f"[detect_jira_board] Found multiple projects: {list(projects.keys())} — using most common: {most_common}")
    return most_common


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Jira to ADO Copy')
    parser.add_argument('--jira-instance', help='Jira source instance name (from URL)')
    parser.add_argument('--jira-filter',   help='Jira source filter ID')
    parser.add_argument('--jira-jql',      help='JQL query string (AI migrate flow)')
    parser.add_argument('--ado-project',   help='ADO target project name')
    parser.add_argument('--project-key',   help='Board prefix (e.g. RRR) or ALL — reads keys from migration_mapping.json')
    parser.add_argument('--jira-keys',      help='Comma-separated list of Jira keys to process (e.g. CSQA-1,CSQA-5,CSQA-12)')
    parser.add_argument('--retry-failed',   action='store_true', help='Re-process all keys recorded in failed_issues.json')
    parser.add_argument('--force-create',   action='store_true', help='Skip duplicate detection — always create new ADO items (use to recover phantom/missing keys)')
    parser.add_argument('--skip-attachments', action='store_true', help='Skip attachment uploads (useful if ADO is timing out on large files)')

    args = parser.parse_args()

    # --jira-jql: resolve keys up-front so the existing --jira-keys path handles everything
    if args.jira_jql and not args.jira_keys:
        # Lazy-init jira client just for the search — full init happens below
        _jcfg = load_jira_config(args.jira_instance) if args.jira_instance else load_jira_config('')
        _jc   = JiraClient(_jcfg)
        logging.info(f"[main] JQL mode: '{args.jira_jql}'")
        print(f"[main] JQL mode: '{args.jira_jql}'")
        _result = _jc.search_jql_paginated(args.jira_jql)
        if not _result or not _result.get('issues'):
            print(f'[main] JQL returned 0 issues — nothing to migrate.')
            sys.exit(0)
        _keys = [i['key'] for i in _result['issues'] if i.get('key')]
        logging.info(f"[main] JQL returned {len(_keys)} issues")
        print(f"[main] JQL returned {len(_keys)} issues")
        args.jira_keys = ','.join(_keys)
        # Auto-detect team name from JQL (e.g. project = "SCRUM" → SCRUM)
        _m = re.search(r'project\s*=\s*["\']?([A-Za-z0-9_-]+)["\']?', args.jira_jql, re.IGNORECASE)
        if _m and not getattr(args, '_detected_team', None):
            args._detected_team = _m.group(1).strip()
            print(f"[board-detect] Auto-detected Jira board from JQL: {args._detected_team}")

    # Only prompt for filter if neither --jira-keys/--project-key nor --retry-failed was supplied
    try:
        while not args.jira_instance:
            args.jira_instance = input("Enter Jira instance name: ").strip()
        if not args.jira_keys and not args.project_key and not args.retry_failed:
            while not args.jira_filter:
                args.jira_filter = input("Enter Jira filter ID: ").strip()
        while not args.ado_project:
            args.ado_project = input("Enter ADO project name: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(1)

    jira_instance = args.jira_instance
    jira_filter   = args.jira_filter
    ado_project   = args.ado_project

    try:
        print(f'Copying Jira Work Items from {jira_instance} to {ado_project}')
        logging.info(f'Copying Jira Work Items from {jira_instance} to {ado_project}')

        ado_config  = load_ado_config()
        ado_client  = AzureDevOpsClient(ado_config, ado_project)

        jira_config = load_jira_config(jira_instance)
        jira_client = JiraClient(jira_config)

        # Load persistent mapping and configs once — passed into every ticket call
        mapping       = load_issue_mapping()
        type_config   = load_config('type_config.json')
        state_config  = load_config('state_config.json')
        custom_fields = load_config('custom_fields_config.json')
        logging.info(f"[main] Loaded mapping with {len(mapping)} existing entries")

        # --project-key: derive jira keys directly from migration mapping cache
        if args.project_key and not args.jira_keys:
            prefix = args.project_key.upper()
            if prefix == 'ALL':
                project_keys = sorted(mapping.keys())
                args._detected_team = None  # Mixed projects — no single team
            else:
                project_keys = sorted(k for k in mapping.keys() if k.startswith(prefix + '-'))
                args._detected_team = prefix  # Use the project key as team name
            if not project_keys:
                logging.error(f"No keys found for project '{args.project_key}' in migration_mapping.json")
                sys.exit(1)
            args.jira_keys = ','.join(project_keys)
            logging.info(f"--project-key {args.project_key}: resolved {len(project_keys)} key(s)")
            print(f"[project-key] Resolved {len(project_keys)} key(s) for {args.project_key}")
            if args._detected_team:
                print(f"[board-detect] Auto-detected Jira board: {args._detected_team}")

        # --retry-failed: treat the failed_issues.json keys as --jira-keys
        if args.retry_failed:
            failed = load_failed_issues()
            if not failed:
                print('[retry-failed] No failed issues on record. Nothing to do.')
                return
            args.jira_keys = ','.join(failed.keys())
            print(f'[retry-failed] Retrying {len(failed)} failed/missed issue(s):')
            for k, v in failed.items():
                reason = v.get("reason", v) if isinstance(v, dict) else v
                ts     = v.get("timestamp", "") if isinstance(v, dict) else ""
                suffix = f" (recorded: {ts})" if ts else ""
                print(f'  {k}: {reason}{suffix}')
            args._detected_team = None  # Mixed projects — no single team

        # --jira-keys: fetch those issues directly — no filter call needed
        if args.jira_keys:
            target_keys = [k.strip().upper() for k in args.jira_keys.split(',') if k.strip()]
            logging.info(f"[main] Direct mode: fetching {len(target_keys)} issues by key: {target_keys}")

            # PHASE 1: Sort issues by dependency (parents before subtasks)
            print(f'[bootstrap] Sorting {len(target_keys)} issues by parent dependencies...')
            jira_tickets_by_key = {}
            for jira_key in target_keys:
                jira_ticket = jira_client.get_jira_issue(jira_key)
                if jira_ticket:
                    jira_tickets_by_key[jira_key] = jira_ticket
            
            # Ensure parents are processed before subtasks
            sorted_keys = []
            processed = set()
            def add_with_deps(key):
                if key in processed or key not in jira_tickets_by_key:
                    return
                ticket = jira_tickets_by_key[key]
                parent_key = ticket['fields'].get('parent', {}).get('key')
                if parent_key and parent_key not in processed:
                    add_with_deps(parent_key)
                sorted_keys.append(key)
                processed.add(key)
            
            for key in target_keys:
                add_with_deps(key)
            
            logging.info(f"[main] Dependency-sorted order: {sorted_keys}")

            # Detect the Jira board name from the tickets (if not already set by --project-key)
            if not hasattr(args, '_detected_team') or not args._detected_team:
                detected_board = detect_jira_board_from_keys(jira_tickets_by_key)
                if detected_board:
                    args._detected_team = detected_board.strip()
                    print(f"[board-detect] Auto-detected Jira board from tickets: {args._detected_team}")

            # Bootstrap: check ADO only for keys NOT already in the local mapping.
            if args.force_create:
                print(f'[bootstrap] --force-create set — skipping all duplicate detection for {len(sorted_keys)} key(s).')
            if getattr(args, '_detected_team', None):
                print(f'[bootstrap] All {len(sorted_keys)} items will be placed under ADO team: "{args._detected_team}"')
            else:
                unknown_keys = [k for k in sorted_keys if k not in mapping]
                if unknown_keys:
                    print(f'[bootstrap] Checking ADO for {len(unknown_keys)} unknown keys (of {len(sorted_keys)} total)...')
                    ado_existing = ado_client.bulk_fetch_jira_key_mapping(unknown_keys, skip_title_search=True)
                    for jira_key, ado_id in ado_existing.items():
                        mapping[jira_key] = ado_id
                        save_issue_mapping(jira_key, ado_id)
                    print(f'[bootstrap] Done. {len(ado_existing)} existing items found in ADO.')
                else:
                    print(f'[bootstrap] All {len(sorted_keys)} keys already in local mapping — skipping ADO lookup.')

            succeeded = []
            failed_items = []
            base_cmd = (f"--jira-instance {jira_instance} "
                        f"--ado-project \"{ado_project}\""
                        + (f" --jira-filter {jira_filter}" if jira_filter else ""))

            # PHASE 2: Process in dependency order
            for jira_key in sorted_keys:
                jira_ticket = jira_tickets_by_key.get(jira_key)
                if jira_ticket is None:
                    logging.error(f"[main] Could not fetch Jira issue {jira_key} — skipping")
                    save_failed_issue(jira_key, "Could not fetch from Jira")
                    failed_items.append(jira_key)
                    continue

                logging.info(f'[main] Copying {jira_key} ...')
                mark_issue_started(jira_key)
                try:
                    ado_id = create_or_update_work_item(
                        jira_ticket, ado_client, jira_client, jira_instance,
                        mapping, type_config, state_config, custom_fields,
                        force_create=args.force_create,
                        detected_team_name=getattr(args, '_detected_team', None),
                        skip_attachments=args.skip_attachments
                    )
                    if ado_id:
                        logging.info(f"[main] Finished work item: {ado_id}")
                        logging.info("*" * 80)
                        clear_failed_issue(jira_key)
                        succeeded.append(jira_key)
                    else:
                        save_failed_issue(jira_key, "create_or_update_work_item returned no ADO id")
                        failed_items.append(jira_key)
                except Exception as e:
                    logging.error(f"[main] Failed to process {jira_key}: {str(e)}")
                    if hasattr(e, 'response') and hasattr(e.response, 'text'):
                        logging.info(f"Response: {e.response.text}")
                    save_failed_issue(jira_key, str(e))
                    failed_items.append(jira_key)

            print_run_summary(succeeded, failed_items, base_cmd)
            return

        # Check if jira_filter is actually a Jira issue key (e.g., DATA-1011)
        if jira_filter and is_jira_key(jira_filter):
            logging.info(f"[main] --jira-filter contains Jira key '{jira_filter}' — fetching single issue directly")
            print(f'[main] Fetching single Jira issue: {jira_filter}')
            
            jira_ticket = jira_client.get_jira_issue(jira_filter)
            if jira_ticket is None:
                logging.error(f"[main] Could not fetch Jira issue {jira_filter}")
                print(f'Error: Could not fetch Jira issue {jira_filter}')
                sys.exit(1)
            
            # Wrap single ticket in filter response format (same structure as filter API)
            jira_df = {
                'issues': [{'id': jira_ticket['id'], '_ticket': jira_ticket}]
            }
            jira_issues = jira_df.get('issues')  # Use consistent format with filter path
            logging.info(f"[main] Fetched 1 issue: {jira_filter}")
        else:
            # Use filter API for actual filter IDs
            # Fetch filter results from Jira
            jira_df = jira_client.get_filter_items(jira_filter)
            if jira_df is None:
                print(f'No items returned from filter {jira_filter}')
                logging.info(f'No items returned from filter {jira_filter}')
                return None

            jira_issues = jira_df.get('issues', [])
            
            # Diagnostic: Check if API returned partial results (pagination issue)
            api_total = jira_df.get('total')
            returned_count = len(jira_issues)
            
            logging.info(f"[main] Found {returned_count} issues in Jira (API total count: {api_total})")
            
            if api_total and api_total > returned_count:
                logging.warning(
                    f"[main] ⚠️  PAGINATION WARNING: Filter {jira_filter} has {api_total} total issues "
                    f"but only {returned_count} were returned. You may need to handle pagination."
                )
                print(f"⚠️  WARNING: Filter has {api_total} total issues but only {returned_count} returned.")
                print(f"   This suggests the API hit a result limit. Check get_filter_items() pagination.")
            else:
                print(f"[main] Found {returned_count} issues in Jira")


        # When the filter has 0 issues, still set up the ADO board/area path so
        # the team and area path exist before any manual card creation.
        if not jira_issues:
            filter_info = jira_client.jira_api_call(
                'GET', f'{jira_client.server}/rest/api/3/filter/{jira_filter}'
            )
            jql = (filter_info or {}).get('jql', '')
            project_info = None

            # Try quoted project name first: project = "Name" or project = 'Name'
            m_name = re.search(r'project\s*=\s*["\']([^"\']+)["\']', jql, re.IGNORECASE)
            if m_name:
                query = m_name.group(1)
                search_resp = jira_client.jira_api_call(
                    'GET', f'{jira_client.server}/rest/api/3/project/search',
                    {'query': query, 'maxResults': 10}
                )
                for p in ((search_resp or {}).get('values') or []):
                    if p.get('name', '').lower() == query.lower():
                        project_info = p
                        break

            # Fall back to unquoted key: project = KEY or project in (KEY)
            if not project_info:
                m_key = re.search(r'project\s*(?:=|in\s*\()\s*([A-Z][A-Z0-9]+)', jql)
                if m_key:
                    project_key = m_key.group(1).upper()
                    project_info = jira_client.jira_api_call(
                        'GET', f'{jira_client.server}/rest/api/3/project/{project_key}'
                    )

            if project_info:
                raw_name  = project_info.get('name', '') or project_info.get('key', '')
                team_name = raw_name.replace('&', 'and')
                team_name = re.sub(r'[\\/<>|:?*"]+', '-', team_name).strip(' -')
                team_name = team_name if team_name else project_info.get('key', 'Unknown')
                area_path = f'{ado_client.project}\\{team_name}'
                pk_str    = project_info.get('key', '?')
                print(f'[board-setup] 0 issues in filter — configuring board for project {pk_str} ({team_name}) ...')
                ok_area = ado_client.ensure_area_path(team_name)
                ok_team = ado_client.ensure_team(team_name)
                ok_cfg  = ado_client.configure_team_area(team_name, area_path)
                ok_iter = ado_client.configure_team_iteration(team_name)
                if ok_area and ok_team and ok_cfg and ok_iter:
                    org_name     = ado_client.organization_url.rstrip('/').split('/')[-1]
                    encoded_team = team_name.replace(' ', '%20')
                    encoded_proj = ado_client.project.replace(' ', '%20')
                    print(f'[board-setup] ✅ Team "{team_name}" ready — board: '
                          f'https://dev.azure.com/{org_name}/{encoded_proj}'
                          f'/_boards/board/t/{encoded_team}/Stories%20Risks%20and%20Insights')
                else:
                    print(f'[board-setup] ⚠️  Some board setup steps failed for "{team_name}" — check logs')
            else:
                print(f'[board-setup] Could not determine project from filter JQL: {jql!r} — board not configured')
            return

        # Bootstrap: resolve all keys against ADO before creating anything
        # This prevents duplicates even when the local mapping cache is missing/stale.
        # Only query ADO for keys NOT already in the local mapping — avoids expensive
        # WIQL title searches for keys we already know about.
        filter_keys = []
        for x in jira_issues:
            ticket = jira_client.get_jira_issue(x['id'])
            if ticket:
                filter_keys.append(ticket['key'])
                x['_ticket'] = ticket  # cache so we don't re-fetch below

        # PHASE 1: Sort by parent dependencies (parents before subtasks)
        print(f'[bootstrap] Sorting {len(filter_keys)} items by parent dependencies...')
        
        # Detect the Jira board name from the filter (if not already set)
        if not hasattr(args, '_detected_team') or not args._detected_team:
            detected_board = detect_jira_board_from_filter(jira_client, jira_filter)
            if detected_board:
                args._detected_team = detected_board.strip()
                print(f"[board-detect] Auto-detected Jira board from filter: {args._detected_team}")
        
        if getattr(args, '_detected_team', None):
            print(f'[bootstrap] All {len(filter_keys)} items will be placed under ADO team: "{args._detected_team}"')
        
        jira_tickets_by_key_filter = {t['key']: t for t in [x.get('_ticket') for x in jira_issues if x.get('_ticket')]}
        sorted_filter_keys = []
        processed_filter = set()
        def add_filter_with_deps(key):
            if key in processed_filter or key not in jira_tickets_by_key_filter:
                return
            ticket = jira_tickets_by_key_filter[key]
            parent_key = ticket['fields'].get('parent', {}).get('key')
            if parent_key and parent_key not in processed_filter:
                add_filter_with_deps(parent_key)
            sorted_filter_keys.append(key)
            processed_filter.add(key)
        
        for key in filter_keys:
            add_filter_with_deps(key)
        
        unknown_keys = [k for k in sorted_filter_keys if k not in mapping]
        known_count  = len(sorted_filter_keys) - len(unknown_keys)
        if known_count:
            print(f'[bootstrap] {known_count} of {len(sorted_filter_keys)} key(s) already in local mapping — skipping ADO lookup for those.')
        if unknown_keys:
            print(f'[bootstrap] Checking ADO for {len(unknown_keys)} unknown key(s) (tag search only)...')
            ado_existing = ado_client.bulk_fetch_jira_key_mapping(unknown_keys, skip_title_search=True)
            for jira_key, ado_id in ado_existing.items():
                mapping[jira_key] = ado_id
                save_issue_mapping(jira_key, ado_id)
            print(f'[bootstrap] Done. {len(ado_existing)} existing items found in ADO.')

        succeeded = []
        failed_items = []
        base_cmd = (f"--jira-instance {jira_instance} "
                    f"--ado-project \"{ado_project}\""
                    + (f" --jira-filter {jira_filter}" if jira_filter else ""))

        for jira_key in sorted_filter_keys:
            jira_ticket = jira_tickets_by_key_filter.get(jira_key)
            if jira_ticket is None:
                continue

            logging.info(f'[main] Copying {jira_key} ...')
            mark_issue_started(jira_key)

            try:
                ado_id = create_or_update_work_item(
                    jira_ticket, ado_client, jira_client, jira_instance,
                    mapping, type_config, state_config, custom_fields,
                    detected_team_name=getattr(args, '_detected_team', None)
                )
                if ado_id:
                    logging.info(f"[main] Finished work item: {ado_id}")
                    logging.info("*" * 80)
                else:
                    save_failed_issue(jira_key, "create_or_update_work_item returned no ADO id")
                    failed_items.append(jira_key)
                    continue
            except Exception as e:
                logging.error(f"[main] Failed to process {jira_key}: {str(e)}")
                if hasattr(e, 'response') and hasattr(e.response, 'text'):
                    logging.info(f"Response: {e.response.text}")
                save_failed_issue(jira_key, str(e))
                failed_items.append(jira_key)
                continue
            else:
                clear_failed_issue(jira_key)
                succeeded.append(jira_key)

        print_run_summary(succeeded, failed_items, base_cmd)

    except Exception as e:
        logging.error(f"Error: {str(e)}")
        if hasattr(e, 'response') and hasattr(e.response, 'text'):
            logging.info(f"Response: {e.response.text}")


if __name__ == "__main__":
    main()
