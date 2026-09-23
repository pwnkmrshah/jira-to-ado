import os
import json
import base64
import time
import requests
from requests import RequestException
from dataclasses import dataclass
import pandas
import re
from datetime import datetime
import logging
from pathlib import Path
from urllib.parse import quote
from urllib.parse import urlparse, parse_qs, unquote

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def _load_error_kb() -> dict:
    """Load ADO error knowledge base from config/ado_error_kb.json."""
    kb_path = Path(__file__).parent.parent / 'config' / 'ado_error_kb.json'
    try:
        return json.loads(kb_path.read_text())
    except Exception:
        return {}

def _friendly_ado_error(msg: str) -> str:
    """Replace raw ADO error codes with plain-English explanations from the KB file."""
    kb = _load_error_kb()
    for code, entry in kb.items():
        if code in msg:
            text = entry.get('short', msg)
            if '{type}' in text:
                m = re.search(r"work item type '([^']+)'", msg, re.IGNORECASE)
                type_name = m.group(1) if m else 'unknown'
                text = text.format(type=type_name)
            return text
    return msg


class WorkItemsSizeLimitExceeded(RuntimeError):
    pass

class WorkItemTypeDisabledError(RuntimeError):
    """Raised when VS403074: the target work item type is disabled in this ADO project."""
    def __init__(self, type_name: str):
        self.type_name = type_name
        super().__init__(f"Work item type '{type_name}' is disabled in this ADO project.")


@dataclass
class AzureDevOpsConfig:
    """Configuration for Azure DevOps connection"""
    organization: str
    credentials: str = None
    project: str = None
    access_token: str = None
    username: str = None

# If running from Azure Pipeline, use the SYSTEM_ACCESSTOKEN. don't call load_ado_config
def load_ado_config() -> AzureDevOpsConfig:
    """Load configuration from environment variables or user input"""
    # Env vars take priority — required on Render, where config/ado_config.json
    # doesn't exist (it's gitignored and only present on local dev machines).
    env_org = os.environ.get('ADO_ORG', '')
    env_pat = os.environ.get('ADO_PAT', '')
    if env_org and env_pat:
        return AzureDevOpsConfig(
            organization=env_org,
            access_token=env_pat,
            username=os.environ.get('ADO_USERNAME', ''),
        )

    config_path = Path(__file__).parent.parent / "config" / "ado_config.json"

    # Fall back to config file
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config_data = json.load(f)

                return AzureDevOpsConfig(**config_data)

        except Exception as e:
            logging.warning(f"[load_ado_config] Warning: Could not read config file: {e}")

    raise RuntimeError(
        "ADO credentials not found. Set ADO_ORG and ADO_PAT environment variables, "
        "or provide config/ado_config.json."
    )

