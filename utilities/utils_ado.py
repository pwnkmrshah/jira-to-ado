import os
import json
import base64
import requests
from requests import RequestException
from dataclasses import dataclass
import pandas
import re
from datetime import datetime
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

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
    #config_path = "C:\\Users\\matt.baker\\CascadeProjects\\ADO-Automation\\Config\\ado_config.json" #os.path.expanduser('~\ado_config.json')
    config_path = Path(__file__).parent.parent / "config" / "ado_config.json"

    # Try to load from config file
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config_data = json.load(f)

                return AzureDevOpsConfig(**config_data)

        except Exception as e:
            logging.warning(f"[load_ado_config] Warning: Could not read config file: {e}")

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

    def ado_api_call(self, method, url, payload=None):
        # Makes a call to the Azure DevOps API with error handling

        auth = (self.username, self.access_token)
        #credentials = self.credentials
        response = None  # Initialize response to None.

        try:
            data = json.dumps(payload)
        except TypeError:
            data = payload

        try:
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
            logging.debug(f"[ado_api_call] Making {method} request to {url}")
            response = requests.request(method, auth=auth, url=url, headers=headers, data=data if payload else None)

            # Raise an exception for HTTP error responses.
            response.raise_for_status()
            
        except RequestException as e:
            # Retry logic for handling 503 status code and other request exceptions.
            logging.error(f"[ado_api_call] Error: {response.status_code if response is not None else 'No response'} for url: {url}")
            logging.error(f"[ado_api_call] Error response: {response.text if response is not None else 'No response'}")
            if response is not None and response.status_code == 400 and 'VS402337' in response.text:
                logging.error("[ado_api_call] Work item size limit exceeded")
                raise WorkItemsSizeLimitExceeded()
            else:
                logging.error(f"[ado_api_call] Error: {e}")
                raise e

        try:
            return response.json()
        except ValueError:
            logging.error(f"[ado_api_call] ValueError parsing response: {response}")
            return response

    def format_date(self, date_string):
        # Format a string from Jira into a date
        date_string = datetime.strptime(date_string, '%Y-%m-%dT%H:%M:%S.%f%z')  # Convert string to datetime
        date_string = date_string.strftime('%Y-%m-%d %H:%M')                    # Re-format datetime
        date_string = ''.join((date_string, ' UTC-6'))

        logging.debug(f"[format_date] Formatted date: {date_string}")
        return date_string

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
        payload = [
            {
                'op': 'add',
                'path': '/fields/System.State',
                'value': state
            }
        ]
        logging.debug(f"[create_item] Setting state to: {state}")
        response = self.ado_api_call('PATCH', url, payload)

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
        url = f'{self.organization_url}/{self.project}/_apis/wit/attachments?fileName={filename}&api-version={self.api_version}'

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
                'op': 'add',
                'path': field,
                'value': text
            }
        ]
        response = self.ado_api_call('PATCH', url, payload)
        logging.debug(f"[update_field] Updated field: {field}")

        return response

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
        try:
            response = self.ado_api_call('POST', url, query)
            if response and response.get('workItems'):
                return response['workItems'][0]['id']
        except Exception as e:
            logging.warning(f"[find_work_item_by_jira_key] Search failed for {jira_key}: {e}")
        return None

    def bulk_fetch_jira_key_mapping(self, jira_keys: list) -> dict:
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
        batch_size = 50

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
                            if key in candidates and item['id'] not in candidates[key]:
                                candidates[key].append(item['id'])
                            break
            except Exception as e:
                logging.warning(f"[bulk_fetch_jira_key_mapping] Tag batch {i//batch_size+1} failed: {e}")

        # ---- Pass 2: title search (catches untagged originals) ----
        for i in range(0, len(jira_keys), batch_size):
            batch = jira_keys[i:i + batch_size]
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
                    for token in _re.findall(r'\[([A-Z]+-\d+)\]', title):
                        if token in candidates and item['id'] not in candidates[token]:
                            candidates[token].append(item['id'])
                        break
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
        """Return a list of existing comment text strings for deduplication."""
        url = (
            f'{self.organization_url}/{self.project}/_apis/wit/workItems/{ado_id}'
            f'/comments?api-version={self.api_version}'
        )
        try:
            response = self.ado_api_call('GET', url)
            if response and 'comments' in response:
                return [c.get('text', '') for c in response['comments']]
        except Exception as e:
            logging.warning(f"[get_work_item_comment_texts] Could not fetch comments for {ado_id}: {e}")
        return []

    def get_work_item_relation_urls(self, ado_id: int) -> list:
        """Return a list of existing relation URL strings for deduplication."""
        item = self.get_work_item_full(ado_id)
        if item and 'relations' in item:
            return [r.get('url', '') for r in item.get('relations', [])]
        return []

    def ensure_jira_key_tag(self, ado_id: int, jira_key: str) -> None:
        """Add 'JiraKey=<jira_key>' tag to the work item if not already present.

        Called last so it is never overwritten by other field updates.
        """
        item = self.get_work_item_full(ado_id)
        existing_tags = ''
        if item:
            existing_tags = (item.get('fields', {}).get('System.Tags') or '').strip()
        tag = f'JiraKey={jira_key}'
        if tag not in existing_tags:
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

    def update_item_core_fields(self, ado_id: int, title: str, state: str, assignee, description: str):
        """Update the core editable fields of an existing work item in one PATCH call."""
        url = (
            f'{self.organization_url}/{self.project}/_apis/wit/workitems/{ado_id}'
            f'?api-version={self.api_version}'
        )
        payload = [
            {'op': 'add', 'path': '/fields/System.Title',       'value': title},
            {'op': 'add', 'path': '/fields/System.Description', 'value': description},
            {'op': 'add', 'path': '/fields/System.State',       'value': state},
        ]
        logging.debug(f"[update_item_core_fields] Updating core fields on {ado_id}")
        return self.ado_api_call('PATCH', url, payload)

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

