import sys
import json
import argparse
import re
import logging
from datetime import timezone
from dateutil.parser import parse as _parse_dt

from pathlib import Path

# Add the utilities directory to the path to import utils_ado
sys.path.append(str(Path(__file__).parent.parent.parent / "utilities"))
from utils_ado import AzureDevOpsClient, load_ado_config
from utils_jira import JiraClient, load_jira_config
from utils_mapping import load_issue_mapping, save_issue_mapping

logging.basicConfig(
    filename='worker_jira_to_ado_copy.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Matches Jira attachment REST URLs so we can rewrite them to ADO URLs in comments
JIRA_CONTENT_PATTERN = re.compile(
    r'https://healthfinch\.atlassian\.net/rest/api/3/attachment/content/(\d+)'
)

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


def save_failed_issue(jira_key: str, reason: str):
    """Append/update a failed issue entry and persist to disk."""
    data = load_failed_issues()
    data[jira_key] = reason
    FAILED_PATH.parent.mkdir(parents=True, exist_ok=True)
    FAILED_PATH.write_text(json.dumps(data, indent=2))
    logging.info(f"[failed_issues] Recorded failure for {jira_key}: {reason}")


def clear_failed_issue(jira_key: str):
    """Remove a jira_key from the failed issues file once it succeeds."""
    data = load_failed_issues()
    if jira_key in data:
        del data[jira_key]
        FAILED_PATH.write_text(json.dumps(data, indent=2))

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
# Also emit to stdout so progress is visible in the terminal
logging.getLogger().addHandler(logging.StreamHandler())

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

    state     = jira_ticket['fields']['status']['name']
    jira_type = jira_ticket['fields']['issuetype']['name']
    description = jira_ticket['renderedFields']['description'] or ''

    # Append customer info to description when present
    customers = jira_ticket['fields'].get('customfield_10907')
    if customers:
        description += '<p><b>Customers:</b></p>'
        customers_content = jira_ticket['fields']['customfield_10907']['content'][0]['content']
        for customer in customers_content:
            description += f'{customer["text"]}<br />'

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
                     ado_client: AzureDevOpsClient) -> dict:
    """Upload Jira attachments and link only new ones to the ADO work item.

    Deduplication: checks the 'name' attribute stored on each existing
    AttachedFile relation.  Falls back to a URL substring check for relations
    created before the 'name' attribute was introduced.

    Returns an attachment_dict mapping filename -> ADO attachment URL.
    """
    existing_item    = ado_client.get_work_item_full(ado_id)
    existing_rels    = (existing_item or {}).get('relations') or []
    existing_attached = [
        r for r in existing_rels if r.get('rel') == 'AttachedFile'
    ]
    # Build a set of filenames already linked (via stored 'name' attribute)
    existing_names = {
        r.get('attributes', {}).get('name', '')
        for r in existing_attached
    } - {''}  # drop empty strings
    attachment_dict = {}

    for i in jira_ticket['fields']['attachment']:
        filename   = i['filename']
        attachment = jira_client.get_attachment(i['content'])
        response   = ado_client.create_attachment(filename, attachment)
        if response is None:
            logging.warning(f"[sync_attachments] create_attachment returned None for {filename}")
            continue

        ado_url = response['url']
        attachment_dict[filename] = ado_url
        # Also index by Jira content ID so sync_comments can rewrite inline image URLs
        attachment_dict[str(i['id'])] = ado_url

        # Skip linking if this filename is already present in ADO
        if filename in existing_names:
            logging.debug(f"[sync_attachments] Skipping duplicate attachment link: {filename}")
            continue

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
    response = jira_client.get_comments(jira_ticket_id)
    if not response or 'comments' not in response:
        return

    url = (
        f'{ado_client.organization_url}/{ado_client.project}'
        f'/_apis/wit/workItems/{ado_id}/comments?api-version={ado_client.api_version}'
    )
    sorted_comments = sorted(response['comments'], key=lambda d: d['created'])

    for x in sorted_comments:
        author  = x['author']['displayName']
        created = ado_client.format_date(x['created'])
        prefix  = f"{author} ({created}):"

        if any(prefix in existing for existing in existing_texts):
            logging.debug(f"[sync_comments] Skipping duplicate comment by {author} at {created}")
            continue

        description = x['renderedBody']

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

        payload = {'text': f'{author} ({created}): {description}'}
        ado_client.ado_api_call('POST', url, payload)


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
        if pr_response['detail'][0]['pullRequests']:
            pr = pr_response['detail'][0]['pullRequests'][0]
            if pr['url'] not in existing_urls:
                ado_client.add_hyperlink(ado_id, pr['url'], pr['author']['name'], pr['lastUpdate'])
        elif pr_response['detail'][0]['branches']:
            branch = pr_response['detail'][0]['branches'][0]
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
                                state_config: dict, custom_fields: list) -> int:
    """Find an existing ADO work item for the Jira ticket or create a new one,
    then sync all fields, comments, attachments, and links.

    Returns the ADO work item ID, or None on failure.
    """
    jira_key = jira_ticket['key']
    jira_id  = jira_ticket['id']

    title, assignee, ado_type, ado_state, description = build_parsed_fields(
        jira_ticket, type_config, state_config
    )

    # ---- Find or create ----
    ado_id = find_existing_ado_work_item(jira_key, ado_client, mapping)

    if ado_id:
        logging.info(f"[UPDATE] Updating existing ADO item {ado_id} for Jira {jira_key}")
        ado_client.update_item_core_fields(ado_id, title, ado_state, None, description)
        # Update work item type in case type_config.json changed (e.g. Task -> Issue)
        try:
            ado_client.update_field(ado_id, '/fields/System.WorkItemType', ado_type)
        except Exception as e:
            logging.warning(f"[create_or_update_work_item] Could not update type to '{ado_type}' for {jira_key}: {e}")
    else:
        work_item = ado_client.create_item(ado_type, None, title, ado_state, None, description)
        if work_item is None:
            logging.error(f"[create_or_update_work_item] Failed to create ADO item for {jira_key}")
            return None
        ado_id = work_item['id']
        save_issue_mapping(jira_key, ado_id)
        mapping[jira_key] = ado_id   # keep in-memory mapping current
        logging.info(f"[CREATE] Created ADO item {ado_id} for Jira {jira_key}")

    # ---- Assignee — always written to description, also try ADO AssignedTo field ----
    if assignee:
        display_name = jira_ticket['fields']['assignee']['displayName']
        assignee_line = (
            f'<b>Assignee:</b> {display_name} ({assignee})'
            if assignee != display_name   # email available
            else f'<b>Assignee:</b> {display_name}'
        )
        # Always put it in description so it's visible regardless of ADO user availability
        try:
            ado_client.append_description(ado_id, assignee_line)
        except Exception as e:
            logging.warning(f"[create_or_update_work_item] Could not append assignee to description for {jira_key}: {e}")
        # Also try to set the ADO AssignedTo field (only works when email is available)
        if assignee != display_name:
            try:
                ado_client.update_field(ado_id, '/fields/System.AssignedTo', assignee)
            except Exception:
                logging.warning(
                    f"[create_or_update_work_item] Assignee {assignee} not found in ADO for {jira_key} — description fallback used"
                )

    # ---- Reporter field — always append to description for visibility ----
    reporter_info = jira_ticket['fields'].get('reporter') or {}
    reporter_display = reporter_info.get('displayName', 'Unknown')
    reporter_email   = reporter_info.get('emailAddress', '')
    reporter_line = (
        f'<b>Reporter:</b> {reporter_display} ({reporter_email})'
        if reporter_email else
        f'<b>Reporter:</b> {reporter_display}'
    )
    try:
        ado_client.append_description(ado_id, reporter_line)
    except Exception as e:
        logging.warning(f"[create_or_update_work_item] Could not append reporter for {jira_key}: {e}")

    # ---- Custom fields ----
    logging.info(f"[main] Updating custom fields for: {ado_id}")
    sync_custom_fields(ado_id, jira_ticket, ado_client, custom_fields)
    logging.info(f"[main] Finished updating custom fields for: {ado_id}")

    # ---- Priority ----
    if field := jira_ticket['fields']['priority']['name']:
        match field:
            case 'Highest': priority = '1-Critical'
            case 'High':    priority = '2-High'
            case 'Medium':  priority = '3-Medium'
            case 'Low':     priority = '4-Low'
            case 'Lowest':  priority = '4-Low'
            case _:         priority = '3-Medium'
        ado_client.update_field(ado_id, '/fields/Custom.PriorityLevel', priority)

    # ---- Due date ----
    if field := jira_ticket['fields']['duedate']:
        ado_client.update_field(ado_id, '/fields/Microsoft.VSTS.Scheduling.TargetDate', field)

    # ---- Date fields (Time frame section) ----
    # Helper: convert Jira ISO timestamp to ADO UTC ISO string
    def _to_ado_dt(jira_ts: str) -> str:
        return _parse_dt(jira_ts).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')

    def _to_ado_date(jira_ts: str) -> str:
        """Return date-only string YYYY-MM-DD from a Jira ISO timestamp."""
        return _parse_dt(jira_ts).strftime('%Y-%m-%d')

    jira_fields = jira_ticket['fields']

    # Current Start Date ← Jira created date
    if ts := jira_fields.get('created'):
        try:
            ado_client.update_field(ado_id, '/fields/Microsoft.VSTS.Scheduling.StartDate', _to_ado_date(ts))
        except Exception as e:
            logging.warning(f"[dates] Could not set StartDate for {jira_key}: {e}")

    # Actual Completion Date ← Jira resolutiondate (overrides the date ADO auto-sets on state change)
    if ts := jira_fields.get('resolutiondate'):
        try:
            ado_client.update_field(ado_id, '/fields/Custom.ActualCompletionDate', _to_ado_dt(ts))
        except Exception as e:
            logging.warning(f"[dates] Could not set ActualCompletionDate for {jira_key}: {e}")

    # Actual Start Date ← Jira created (best proxy; field may not exist in all ADO templates)
    if ts := jira_fields.get('created'):
        try:
            ado_client.update_field(ado_id, '/fields/Custom.ActualStartDate', _to_ado_dt(ts))
        except Exception as e:
            logging.debug(f"[dates] ActualStartDate not available for {jira_key}: {e}")

    # ---- Labels / tags (merge, do not overwrite existing tags) ----
    labels = jira_ticket['fields'].get('labels', [])
    if labels:
        item = ado_client.get_work_item_full(ado_id)
        existing_tags = (item.get('fields', {}).get('System.Tags') or '').strip() if item else ''
        tag_parts = [t.strip() for t in existing_tags.split(';') if t.strip()]
        for label in labels:
            if label not in tag_parts:
                tag_parts.append(label)
        ado_client.update_field(ado_id, '/fields/System.Tags', '; '.join(tag_parts))

    # ---- Attachments ----
    attachment_dict = sync_attachments(ado_id, jira_ticket, jira_client, ado_client)

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

    # ---- Board / Team / Area path: one ADO team per Jira project = one board ----
    # Naming convention: ZZ-Archive-{PROJECT_KEY}  (e.g. ZZ-Archive-CSQA, ZZ-Archive-ICE)
    # The ZZ- prefix sorts archive teams to the bottom of the team list in ADO.
    # Using the Jira project KEY (not name) keeps names short, stable, and consistent.
    jira_project = jira_ticket['fields'].get('project') or {}
    jira_project_key = jira_project.get('key', '')
    if jira_project_key:
        team_name = f'ZZ-Archive-{jira_project_key}'
        area_path = f'{ado_client.project}\\{team_name}'
        if jira_project_key not in _BOARD_SETUP_DONE:
            print(f'[board-setup] Configuring ADO team and board for Jira project {jira_project_key} ...')
            ok_area = ado_client.ensure_area_path(team_name)
            ok_team = ado_client.ensure_team(team_name)
            ok_cfg  = ado_client.configure_team_area(team_name, area_path)
            ok_iter = ado_client.configure_team_iteration(team_name)
            if ok_area and ok_team and ok_cfg and ok_iter:
                org_name = ado_client.organization_url.rstrip('/').split('/')[-1]
                encoded_team = team_name.replace(' ', '%20')
                encoded_project = ado_client.project.replace(' ', '%20')
                print(f'[board-setup] ✅ Team "{team_name}" ready — board: '
                      f'https://dev.azure.com/{org_name}/{encoded_project}'
                      f'/_boards/board/t/{encoded_team}/Stories%20Risks%20and%20Insights')
            else:
                print(f'[board-setup] ⚠️  Some board setup steps failed for "{team_name}" — check logs')
            _BOARD_SETUP_DONE.add(jira_project_key)
        try:
            ado_client.update_field(ado_id, '/fields/System.AreaPath', area_path)
        except Exception as e:
            logging.warning(
                f"[create_or_update_work_item] Could not set area path '{area_path}' "
                f"for {jira_key}: {e}"
            )

    # ---- JiraKey tag — applied last so other updates cannot remove it ----
    ado_client.ensure_jira_key_tag(ado_id, jira_key)

    return ado_id


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Jira to ADO Copy')
    parser.add_argument('--jira-instance', help='Jira source instance name (from URL)')
    parser.add_argument('--jira-filter',   help='Jira source filter ID')
    parser.add_argument('--ado-project',   help='ADO target project name')
    parser.add_argument('--jira-keys',      help='Comma-separated list of Jira keys to process (e.g. CSQA-1,CSQA-5,CSQA-12)')
    parser.add_argument('--retry-failed',   action='store_true', help='Re-process all keys recorded in failed_issues.json')

    args = parser.parse_args()

    # Only prompt for filter if neither --jira-keys nor --retry-failed was supplied
    try:
        while not args.jira_instance:
            args.jira_instance = input("Enter Jira instance name: ").strip()
        if not args.jira_keys and not args.retry_failed:
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

        # --retry-failed: treat the failed_issues.json keys as --jira-keys
        if args.retry_failed:
            failed = load_failed_issues()
            if not failed:
                print('[retry-failed] No failed issues on record. Nothing to do.')
                return
            args.jira_keys = ','.join(failed.keys())
            print(f'[retry-failed] Retrying {len(failed)} failed issues: {list(failed.keys())}')

        # --jira-keys: fetch those issues directly — no filter call needed
        if args.jira_keys:
            target_keys = [k.strip().upper() for k in args.jira_keys.split(',') if k.strip()]
            logging.info(f"[main] Direct mode: fetching {len(target_keys)} issues by key: {target_keys}")

            # Bootstrap: check ADO for existing items before creating anything
            print(f'[bootstrap] Checking ADO for {len(target_keys)} keys...')
            ado_existing = ado_client.bulk_fetch_jira_key_mapping(target_keys)
            for jira_key, ado_id in ado_existing.items():
                mapping[jira_key] = ado_id
                save_issue_mapping(jira_key, ado_id)
            print(f'[bootstrap] Done. {len(ado_existing)} existing items found in ADO.')

            for jira_key in target_keys:
                jira_ticket = jira_client.get_jira_issue(jira_key)
                if jira_ticket is None:
                    logging.error(f"[main] Could not fetch Jira issue {jira_key} — skipping")
                    continue
                logging.info(f'[main] Copying {jira_key} ...')
                try:
                    ado_id = create_or_update_work_item(
                        jira_ticket, ado_client, jira_client, jira_instance,
                        mapping, type_config, state_config, custom_fields
                    )
                    if ado_id:
                        logging.info(f"[main] Finished work item: {ado_id}")
                        logging.info("*" * 80)
                        clear_failed_issue(jira_key)
                except Exception as e:
                    logging.error(f"[main] Failed to process {jira_key}: {str(e)}")
                    if hasattr(e, 'response') and hasattr(e.response, 'text'):
                        logging.info(f"Response: {e.response.text}")
                    save_failed_issue(jira_key, str(e))
            return

        # Fetch filter results from Jira
        jira_df = jira_client.get_filter_items(jira_filter)
        if jira_df is None:
            print(f'No items returned from filter {jira_filter}')
            logging.info(f'No items returned from filter {jira_filter}')
            return None

        jira_issues = jira_df.get('issues')
        logging.info(f"[main] Found {len(jira_issues)} issues in Jira")

        # Bootstrap: resolve all keys against ADO before creating anything
        # This prevents duplicates even when the local mapping cache is missing/stale
        filter_keys = []
        for x in jira_issues:
            ticket = jira_client.get_jira_issue(x['id'])
            if ticket:
                filter_keys.append(ticket['key'])
                x['_ticket'] = ticket  # cache so we don't re-fetch below

        if filter_keys:
            print(f'[bootstrap] Checking ADO for {len(filter_keys)} keys...')
            ado_existing = ado_client.bulk_fetch_jira_key_mapping(filter_keys)
            for jira_key, ado_id in ado_existing.items():
                mapping[jira_key] = ado_id
                save_issue_mapping(jira_key, ado_id)
            print(f'[bootstrap] Done. {len(ado_existing)} existing items found in ADO.')

        for x in jira_issues:
            jira_ticket = x.get('_ticket') or jira_client.get_jira_issue(x['id'])
            if jira_ticket is None:
                continue
            jira_key    = jira_ticket['key']

            logging.info(f'[main] Copying {jira_key} ...')

            try:
                ado_id = create_or_update_work_item(
                    jira_ticket, ado_client, jira_client, jira_instance,
                    mapping, type_config, state_config, custom_fields
                )
                if ado_id:
                    logging.info(f"[main] Finished work item: {ado_id}")
                    logging.info("*" * 80)
            except Exception as e:
                logging.error(f"[main] Failed to process {jira_key}: {str(e)}")
                if hasattr(e, 'response') and hasattr(e.response, 'text'):
                    logging.info(f"Response: {e.response.text}")
                save_failed_issue(jira_key, str(e))
                continue
            else:
                clear_failed_issue(jira_key)

    except Exception as e:
        logging.error(f"Error: {str(e)}")
        if hasattr(e, 'response') and hasattr(e.response, 'text'):
            logging.info(f"Response: {e.response.text}")


if __name__ == "__main__":
    main()