class AzureDevOpsClient:
    """Client for interacting with Azure DevOps"""
    
    def __init__(self, config: AzureDevOpsConfig, project):
        # Initialize the Azure DevOps client with configuration
        self.username = config.username
        self.access_token = config.access_token
        self.organization_url = f"https://dev.azure.com/{config.organization}"
        self.api_version = '7.0-preview.3'
        self.project = project
        #self.credentials = base64.b64encode(f":{config.credentials}".encode()).decode()

    def ado_api_call(self, method, url, payload=None, _retry_on_state_error=True):
        # Makes a call to the Azure DevOps API with error handling

        auth = (self.username, self.access_token)
        #credentials = self.credentials
        response = None  # Initialize response to None.

        try:
            data = json.dumps(payload)
        except TypeError:
            data = payload

        match method:
            case 'GET':
                headers = {'Content-Type': 'application/json'}
            case 'POST':
                headers = {'Content-Type': 'application/json'}
            case 'POSTP':           # If method is POSTP, then the content-type must be json-patch or the REST API fails
                method = 'POST'
                headers = {'Content-Type': 'application/json-patch+json'}
            case 'PATCH':
                headers = {'Content-Type': 'application/json-patch+json'}
            case _:
                raise ValueError("Method must be either 'GET', 'POST', or 'PATCH'")

        # Retry transient failures (no timeout previously meant a real ADO outage or a
        # slow/stuck connection could hang forever or silently derail a multi-hour migration
        # partway through). Connection errors/timeouts and 429/502/503/504 are retried with
        # backoff; anything else (4xx validation errors, etc.) fails immediately as before.
        _MAX_ATTEMPTS = 4
        _BACKOFFS = [2, 5, 10]
        for attempt in range(_MAX_ATTEMPTS):
            try:
                logging.debug(f"[ado_api_call] Making {method} request to {url} (attempt {attempt + 1})")
                response = requests.request(
                    method, auth=auth, url=url, headers=headers,
                    data=data if payload else None, timeout=60,
                )
                response.raise_for_status()
                break  # success
            except RequestException as e:
                is_transient = response is None or response.status_code in (429, 502, 503, 504)
                if is_transient and attempt < _MAX_ATTEMPTS - 1:
                    wait = _BACKOFFS[min(attempt, len(_BACKOFFS) - 1)]
                    status = response.status_code if response is not None else type(e).__name__
                    logging.warning(
                        f"[ado_api_call] Transient error ({status}) on {url} — "
                        f"retrying in {wait}s (attempt {attempt + 1}/{_MAX_ATTEMPTS})"
                    )
                    time.sleep(wait)
                    continue

                logging.error(f"[ado_api_call] Error: {response.status_code if response is not None else 'No response'} for url: {url}")
                logging.error(f"[ado_api_call] Error response: {response.text if response is not None else 'No response'}")
                if response is not None and response.status_code == 400 and 'VS402337' in response.text:
                    logging.error("[ado_api_call] Work item size limit exceeded")
                    raise WorkItemsSizeLimitExceeded()
                # Extract the human-readable message from the ADO response body
                if response is not None:
                    try:
                        body = response.json()
                        ado_msg = (body.get('message') or '').strip()
                        if ado_msg:
                            if 'VS403074' in ado_msg:
                                m = re.search(r"work item type '([^']+)'", ado_msg, re.IGNORECASE)
                                raise WorkItemTypeDisabledError(m.group(1) if m else 'unknown') from None
                            # Self-heal: a work item whose System.State is already invalid for its
                            # CURRENT type (e.g. left over from an earlier bug, or after a type
                            # change) fails FULL-DOCUMENT validation on every subsequent PATCH —
                            # even ones that don't touch State at all (iteration path, dates, ...).
                            # Repair the state once and retry this exact request instead of
                            # letting every unrelated field update fail forever.
                            if (_retry_on_state_error and method == 'PATCH'
                                    and "field 'state'" in ado_msg.lower()
                                    and 'not in the list of supported values' in ado_msg.lower()):
                                m = re.search(r'/workitems/(\d+)\b', url)
                                if m and self._repair_invalid_state(int(m.group(1))):
                                    return self.ado_api_call(method, url, payload, _retry_on_state_error=False)
                            raise RuntimeError(_friendly_ado_error(ado_msg)) from None
                    except (ValueError, AttributeError):
                        pass
                raise e

        try:
            return response.json()
        except ValueError:
            logging.error(f"[ado_api_call] ValueError parsing response: {response}")
            return response

    def _repair_invalid_state(self, item_id: int) -> bool:
        """Best-effort self-heal for a work item whose System.State is invalid for its
        current type. Resolves a valid state via resolve_valid_state() and PATCHes just
        that field (bypassRules=true) so the item stops failing full-document validation
        on every other field update. Returns True if a repair PATCH was sent.
        """
        try:
            item = self.get_work_item_full(item_id)
            fields = (item or {}).get('fields', {})
            current_type  = fields.get('System.WorkItemType')
            current_state = fields.get('System.State')
            if not current_type or not current_state:
                return False
            fixed_state = self.resolve_valid_state(current_type, current_state)
            if fixed_state == current_state:
                return False  # already "valid" per our resolver — repairing again won't help
            url = (
                f'{self.organization_url}/{self.project}/_apis/wit/workitems/{item_id}'
                f'?api-version={self.api_version}&bypassRules=true'
            )
            payload = [{'op': 'add', 'path': '/fields/System.State', 'value': fixed_state}]
            requests.request(
                'PATCH', url, auth=(self.username, self.access_token),
                headers={'Content-Type': 'application/json-patch+json'},
                data=json.dumps(payload),
            ).raise_for_status()
            logging.info(f"[state-repair] \u2705 {item_id}: invalid state '{current_state}' \u2192 '{fixed_state}' ({current_type})")
            return True
        except Exception as e:
            logging.warning(f"[state-repair] Could not repair state on {item_id}: {e}")
            return False


    def format_date(self, date_string):
        # Format a string from Jira into a date
        date_string = datetime.strptime(date_string, '%Y-%m-%dT%H:%M:%S.%f%z')  # Convert string to datetime
        date_string = date_string.strftime('%Y-%m-%d %H:%M')                    # Re-format datetime
        date_string = ''.join((date_string, ' UTC-6'))

        logging.debug(f"[format_date] Formatted date: {date_string}")
        return date_string

    def get_project_work_item_types(self) -> list[str]:
        """Return the names of all enabled work item types in self.project."""
        url = f'{self.organization_url}/{quote(self.project)}/_apis/wit/workitemtypes?api-version=7.0'
        try:
            resp = self.ado_api_call('GET', url)
            return [t['name'] for t in resp.get('value', [])]
        except Exception as e:
            logging.warning(f"[get_project_work_item_types] Could not fetch types: {e}")
            return []

    def get_item_info(self, item_id):
        # Retrieves all information about a specific work item
        url = f'{self.organization_url}/_apis/wit/workitems?ids={item_id}&api-version={self.api_version}'
        response = self.ado_api_call('GET', url)

        if response is None:
            return None

        # Normalize the response from the API
        fields = response['value']
        norm_df = pandas.json_normalize(fields)

        logging.debug(f"[get_item_info] Work item info for: {item_id}")
        return norm_df

    def create_item(
            self
            ,type
            ,area_path = None
            ,title = None
            ,state = None
            ,assigned_to = None
            ,description = None
            ):

        # Create an ado work item
        url = f'{self.organization_url}/{self.project}/_apis/wit/workitems/${type}?api-version={self.api_version}'

        payload = [
            {
                'op': 'add',
                'path': '/fields/System.Title',
                'value': title
            },
        ]
        # ADO rejects null values; omit the field entirely when no description is supplied
        if description is not None:
            payload += [
                {
                    'op': 'add',
                    'path': '/fields/System.Description',
                    'value': description or '',
                },
                {
                    'op': 'add',
                    'path': '/multilineFieldsFormat/System.Description',
                    'value': 'Markdown',
                },
            ]

        if assigned_to:
            assigned_to_string = {
                'op': 'add',
                'path': '/fields/System.AssignedTo',
                'value': assigned_to
            }
            payload.append(assigned_to_string)

        logging.debug(f"[create_item] Creating work item: {title}")
        response = self.ado_api_call('POSTP', url, payload)

        if response is None:
            return None

        # Get id of newly created work item
        item_id = response['id']

        # Set additional fields in the new work item based on user input
        url = f'{self.organization_url}/{self.project}/_apis/wit/workitems/{item_id}?api-version={self.api_version}'  # 6.0

        if state is None:
            return response
        
        # Other fields for the work item
        resolved_state = self.resolve_valid_state(type, state)
        payload = [
            {
                'op': 'add',
                'path': '/fields/System.State',
                'value': resolved_state
            }
        ]
        logging.debug(f"[create_item] Setting state to: {resolved_state}")
        try:
            response = self.ado_api_call('PATCH', url, payload)
        except RuntimeError as e:
            if 'State' in str(e) and 'not in the list of supported values' in str(e):
                fallback_state = self.resolve_valid_state(type, 'Active')
                logging.warning(f"[create_item] State '{resolved_state}' not valid for this work item type — falling back to '{fallback_state}'")
                payload[0]['value'] = fallback_state
                response = self.ado_api_call('PATCH', url, payload)
            else:
                raise

        return response

    def add_comments(self, item_id, attachment_dict, comments):
        logging.debug(f"[add_comments] Adding comments to work item: {item_id}")

        # Add comments to a work item from dict, replace any image references with the correct ADO attachment reference
        url = f'{self.organization_url}/{self.project}/_apis/wit/workItems/{item_id}/comments?api-version={self.api_version}'  # api-version=6.1-preview.3
        sorted_comments = sorted(comments, key=lambda d: d['created'])
        response = []

        # For each comment on the ticket
        for x in sorted_comments:
            author = x['author']['displayName']
            created = self.format_date(x['created'])
            created = ''.join((' (',created,'): '))
            description = x['renderedBody']

            # Update the work item comment if it contains any image links
            if '<img src=' in description.lower():
                # Find all image HTML references
                img_links = re.findall('<img src="([^"]+)" alt="([^"]+)"', description)
            
                # For each image reference in the comment
                for a in img_links:
                    # Replace the image source link with the new ADO attachment link
                    description = description.replace(a[0], attachment_dict[a[1]])

            # Update the work item comment if it contains any thumbnail links
            if '<jira-attachment-thumbnail' in description.lower():
                # Find all thumbnail HTML references
                thumbnail_links = re.findall('<img src="([^"]+)" data-attachment-name="([^"]+)"', description)

                # For each thumbnail reference in the comment
                for a in thumbnail_links:
                    # Replace the thumbnail source link with the new ADO attachment link
                    description = description.replace(a[0], attachment_dict[a[1]])

            # Get the comment body
            payload = {
                'text': ''.join((author, created, description))
            }
            # Write the comment to the work item specified
            response = self.ado_api_call('POST', url, payload)

            # Modify comment to be created by original author
            #patch_url = f'{self.organization_url}/{self.project}/_apis/wit/workItems/{item_id}/comments/{response['id']}?bypassRules=true&api-version={self.api_version}'
            '''
            patch_payload = [
                {
                    'op': 'add',
                    'path': '/fields/System.createdBy',
                    'value': 'scott.bowman@healthcatalyst.com'
                }
            ]
            '''
            # ***This patch does not work, need to revisit***
            #patch_response = self.ado_api_call('PATCH', patch_url, patch_payload)

        logging.debug(f"[add_comments] Added comments to work item: {item_id}")
        return response

    def create_attachment(self, filename, attachment):
        # Create an attachment from the provided file information
        # URL-encode the filename to handle special chars (e.g. Confluence media blob
        # filenames that contain '#media-blob-url=...' fragments which break the URL)
        safe_filename = quote(filename, safe='')
        url = f'{self.organization_url}/{self.project}/_apis/wit/attachments?fileName={safe_filename}&api-version={self.api_version}'

        response = self.ado_api_call('POST', url, attachment)
        logging.debug(f"[create_attachment] Created attachment: {filename}")
        return response
    
    def add_attachment(self, item_id, attachment_url, author, created_date, filename=None):
        # Add an attachment to a work item
        url = f'{self.organization_url}/{self.project}/_apis/wit/workitems/{item_id}?api-version={self.api_version}'
        created_date = self.format_date(created_date)
        comment = f'Created by {author} on {created_date}'
        attributes = {'comment': comment}
        if filename:
            attributes['name'] = filename  # stored for idempotent dedup on re-runs
        payload = [
            {
                'op': 'add',
                'path': '/relations/-',
                'value': {
                    'rel': 'AttachedFile',
                    'url': attachment_url,
                    'attributes': attributes
                }
            }
        ]

        response = self.ado_api_call('PATCH', url, payload)

        logging.debug(f"[add_attachment] Linked attachment to work item: {item_id}")
        return response

    def update_field(self, item_id, field, text):
        logging.debug(f"[update_field] Updating field on {item_id}: {field}")

        # Update a field
        url = f'{self.organization_url}/{self.project}/_apis/wit/workitems/{item_id}?api-version={self.api_version}'  # 6.0

        payload = [
            {
                'op': 'replace',  # 'add' silently fails on already-existing fields; 'replace' always wins
                'path': field,
                'value': text
            }
        ]
        response = self.ado_api_call('PATCH', url, payload)
        logging.debug(f"[update_field] Updated field: {field}")

        return response

    def change_work_item_type(self, item_id: int, new_type: str) -> bool:
        """Change work item type using bypassRules=true — required by ADO for type changes."""
        url = (
            f'{self.organization_url}/{self.project}'
            f'/_apis/wit/workitems/{item_id}?bypassRules=true&api-version={self.api_version}'
        )
        payload = [{'op': 'add', 'path': '/fields/System.WorkItemType', 'value': new_type}]
        try:
            response = self.ado_api_call('PATCH', url, payload)
            if response is not None:
                logging.info(f"[change_work_item_type] ✅ {item_id} → '{new_type}'")
                return True
            logging.warning(f"[change_work_item_type] ❌ {item_id} → '{new_type}': API returned None")
            return False
        except Exception as exc:
            logging.warning(f"[change_work_item_type] ❌ {item_id} → '{new_type}': {exc}")
            return False

    def add_hyperlink(self, item_id, hyperlink, author = None, last_update = None):
        # Add a hyperlink to a work item
        url = f'{self.organization_url}/{self.project}/_apis/wit/workitems/{item_id}?api-version={self.api_version}'
        comment = 'Created by Jira to ADO copy'

        if(author):
            last_update = self.format_date(last_update)
            #last_update = datetime.strptime(last_update, '%Y-%m-%dT%H:%M:%S.%f%z')  # Convert string to datetime
            #last_update = last_update.strftime('%Y-%m-%d %H:%M')                    # Re-format datetime
            comment = f'Created by {author} on {last_update}'
        
        payload = [
            {
                'op': 'add',
                'path': '/relations/-',
                'value': {
                    'rel': 'Hyperlink',
                    'url': hyperlink,
                    'attributes': {
                        'comment': comment
                    }
                }
            }
        ]
        response = self.ado_api_call('PATCH', url, payload)
        logging.debug(f"[add_hyperlink] Added hyperlink to work item: {item_id}")

        return response

    def append_description(self, item_id, add_text):
        # Append to the description of a work item

        # Get the current description
        response = self.get_item_info(item_id)
        if 'fields.System.Description' in response.columns:
            description = response.at[0, 'fields.System.Description']
        else:
            description = None
        if not description:
            description = ''

        # Add the new text to the description
        add_text = ''.join((description, '<br />' if description else '', add_text))
        #url = f'{self.organization_url}/{self.project}/_apis/wit/workitems/{item_id}?api-version={self.api_version}'

        # Update the description
        response =self.update_field(item_id, '/fields/System.Description', add_text)

        '''
        payload = [
            {
                'op': 'add',
                'path': '/fields/System.Description',
                'value': description
            },
            {
                'op': 'add',
                'path': '/multilineFieldsFormat/System.Description',
                'value': 'Markdown'
            }
        ]
        response = self.ado_api_call('PATCH', url, payload)
        '''
        logging.debug(f"[append_description] Modified description to work item: {item_id}")
        return response

    # ------------------------------------------------------------------
    # Idempotency helpers
    # ------------------------------------------------------------------

    def find_work_item_by_jira_key(self, jira_key: str):
        """Search ADO via WIQL for a work item tagged with JiraKey=<jira_key>.

        Uses CONTAINS for the WIQL query (ADO doesn't support exact tag match),
        then validates the returned item actually carries the exact tag to avoid
        false positives when an ADO item has multiple JiraKey= tags (phantom tags
        from interrupted migration runs).

        Returns the integer ADO work item ID, or None if not found.
        """
        url = f'{self.organization_url}/{self.project}/_apis/wit/wiql?api-version=7.0'
        query = {
            "query": (
                f"SELECT [System.Id] FROM WorkItems "
                f"WHERE [System.Tags] CONTAINS 'JiraKey={jira_key}' "
                f"ORDER BY [System.Id] ASC"
            )
        }
        expected_tag = f'JiraKey={jira_key}'
        try:
            response = self.ado_api_call('POST', url, query)
            if response and response.get('workItems'):
                # Validate each candidate — CONTAINS can match phantom tags on shared items.
                # Return the first ADO item whose tag list contains an exact match.
                ids = ','.join(str(w['id']) for w in response['workItems'])
                detail = self.ado_api_call('GET',
                    f'{self.organization_url}/_apis/wit/workitems'
                    f'?ids={ids}&fields=System.Id,System.Tags&api-version={self.api_version}')
                if detail and detail.get('value'):
                    for item in detail['value']:
                        tags_str = item.get('fields', {}).get('System.Tags', '') or ''
                        tag_set = {t.strip() for t in tags_str.split(';') if t.strip()}
                        if expected_tag in tag_set:
                            return item['id']
        except Exception as e:
            logging.warning(f"[find_work_item_by_jira_key] Search failed for {jira_key}: {e}")
        return None

    def bulk_fetch_jira_key_mapping(self, jira_keys: list, skip_title_search: bool = False) -> dict:
        """Fetch ADO work item IDs for all given Jira keys.

        Two-pass strategy — both passes run for ALL keys so duplicates are resolved:
          Pass 1 — Tag search:   WHERE [System.Tags] CONTAINS 'JiraKey=KEY'
          Pass 2 — Title search: WHERE [System.Title] CONTAINS '[KEY]'

        When multiple ADO items match (duplicates), lowest ID (oldest) wins and
        gets tagged with JiraKey= so future runs find it via the fast tag path.

        Returns {jira_key: ado_id} for every key found in ADO.
        """
        import re as _re
        if not jira_keys:
            return {}

        candidates = {k: [] for k in jira_keys}
        batch_size = 10

        # ---- Pass 1: tag search ----
        for i in range(0, len(jira_keys), batch_size):
            batch = jira_keys[i:i + batch_size]
            conditions = " OR ".join(f"[System.Tags] CONTAINS 'JiraKey={k}'" for k in batch)
            url = f'{self.organization_url}/{self.project}/_apis/wit/wiql?api-version=7.0'
            try:
                response = self.ado_api_call('POST', url,
                    {"query": f"SELECT [System.Id],[System.Tags] FROM WorkItems WHERE {conditions}"})
                if not response or not response.get('workItems'):
                    continue
                ids = ','.join(str(w['id']) for w in response['workItems'])
                detail = self.ado_api_call('GET',
                    f'{self.organization_url}/_apis/wit/workitems'
                    f'?ids={ids}&fields=System.Id,System.Tags&api-version={self.api_version}')
                if not detail or 'value' not in detail:
                    continue
                for item in detail['value']:
                    tags_str = item.get('fields', {}).get('System.Tags', '') or ''
                    for tag in tags_str.split(';'):
                        tag = tag.strip()
                        if tag.startswith('JiraKey='):
                            key = tag[len('JiraKey='):]
                            # Do NOT break — one ADO item may carry multiple JiraKey= tags
                            # (phantom tags from interrupted runs). Scan all of them so
                            # every key present on the item is properly attributed.
                            if key in candidates and item['id'] not in candidates[key]:
                                candidates[key].append(item['id'])
            except Exception as e:
                logging.warning(f"[bulk_fetch_jira_key_mapping] Tag batch {i//batch_size+1} failed: {e}")

        # ---- Pass 2: title search (catches untagged originals) ----
        # Only search keys not already found via tags; if skip_title_search is set,
        # bypass entirely (safe for fresh migrations where no untagged ADO items exist).
        if skip_title_search:
            keys_for_title = []
        else:
            keys_for_title = [k for k in jira_keys if not candidates[k]]
        for i in range(0, len(keys_for_title), batch_size):
            batch = keys_for_title[i:i + batch_size]
            conditions = " OR ".join(f"[System.Title] CONTAINS '[{k}]'" for k in batch)
            url = f'{self.organization_url}/{self.project}/_apis/wit/wiql?api-version=7.0'
            try:
                response = self.ado_api_call('POST', url,
                    {"query": f"SELECT [System.Id],[System.Title] FROM WorkItems WHERE {conditions}"})
                if not response or not response.get('workItems'):
                    continue
                ids = ','.join(str(w['id']) for w in response['workItems'])
                detail = self.ado_api_call('GET',
                    f'{self.organization_url}/_apis/wit/workitems'
                    f'?ids={ids}&fields=System.Id,System.Title&api-version={self.api_version}')
                if not detail or 'value' not in detail:
                    continue
                for item in detail['value']:
                    title = item.get('fields', {}).get('System.Title', '') or ''
                    tokens = _re.findall(r'\[([A-Z]+-\d+)\]', title)
                    # Only match the LAST [KEY] token in the title.
                    # ADO titles have the format "[PARENT-KEY] [CHILD-KEY] Summary"
                    # for child issues. Using only the last token prevents the
                    # parent key from being falsely matched to the child's ADO item,
                    # which would create phantom duplicate mappings.
                    if tokens:
                        last_token = tokens[-1]
                        if last_token in candidates and item['id'] not in candidates[last_token]:
                            candidates[last_token].append(item['id'])
            except Exception as e:
                logging.warning(f"[bulk_fetch_jira_key_mapping] Title batch {i//batch_size+1} failed: {e}")

        # ---- Resolve: lowest ID = canonical; tag it ----
        result = {}
        for jira_key, ids in candidates.items():
            if not ids:
                continue
            canonical_id = min(ids)
            result[jira_key] = canonical_id
            if len(ids) > 1:
                logging.warning(
                    f"[bulk_fetch_jira_key_mapping] {jira_key}: {len(ids)} items found "
                    f"{sorted(ids)} — using oldest {canonical_id}"
                )
            try:
                self.ensure_jira_key_tag(canonical_id, jira_key)
            except Exception as e:
                logging.warning(f"[bulk_fetch_jira_key_mapping] Could not tag {canonical_id}: {e}")
        return result

    def get_work_item_full(self, ado_id: int):
        """Return a work item with fields and relations expanded, or None on error."""
        url = (
            f'{self.organization_url}/_apis/wit/workitems/{ado_id}'
            f'?$expand=relations&api-version={self.api_version}'
        )
        try:
            return self.ado_api_call('GET', url)
        except Exception as e:
            logging.warning(f"[get_work_item_full] Could not fetch item {ado_id}: {e}")
            return None

    def item_exists(self, ado_id: int) -> bool:
        """Return True if the ADO work item still exists."""
        item = self.get_work_item_full(ado_id)
        return item is not None and 'id' in item

    def get_work_item_comment_texts(self, ado_id: int) -> list:
        """Return a list of existing comment text strings for deduplication.

        Fetches all pages (ADO returns max 200 per page via continuationToken).
        """
        texts = []
        url = (
            f'{self.organization_url}/{self.project}/_apis/wit/workItems/{ado_id}'
            f'/comments?api-version={self.api_version}'
        )
        try:
            while url:
                response = self.ado_api_call('GET', url)
                if not response or 'comments' not in response:
                    break
                texts.extend(c.get('text', '') for c in response['comments'])
                token = response.get('continuationToken')
                if token:
                    url = (
                        f'{self.organization_url}/{self.project}/_apis/wit/workItems/{ado_id}'
                        f'/comments?continuationToken={token}&api-version={self.api_version}'
                    )
                else:
                    url = None
        except Exception as e:
            logging.warning(f"[get_work_item_comment_texts] Could not fetch comments for {ado_id}: {e}")
        return texts

    def get_work_item_relation_urls(self, ado_id: int) -> list:
        """Return a list of existing relation URL strings for deduplication."""
        item = self.get_work_item_full(ado_id)
        if item and 'relations' in item:
            return [r.get('url', '') for r in item.get('relations', [])]
        return []

    def _attachment_name_from_relation(self, relation: dict) -> str:
        """Best-effort filename extraction for an AttachedFile relation."""
        attrs = relation.get('attributes') or {}
        if attrs.get('name'):
            return attrs['name']

        rel_url = relation.get('url', '')
        try:
            parsed = urlparse(rel_url)
            qs = parse_qs(parsed.query)
            if qs.get('fileName'):
                return unquote(qs['fileName'][0])
            basename = unquote(parsed.path.rsplit('/', 1)[-1])
            return basename if basename and basename != 'attachments' else ''
        except Exception:
            return ''

    def _attachment_signature(self, relation: dict):
        """Stable signature used to detect duplicate attachment relations."""
        attrs = relation.get('attributes') or {}
        name = (self._attachment_name_from_relation(relation) or '').strip().lower()
        rel_url = relation.get('url', '')
        try:
            parsed = urlparse(rel_url)
            canonical_url = f'{parsed.scheme}://{parsed.netloc}{parsed.path}'.lower()
        except Exception:
            canonical_url = rel_url.lower()

        # Filename is the most stable identity across reruns (URL changes on re-upload,
        # author/comment strings can vary between historical migrations).
        if name:
            return ('name', name)
        return ('url', canonical_url)

    def remove_duplicate_attachment_relations(self, item_id: int) -> int:
        """Remove duplicate AttachedFile relations from a work item.

        Duplicates are detected by logical attachment signature; relation indices
        are removed in descending order so patch offsets remain valid.

        Returns number of removed relations.
        """
        item = self.get_work_item_full(item_id)
        relations = (item or {}).get('relations') or []
        seen = {}
        dup_indices = []

        for idx, rel in enumerate(relations):
            if rel.get('rel') != 'AttachedFile':
                continue
            sig = self._attachment_signature(rel)
            if sig in seen:
                dup_indices.append(idx)
            else:
                seen[sig] = idx

        if not dup_indices:
            return 0

        url = (
            f'{self.organization_url}/{self.project}/_apis/wit/workitems/{item_id}'
            f'?api-version={self.api_version}'
        )
        payload = [
            {'op': 'remove', 'path': f'/relations/{idx}'}
            for idx in sorted(dup_indices, reverse=True)
        ]
        self.ado_api_call('PATCH', url, payload)
        return len(dup_indices)

    def ensure_jira_key_tag(self, ado_id: int, jira_key: str) -> None:
        """Add 'JiraKey=<jira_key>' tag to the work item if not already present.

        Called last so it is never overwritten by other field updates.
        """
        item = self.get_work_item_full(ado_id)
        existing_tags = ''
        if item:
            existing_tags = (item.get('fields', {}).get('System.Tags') or '').strip()
        tag = f'JiraKey={jira_key}'
        tag_set = {t.strip() for t in existing_tags.split(';') if t.strip()}
        if tag not in tag_set:  # exact set membership — avoids 'JiraKey=ENG-9' matching 'JiraKey=ENG-92'
            tag_parts = [t.strip() for t in existing_tags.split(';') if t.strip()] if existing_tags else []
            tag_parts.append(tag)
            self.update_field(ado_id, '/fields/System.Tags', '; '.join(tag_parts))
            logging.debug(f"[ensure_jira_key_tag] Added tag '{tag}' to work item {ado_id}")

    def ensure_area_path(self, area_name: str) -> bool:
        """Create the area node under the project if it does not already exist.

        Uses api-version=7.0 (not preview) which supports classification node creation.
        Returns True if the node exists or was successfully created, False on error.
        """
        import requests as _req
        url = (
            f'{self.organization_url}/{self.project}/_apis/wit/classificationnodes/areas'
            f'?api-version=7.0'
        )
        auth = (self.username, self.access_token)
        headers = {'Content-Type': 'application/json'}
        import json as _json
        data = _json.dumps({'name': area_name})
        try:
            response = _req.post(url, auth=auth, headers=headers, data=data)
            if response.status_code in (200, 201):
                logging.info(f"[ensure_area_path] Created area node '{area_name}'")
                return True
            if response.status_code == 409:
                logging.debug(f"[ensure_area_path] Area node '{area_name}' already exists")
                return True
            logging.warning(
                f"[ensure_area_path] Could not create area node '{area_name}': "
                f"{response.status_code} {response.text}"
            )
            return False
        except Exception as e:
            logging.warning(f"[ensure_area_path] Could not create area node '{area_name}': {e}")
            return False

    def ensure_team(self, team_name: str) -> bool:
        """Create an ADO team named *team_name* under the project if it does not already exist.

        Each team gets its own board automatically in ADO.
        Returns True if the team exists or was created, False on error.
        """
        import requests as _req, json as _json
        auth = (self.username, self.access_token)
        headers = {'Content-Type': 'application/json'}

        # Check if team already exists
        get_url = (
            f'{self.organization_url}/_apis/projects/{self.project}/teams'
            f'?api-version=7.0'
        )
        try:
            resp = _req.get(get_url, auth=auth, headers=headers)
            if resp.ok:
                existing = {t['name'].lower() for t in resp.json().get('value', [])}
                if team_name.lower() in existing:
                    logging.debug(f"[ensure_team] Team '{team_name}' already exists")
                    return True
        except Exception:
            pass  # fall through to create

        # Create the team
        create_url = (
            f'{self.organization_url}/_apis/projects/{self.project}/teams'
            f'?api-version=7.0'
        )
        try:
            resp = _req.post(create_url, auth=auth, headers=headers,
                             data=_json.dumps({'name': team_name}))
            if resp.status_code in (200, 201):
                logging.info(f"[ensure_team] Created team '{team_name}'")
                return True
            if resp.status_code == 409:
                logging.debug(f"[ensure_team] Team '{team_name}' already exists (409)")
                return True
            logging.warning(
                f"[ensure_team] Could not create team '{team_name}': "
                f"{resp.status_code} {resp.text}"
            )
            return False
        except Exception as e:
            logging.warning(f"[ensure_team] Could not create team '{team_name}': {e}")
            return False

    def configure_team_area(self, team_name: str, area_path: str) -> bool:
        """Set *area_path* as the default (and only) area for *team_name*.

        This makes work items in that area path appear on that team's board.
        """
        import requests as _req, json as _json
        auth = (self.username, self.access_token)
        headers = {'Content-Type': 'application/json'}
        url = (
            f'{self.organization_url}/{self.project}/{team_name}'
            f'/_apis/work/teamsettings/teamfieldvalues?api-version=7.0'
        )
        payload = {
            'defaultValue': area_path,
            'values': [{'value': area_path, 'includeChildren': True}]
        }
        try:
            resp = _req.patch(url, auth=auth, headers=headers,
                              data=_json.dumps(payload))
            if resp.ok:
                logging.info(f"[configure_team_area] Team '{team_name}' -> area '{area_path}'")
                return True
            logging.warning(
                f"[configure_team_area] Could not set area for team '{team_name}': "
                f"{resp.status_code} {resp.text}"
            )
            return False
        except Exception as e:
            logging.warning(f"[configure_team_area] Error for team '{team_name}': {e}")
            return False

    def ensure_team_iteration_node(self, team_name: str) -> str | None:
        """Create a dedicated child iteration node for *team_name* (if missing), and
        add it to the team's SELECTED iterations (_apis/work/teamsettings/iterations).

        configure_team_iteration() alone only sets 'backlogIteration' (a broad filter
        for the Backlog page) — it does NOT add anything to the team's selected
        iterations. Without a selected iteration, the Kanban BOARD stays empty even
        when items exist under the correct AreaPath, because Boards require each
        item's IterationPath to match one of the team's explicitly selected iterations.

        Returns the full iteration path (e.g. "<Project>\\<team_name>") to assign on
        each migrated work item's System.IterationPath, or None on failure.
        """
        import requests as _req, json as _json
        auth = (self.username, self.access_token)
        headers = {'Content-Type': 'application/json'}

        # Step 1: does a child iteration named <team_name> already exist under root?
        try:
            tree_resp = _req.get(
                f'{self.organization_url}/{self.project}'
                f'/_apis/wit/classificationnodes/iterations?$depth=1&api-version=7.0',
                auth=auth, headers=headers
            )
            if not tree_resp.ok:
                logging.warning(f"[ensure_team_iteration_node] Could not fetch iteration tree")
                return None
            tree = tree_resp.json()
            existing = next((c for c in (tree.get('children') or []) if c.get('name') == team_name), None)
            if existing:
                node_guid = existing.get('identifier')
            else:
                create_resp = _req.post(
                    f'{self.organization_url}/{self.project}'
                    f'/_apis/wit/classificationnodes/iterations?api-version=7.0',
                    auth=auth, headers=headers,
                    data=_json.dumps({'name': team_name})
                )
                if not create_resp.ok:
                    logging.warning(
                        f"[ensure_team_iteration_node] Could not create iteration '{team_name}': "
                        f"{create_resp.status_code} {create_resp.text[:200]}"
                    )
                    return None
                node_guid = create_resp.json().get('identifier')
        except Exception as e:
            logging.warning(f"[ensure_team_iteration_node] Error resolving iteration node: {e}")
            return None

        if not node_guid:
            return None

        # Step 2: add it to the team's selected iterations (idempotent — a 404/409 on
        # "already selected" is expected and harmless on repeat runs)
        try:
            add_resp = _req.post(
                f'{self.organization_url}/{self.project}/{team_name}'
                f'/_apis/work/teamsettings/iterations?api-version=7.0',
                auth=auth, headers=headers,
                data=_json.dumps({'id': node_guid})
            )
            if not add_resp.ok and 'already' not in add_resp.text.lower():
                logging.warning(
                    f"[ensure_team_iteration_node] Could not add iteration to team '{team_name}': "
                    f"{add_resp.status_code} {add_resp.text[:200]}"
                )
        except Exception as e:
            logging.warning(f"[ensure_team_iteration_node] Error adding iteration to team: {e}")

        iteration_path = f'{self.project}\\{team_name}'
        logging.info(f"[ensure_team_iteration_node] Team '{team_name}' iteration ready: '{iteration_path}'")
        return iteration_path

    def configure_team_iteration(self, team_name: str) -> bool:
        """Set the backlog iteration for *team_name* to the project root iteration.

        Without this ADO shows 'Configuration required - No backlog iteration path'
        and the Kanban board is empty even when work items exist.

        Key learnings:
        - backlogIteration must be the PROJECT ROOT iteration (not a child like 'Archive Backlog')
          so that items with the default IterationPath = project root appear on the board.
          ADO Kanban only shows items whose IterationPath is under the team's backlogIteration.
        - backlogIteration must be sent as a plain GUID string (not {"id": "..."})
        - The root iteration GUID is fetched from classificationnodes with $depth=0
        """
        import requests as _req, json as _json
        auth = (self.username, self.access_token)
        headers = {'Content-Type': 'application/json'}

        # Step 1: get the project root iteration GUID
        try:
            tree_resp = _req.get(
                f'{self.organization_url}/{self.project}'
                f'/_apis/wit/classificationnodes/iterations?$depth=0&api-version=7.0',
                auth=auth, headers=headers
            )
            if not tree_resp.ok:
                logging.warning(f"[configure_team_iteration] Could not fetch iteration tree")
                return False
            root_guid = tree_resp.json().get('identifier')
            root_name = tree_resp.json().get('name')
            if not root_guid:
                logging.warning(f"[configure_team_iteration] No root iteration GUID found")
                return False
        except Exception as e:
            logging.warning(f"[configure_team_iteration] Error fetching iteration tree: {e}")
            return False

        # Step 2: get canonical team settings URL from _links (uses team GUID, avoids encoding issues)
        try:
            settings_get = _req.get(
                f'{self.organization_url}/{self.project}/{team_name}'
                f'/_apis/work/teamsettings?api-version=7.0',
                auth=auth, headers=headers
            )
            if not settings_get.ok:
                logging.warning(f"[configure_team_iteration] Could not GET team settings for '{team_name}'")
                return False
            links = settings_get.json().get('_links', {})
            settings_url = links['self']['href'] + '?api-version=7.0'
        except Exception as e:
            logging.warning(f"[configure_team_iteration] Could not get team settings links: {e}")
            return False

        # Step 3: PATCH backlogIteration to project root GUID (plain string, not object)
        try:
            resp = _req.patch(settings_url, auth=auth, headers=headers,
                              data=_json.dumps({'backlogIteration': root_guid}))
            if resp.ok:
                actual_name = resp.json().get('backlogIteration', {}).get('name')
                logging.info(
                    f"[configure_team_iteration] Team '{team_name}' backlogIteration → '{actual_name}'"
                )
                return True
            logging.warning(
                f"[configure_team_iteration] Could not set iteration for '{team_name}': "
                f"{resp.status_code} {resp.text[:200]}"
            )
            return False
        except Exception as e:
            logging.warning(f"[configure_team_iteration] Error for team '{team_name}': {e}")
            return False

    def get_work_item_type_states(self, type_name: str) -> list[dict]:
        """Return (and cache) the valid System.State values for a work item type in
        self.project, e.g. [{'name': 'To Do', 'category': 'Proposed'}, ...].

        Different processes/types have completely different state sets (Task in the
        Agile process has 'To Do'/'In Progress'/'Done' — no 'Active' at all — while
        User Story has 'New'/'Active'/'Resolved'/'Closed'), so state values can never
        be hardcoded across types.
        """
        cache = getattr(self, '_wit_states_cache', None)
        if cache is None:
            cache = {}
            self._wit_states_cache = cache
        if type_name in cache:
            return cache[type_name]
        url = (
            f'{self.organization_url}/{quote(self.project)}'
            f'/_apis/wit/workitemtypes/{quote(type_name)}/states?api-version=7.0'
        )
        try:
            resp = self.ado_api_call('GET', url)
            states = (resp or {}).get('value', [])
        except Exception as e:
            logging.warning(f"[get_work_item_type_states] Could not fetch states for '{type_name}': {e}")
            states = []
        cache[type_name] = states
        return states

    def resolve_valid_state(self, type_name: str, desired_state: str) -> str:
        """Map *desired_state* onto a state that's actually valid for *type_name*.

        Falls back by state category (Proposed/InProgress/Resolved/Completed) when
        the exact name doesn't exist on this type, instead of assuming a fixed name
        like 'Active' is universally valid (it often isn't — e.g. Task states).
        """
        states = self.get_work_item_type_states(type_name)
        if not states:
            return desired_state  # can't validate — pass through unchanged
        names = [s.get('name', '') for s in states if s.get('name')]
        desired_lower = (desired_state or '').strip().lower()
        for n in names:
            if n.lower() == desired_lower:
                return n

        def first_in_category(cat: str):
            for s in states:
                if (s.get('category') or '').lower() == cat.lower():
                    return s.get('name')
            return None

        if desired_lower in ('new', 'to do', 'open', 'backlog', 'proposed'):
            return first_in_category('Proposed') or (names[0] if names else desired_state)
        if desired_lower in ('active', 'in progress', 'doing', 'committed'):
            return first_in_category('InProgress') or (names[0] if names else desired_state)
        if desired_lower in ('done', 'closed', 'resolved', 'completed'):
            return first_in_category('Completed') or first_in_category('Resolved') or (names[-1] if names else desired_state)
        return names[0] if names else desired_state

    def update_item_core_fields(self, ado_id: int, title: str, state: str, assignee, description: str,
                                ado_type: str = None):
        """Update the core editable fields of an existing work item in one PATCH call.

        Pass ado_type (the work item's CURRENT type) so *state* can be validated/
        remapped against that type's actual state set before sending — prevents the
        "field 'State' ... not in the list of supported values" 400 that otherwise
        poisons the item (every subsequent PATCH to it fails full-document validation
        until the invalid state is corrected).
        """
        if ado_type:
            state = self.resolve_valid_state(ado_type, state)
        url = (
            f'{self.organization_url}/{self.project}/_apis/wit/workitems/{ado_id}'
            f'?api-version={self.api_version}'
        )
        payload = [
            {'op': 'add', 'path': '/fields/System.Title', 'value': title},
            {'op': 'add', 'path': '/fields/System.State', 'value': state},
        ]
        # Omit description entirely when None (ADO rejects null values)
        if description is not None:
            payload += [
                {'op': 'add', 'path': '/fields/System.Description', 'value': description or ''},
                {'op': 'add', 'path': '/multilineFieldsFormat/System.Description', 'value': 'Markdown'},
            ]
        logging.debug(f"[update_item_core_fields] Updating core fields on {ado_id}")
        try:
            return self.ado_api_call('PATCH', url, payload)
        except RuntimeError as e:
            if 'State' in str(e) and 'not in the list of supported values' in str(e):
                fallback_state = self.resolve_valid_state(ado_type, 'Active') if ado_type else 'Active'
                logging.warning(f"[update_item_core_fields] State '{state}' not valid for this work item type — falling back to '{fallback_state}'")
                for op in payload:
                    if op.get('path') == '/fields/System.State':
                        op['value'] = fallback_state
                return self.ado_api_call('PATCH', url, payload)
            raise

    def add_work_item_link(self, item_id: int, target_ado_id: int, relation_type: str):
        """Create a real ADO work item relationship (parent/child, blocks, related, etc.).

        relation_type examples:
          'System.LinkTypes.Hierarchy-Reverse'    # parent of this item
          'System.LinkTypes.Hierarchy-Forward'    # child of this item
          'System.LinkTypes.Dependency-Forward'   # this item is predecessor (blocks target)
          'System.LinkTypes.Dependency-Reverse'   # this item is successor (blocked by target)
          'System.LinkTypes.Related'              # generic related
          'System.LinkTypes.Duplicate-Forward'    # this item duplicates target
          'System.LinkTypes.Duplicate-Reverse'    # this item is duplicated by target
        """
        url = (
            f'{self.organization_url}/{self.project}/_apis/wit/workitems/{item_id}'
            f'?api-version={self.api_version}'
        )
        target_url = f'{self.organization_url}/_apis/wit/workitems/{target_ado_id}'
        payload = [
            {
                'op': 'add',
                'path': '/relations/-',
                'value': {
                    'rel': relation_type,
                    'url': target_url,
                    'attributes': {'comment': 'Migrated from Jira'}
                }
            }
        ]
        logging.debug(f"[add_work_item_link] Linking {item_id} --[{relation_type}]--> {target_ado_id}")
        return self.ado_api_call('PATCH', url, payload)

    def ensure_jira_key_tag(self, ado_id: int, jira_key: str):
        """Add or update the jiraKey tag on an ADO work item.
        
        Ensures that the tag 'jiraKey=<jira_key>' exists on the item.
        Preserves existing tags and merges the jiraKey tag.
        """
        try:
            item = self.get_work_item_full(ado_id)
            if not item:
                logging.warning(f"[ensure_jira_key_tag] Could not fetch item {ado_id} to add jiraKey tag")
                return False
            
            # Get existing tags
            existing_tags_str = item.get('fields', {}).get('System.Tags', '') or ''
            tag_parts = [t.strip() for t in existing_tags_str.split(';') if t.strip()]
            
            # Check if jiraKey tag already exists
            jira_key_tag = f'jiraKey={jira_key}'
            if jira_key_tag in tag_parts:
                logging.debug(f"[ensure_jira_key_tag] jiraKey tag already exists on {ado_id}: {jira_key_tag}")
                return True
            
            # Add the jiraKey tag
            tag_parts.append(jira_key_tag)
            updated_tags = '; '.join(tag_parts)
            
            # Update the item with the new tags
            self.update_field(ado_id, '/fields/System.Tags', updated_tags)
            logging.info(f"[ensure_jira_key_tag] Added jiraKey tag to {ado_id}: {jira_key_tag}")
            return True
        except Exception as e:
            logging.error(f"[ensure_jira_key_tag] Error adding jiraKey tag to {ado_id}: {e}")
            return False

    # -----------------------------------------------------------------------
    # Dynamic custom-field discovery / creation
    # -----------------------------------------------------------------------
    # Lets the migration map ANY Jira custom field (RICE scores, Fix Version,
    # etc.) onto an ADO field without a static config file: look for an
    # existing ADO field with a matching name first; if none exists, try to
    # create one (org-level field + attach to the work item type). If field
    # creation isn't possible (e.g. the project uses a non-Inherited process,
    # or the PAT lacks permission), the caller falls back to the description.

    def get_all_fields(self) -> list[dict]:
        """Return (and cache) every field defined in the organization."""
        if getattr(self, '_all_fields_cache', None) is not None:
            return self._all_fields_cache
        url = f'{self.organization_url}/_apis/wit/fields?api-version=7.0'
        try:
            resp = self.ado_api_call('GET', url)
            self._all_fields_cache = (resp or {}).get('value', [])
        except Exception as e:
            logging.warning(f"[get_all_fields] Could not list organization fields: {e}")
            self._all_fields_cache = []
        return self._all_fields_cache

    _FIELD_NAME_STOPWORDS = {'estimate', 'field', 'the', 'of', 'a', 'an', 'value'}
    
    # Known Jira → ADO field mappings for common fields that have different names
    _KNOWN_FIELD_MAPPINGS = {
        'story point estimate': 'Story Points',
        'story points': 'Story Points',
        'fix version': 'Fix Versions',
        'fix versions': 'Fix Versions',
        'affects version': 'Affected Versions',
        'affects versions': 'Affected Versions',
        'label': 'System.Tags',
        'labels': 'System.Tags',
    }

    @classmethod
    def _field_name_tokens(cls, name: str) -> set:
        """Normalize a display name into a comparable token set: lowercase, strip
        punctuation, drop filler words, crude de-pluralize. Used for a last-resort
        fuzzy match (e.g. Jira 'Story point estimate' vs ADO 'Story Points').
        """
        words = re.findall(r'[a-z0-9]+', (name or '').lower())
        tokens = set()
        for w in words:
            if w in cls._FIELD_NAME_STOPWORDS:
                continue
            tokens.add(w[:-1] if len(w) > 3 and w.endswith('s') else w)
        return tokens

    def find_field_by_name(self, display_name: str, exclude_ref_names: set | None = None) -> str | None:
        """Match an existing ADO field by display name. Returns referenceName or None.

        *exclude_ref_names* lets callers keep this generic matcher from ever returning
        a field the migration already manages via dedicated logic elsewhere (e.g.
        Custom.ActualStartDate, which is populated from Jira's system 'created' date,
        NOT from an unrelated Jira custom field that merely has a similar name).
        """
        if not display_name:
            return None
        exclude_ref_names = exclude_ref_names or set()
        target = display_name.strip().lower()
        
        # Step 1: Check if there's a known mapping for this Jira field name
        if target in self._KNOWN_FIELD_MAPPINGS:
            expected_ado_name = self._KNOWN_FIELD_MAPPINGS[target]
            fields = self.get_all_fields()
            for f in fields:
                if (f.get('name') or '').strip().lower() == expected_ado_name.lower():
                    ref = f.get('referenceName')
                    if ref not in exclude_ref_names:
                        import logging as _logging
                        _logging.debug(f"[find_field_by_name] Matched '{display_name}' → '{expected_ado_name}' ({ref}) via known mapping")
                        return ref
        
        fields = [f for f in self.get_all_fields() if f.get('referenceName') not in exclude_ref_names]
        
        # Step 2: Exact match
        for f in fields:
            if (f.get('name') or '').strip().lower() == target:
                import logging as _logging
                _logging.debug(f"[find_field_by_name] Matched '{display_name}' → '{f.get('name')}' ({f.get('referenceName')}) via exact match")
                return f.get('referenceName')
        
        # Step 3: Loose contains-match (handles "Fix Version" vs "Fix Version/s", etc.)
        for f in fields:
            fname = (f.get('name') or '').strip().lower()
            if fname and (target in fname or fname in target):
                import logging as _logging
                _logging.debug(f"[find_field_by_name] Matched '{display_name}' → '{f.get('name')}' ({f.get('referenceName')}) via contains-match")
                return f.get('referenceName')
        
        # Step 4: Token-overlap fuzzy match (handles "Story point estimate" vs "Story Points")
        target_tokens = self._field_name_tokens(display_name)
        if target_tokens:
            best_match = None
            best_overlap = 0
            best_field_name = None
            for f in fields:
                fname = f.get('name') or ''
                f_tokens = self._field_name_tokens(fname)
                if not f_tokens:
                    continue
                overlap = len(target_tokens & f_tokens) / min(len(target_tokens), len(f_tokens))
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_match = f.get('referenceName')
                    best_field_name = fname
                if best_overlap >= 0.75:
                    import logging as _logging
                    _logging.debug(f"[find_field_by_name] Matched '{display_name}' → '{best_field_name}' ({best_match}) via token-fuzzy (overlap={best_overlap:.2f})")
                    return best_match
            if best_overlap >= 0.75:
                import logging as _logging
                _logging.debug(f"[find_field_by_name] Matched '{display_name}' → '{best_field_name}' ({best_match}) via token-fuzzy (overlap={best_overlap:.2f})")
                return best_match
        
        import logging as _logging
        _logging.debug(f"[find_field_by_name] No match found for '{display_name}' in {len(fields)} searchable ADO fields")
        return None

    def get_process_id_for_project(self) -> str | None:
        """Return the process template GUID backing self.project, or None if it can't be determined."""
        if getattr(self, '_process_id_cache', None) is not None:
            return self._process_id_cache or None

        def _fetch_capabilities(project_ref: str):
            # NOTE: the correct Core API path is "_apis/projects/{project}" — there is
            # no "/core/" segment. That typo used to 404 silently here, which made
            # dynamic custom-field creation appear "broken" for every project.
            url = (
                f'{self.organization_url}/_apis/projects/{quote(project_ref)}'
                f'?includeCapabilities=true&api-version=7.0'
            )
            resp = self.ado_api_call('GET', url)
            return (
                (resp or {}).get('capabilities', {})
                .get('processTemplate', {})
                .get('templateTypeId')
            )

        try:
            process_id = _fetch_capabilities(self.project)
        except Exception as e:
            logging.debug(f"[get_process_id_for_project] Lookup by name failed for '{self.project}' ({e}) — retrying by project ID")
            process_id = None
            # Some orgs 404 on core/projects/{name} for names with spaces/special
            # characters — fall back to listing all projects and matching by name,
            # then re-query capabilities using the project's GUID.
            try:
                list_url = f'{self.organization_url}/_apis/projects?api-version=7.0&$top=1000'
                resp = self.ado_api_call('GET', list_url)
                for p in (resp or {}).get('value', []):
                    if (p.get('name') or '').strip().lower() == self.project.strip().lower():
                        process_id = _fetch_capabilities(p['id'])
                        break
            except Exception as e2:
                logging.warning(f"[get_process_id_for_project] Could not resolve process for '{self.project}': {e2}")

        self._process_id_cache = process_id or ''
        return process_id

    def get_work_item_type_ref_name(self, type_name: str) -> str | None:
        """Return the process referenceName for a work item type (e.g. 'Microsoft.VSTS.WorkItemTypes.UserStory')."""
        cache = getattr(self, '_wit_ref_cache', None)
        if cache is None:
            cache = {}
            self._wit_ref_cache = cache
        if type_name in cache:
            return cache[type_name]
        url = (
            f'{self.organization_url}/{quote(self.project)}'
            f'/_apis/wit/workitemtypes/{quote(type_name)}?api-version=7.0'
        )
        try:
            resp = self.ado_api_call('GET', url)
            ref_name = (resp or {}).get('referenceName')
            cache[type_name] = ref_name
            return ref_name
        except Exception as e:
            logging.debug(f"[get_work_item_type_ref_name] Could not resolve type '{type_name}': {e}")
            cache[type_name] = None
            return None

    def create_org_field(self, display_name: str, field_type: str = 'string') -> str | None:
        """Create a new organization-level field. Returns its referenceName, or None on failure."""
        if getattr(self, '_field_creation_blocked', False):
            # Already confirmed the PAT/account lacks 'Edit process' permission this run —
            # don't retry (and don't spam the log) for every remaining field.
            return None
        ref_name = 'Custom.' + re.sub(r'[^A-Za-z0-9]', '', display_name.title())
        url = f'{self.organization_url}/_apis/wit/fields?api-version=7.0'
        payload = {
            'name': display_name,
            'referenceName': ref_name,
            'type': field_type,
            'description': 'Auto-created by Jira→ADO migration for a Jira custom field.',
            'usage': 'workItem',
        }
        try:
            resp = self.ado_api_call('POST', url, payload)
            created_ref = (resp or {}).get('referenceName', ref_name)
            logging.info(f"[create_org_field] ✅ Created ADO field '{display_name}' → {created_ref}")
            # Invalidate the field list cache so subsequent lookups see the new field
            self._all_fields_cache = None
            return created_ref
        except Exception as e:
            msg = str(e)
            if 'already exists' in msg.lower() or 'VS402903' in msg:
                logging.debug(f"[create_org_field] Field '{display_name}' already exists — reusing {ref_name}")
                return ref_name
            if 'VS402356' in msg or 'do not have the permissions' in msg.lower():
                self._field_creation_blocked = True
                logging.warning(
                    f"[create_org_field] ❌ Account/PAT lacks 'Edit process' permission — "
                    f"cannot auto-create ADO fields this run (first blocked field: '{display_name}'). "
                    f"Values will be preserved in each item's description instead."
                )
                return None
            logging.warning(f"[create_org_field] Could not create field '{display_name}': {e}")
            return None

    def field_creation_blocked(self) -> bool:
        """True if this run already hit a permission error trying to create a custom field."""
        return getattr(self, '_field_creation_blocked', False)

    def add_field_to_work_item_type(self, wit_type_name: str, field_ref_name: str) -> bool:
        """Attach an existing org-level field to a work item type via the Process API.

        Only works when the project's process is Inherited; returns False (and logs once)
        for out-of-the-box (System) processes, which don't support field customization here.
        """
        process_id = self.get_process_id_for_project()
        wit_ref    = self.get_work_item_type_ref_name(wit_type_name)
        if not process_id or not wit_ref:
            return False
        url = (
            f'{self.organization_url}/_apis/work/processes/{process_id}'
            f'/workItemTypes/{wit_ref}/fields?api-version=7.0'
        )
        payload = {'referenceName': field_ref_name, 'required': False}
        try:
            self.ado_api_call('POST', url, payload)
            logging.info(f"[add_field_to_work_item_type] ✅ Attached {field_ref_name} to '{wit_type_name}'")
            return True
        except Exception as e:
            msg = str(e)
            if 'already' in msg.lower():
                return True  # already attached — treat as success
            logging.warning(
                f"[add_field_to_work_item_type] Could not attach {field_ref_name} to '{wit_type_name}' "
                f"(process may not be Inherited): {e}"
            )
            return False

    def get_or_create_custom_field(self, display_name: str, wit_type_name: str,
                                   field_type: str = 'string',
                                   exclude_ref_names: set | None = None) -> str | None:
        """Resolve a Jira custom field name to a usable ADO field reference.

        1. Reuse an already-discovered/created field for this display_name (per-run cache).
        2. Look for an existing ADO field with a matching name (excluding any reference
           names the caller says are already spoken for by other, unrelated logic).
        3. Otherwise, try to create one and attach it to *wit_type_name*.
        Returns None if no field is usable — caller should fall back to description text.
        """
        cache = getattr(self, '_dynamic_field_cache', None)
        if cache is None:
            cache = {}
            self._dynamic_field_cache = cache
        cache_key = f'{display_name}::{wit_type_name}'
        if cache_key in cache:
            logging.debug(f"[get_or_create_custom_field] Using cached result for '{display_name}': {cache[cache_key]}")
            return cache[cache_key]

        ref_name = self.find_field_by_name(display_name, exclude_ref_names=exclude_ref_names)
        if ref_name:
            logging.info(f"[get_or_create_custom_field] ✅ Found existing ADO field for '{display_name}': {ref_name}")
        else:
            logging.debug(f"[get_or_create_custom_field] No existing field for '{display_name}' — attempting to create")
            ref_name = self.create_org_field(display_name, field_type)
            if ref_name:
                logging.info(f"[get_or_create_custom_field] ✅ Created new ADO field for '{display_name}': {ref_name}")
            else:
                logging.warning(f"[get_or_create_custom_field] ❌ Could not create field for '{display_name}'")
        
        # Belt-and-suspenders: even a freshly-created field's auto-generated reference
        # name could coincidentally collide with a reserved one (e.g. Jira field named
        # "Priority Level" -> "Custom.PriorityLevel"). Never hand back a reserved ref.
        if ref_name and exclude_ref_names and ref_name in exclude_ref_names:
            logging.warning(
                f"[get_or_create_custom_field] '{display_name}' resolved to reserved field "
                f"{ref_name} — refusing to reuse it; falling back to description."
            )
            ref_name = None
        if ref_name and not self.add_field_to_work_item_type(wit_type_name, ref_name):
            # Field exists at the org level but couldn't be attached to this work item
            # type — writing to it would 400. Treat as unusable for this type.
            logging.warning(f"[get_or_create_custom_field] Could not attach {ref_name} to '{wit_type_name}'")
            ref_name = None

        cache[cache_key] = ref_name
        logging.debug(f"[get_or_create_custom_field] Final result for '{display_name}': {ref_name}")
        return ref_name

    def get_work_item_revisions(self, ado_id: int) -> list:
        """Return all revisions (history) of a work item — read-only, used for diagnostics."""
        url = f'{self.organization_url}/{self.project}/_apis/wit/workitems/{ado_id}/revisions?api-version=7.0'
        try:
            resp = self.ado_api_call('GET', url)
            return (resp or {}).get('value', [])
        except Exception as e:
            logging.warning(f"[get_work_item_revisions] Could not fetch revisions for {ado_id}: {e}")
            return []



# Semantic fallback chains: for each Jira type, preferred ADO equivalents in order.
_TYPE_FALLBACKS: dict[str, list[str]] = {
    'story':           ['User Story', 'Product Backlog Item', 'Requirement', 'Feature', 'Task'],
    'improvement':     ['User Story', 'Product Backlog Item', 'Requirement', 'Feature', 'Task'],
    'epic':            ['Epic', 'Feature', 'User Story', 'Product Backlog Item'],
    'feature request': ['Feature', 'User Story', 'Product Backlog Item', 'Request', 'Task'],
    'bug':             ['Bug', 'Defect', 'Issue', 'Task'],
    'task':            ['Task', 'User Story', 'Product Backlog Item', 'Issue'],
    'sub-task':        ['Task', 'Child Task', 'User Story'],
    'subtask':         ['Task', 'Child Task', 'User Story'],
    'new feature':     ['Feature', 'User Story', 'Product Backlog Item', 'Task'],
    'support request': ['Issue', 'Task', 'User Story', 'Impediment'],
    'triage':          ['Issue', 'Task', 'Impediment', 'User Story'],
    'incident':        ['Issue', 'Bug', 'Task', 'Impediment'],
    '__default__':     ['User Story', 'Product Backlog Item', 'Task', 'Issue', 'Feature'],
}


def closest_ado_type(jira_type: str, wanted_ado_type: str, available: list[str]) -> str:
    """Return the best available ADO type for a Jira type given project-available types."""
    available_lower = {t.lower(): t for t in available}
    if wanted_ado_type.lower() in available_lower:
        return available_lower[wanted_ado_type.lower()]
    chain = _TYPE_FALLBACKS.get(jira_type.lower(), _TYPE_FALLBACKS['__default__'])
    for candidate in chain:
        if candidate.lower() in available_lower:
            return available_lower[candidate.lower()]
    return available[0]


def check_type_config_compatibility(type_config: dict, available_types: list[str]) -> list[dict]:
    """Compare type_config mappings against available ADO types.

    Returns a list of remapping warnings: [{'jira_type', 'configured', 'will_use'}]
    so the caller (preflight endpoint) can surface them to the user before migration.
    """
    available_lower = {t.lower(): t for t in available_types}
    warnings = []
    for jira_type, ado_type in type_config.items():
        if jira_type == 'Jira Type':
            continue
        if ado_type.lower() not in available_lower:
            best = closest_ado_type(jira_type, ado_type, available_types)
            warnings.append({'jira_type': jira_type, 'configured': ado_type, 'will_use': best})
    return warnings
