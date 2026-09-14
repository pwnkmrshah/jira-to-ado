import os
import json
import requests
from requests.exceptions import RequestException
from dataclasses import dataclass
import logging
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

@dataclass
class JiraConfig:
    """Configuration for Jira connection"""
    server: str
    email: str
    access_token: str

def load_jira_config(jira_instance: str = None) -> JiraConfig:
    """Load configuration from environment variables or user input"""
    # Check env var overrides first — set by Flask when forwarding Forge KVS credentials
    env_url   = os.environ.get('JIRA_URL', '').strip()
    env_email = os.environ.get('JIRA_EMAIL', '').strip()
    env_token = os.environ.get('JIRA_TOKEN', '').strip()
    if env_url and env_email and env_token:
        logging.info(f"[load_jira_config] Using JIRA_URL env override: {env_url}")
        return JiraConfig(server=env_url, email=env_email, access_token=env_token)

    #config_path = "C:\\Users\\matt.baker\\CascadeProjects\\ADO-Automation\\Config\\jira_config.json" #os.path.expanduser('~\ado_config.json')
    config_path = Path(__file__).parent.parent / "config" / "jira_config.json"

    # Try to load from config file
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config_data = json.load(f)

                if not jira_instance:
                    return JiraConfig(**config_data)
                
                url = f'https://{jira_instance}.atlassian.net'
                return JiraConfig(server=url, email=config_data['email'], access_token=config_data['access_token'])
        except Exception as e:
            logging.error(f"[load_jira_config] Could not read config file: {e}")

class JiraClient:
    def __init__(self, config: JiraConfig):
        """Initialize the JIRA client with configuration"""
        self.server = config.server.rstrip('/')  # Remove trailing slash if present
        self.email = config.email
        self.access_token = config.access_token

    def jira_api_call(self, method, url, payload=None):
        auth = (self.email, self.access_token)
        headers = {'Accept': 'application/json'}

        #logger.info(f'URL Sent = {url}')
        #logger.info(f'Payload Sent = {payload}')
        #logger.info('')

        try:
            # Make GET or POST request based on the specified method.
            if method == 'GET':
                response = requests.request('GET', url, auth=auth, headers=headers, params=payload)
            elif method == 'POST':
                response = requests.request('POST', url, auth=auth, headers=headers, data=json.dumps(payload) if payload else None)
            else:
                raise ValueError("Method must be either 'GET' or 'POST'")

            # Raise an exception for HTTP error responses.
            response.raise_for_status()
        
        except RequestException as e:   
            logger.error(f"Failed to connect to JIRA: {str(e)}")
            return None

        try:
            return response.json()
        except ValueError:
            return response

    def get_jira_issue(self, issue_key):
        # Get a single JIRA issue by its key.

        url = f'{self.server}/rest/api/3/issue/{issue_key}?expand=renderedFields'

        try:
            response = self.jira_api_call('GET', url)
            #logger.info(f"Successfully retrieved issue: {issue_key}")

            return response

        except Exception as e:
            logger.error(f"Unexpected error while retrieving issue {issue_key}: {str(e)}")
        
        return None

    def get_filter_items(self, filter_id):
        # Get list of tickets from filter results based on a filter ID
        url = f'{self.server}/rest/api/3/filter/{filter_id}'

        # Get the JQL for the filter
        try:
            response = self.jira_api_call('GET', url)
            
        except Exception as e:
            logger.error(f"Unexpected error while retrieving filter {filter_id}: {str(e)}")
            return None

        # Guard: jira_api_call returns None on HTTP errors (e.g. 404 filter not found)
        if response is None:
            logger.error(
                f"Filter {filter_id!r} not found or not accessible. "
                "Check the filter ID exists and is shared with this API token's account."
            )
            return None

        # Get ALL filter results via cursor-based pagination — no 1000-item cap.
        jql = response['jql']
        return self.search_jql_paginated(jql)

    def search_issues(self, jql):
        """Execute a JQL search and return matching issues.
        
        Args:
            jql: JQL query string (e.g., 'project = DATA')
        
        Returns:
            Response dict with 'issues' key containing list of issues, each with 'key' field
        """
        url = f'{self.server}/rest/api/3/search'
        payload = {
            'jql': jql,
            'startAt': 0,
            'maxResults': 1000,
            'fields': 'key'
        }

        try:
            response = self.jira_api_call('GET', url, payload)
            if response:
                logger.info(f"[search_issues] JQL '{jql}' returned {len(response.get('issues', []))} issues")
            return response

        except Exception as e:
            logger.error(f"Unexpected error while executing JQL '{jql}': {str(e)}")
            return None

    def search_jql_paginated(self, jql: str) -> dict | None:
        """Fetch ALL issues matching a JQL via POST /rest/api/3/search/jql with nextPageToken pagination.

        Returns same shape as get_filter_items: {'issues': [{'id': ..., 'key': ...}, ...]}
        Scalable to any number of issues — no ARG_MAX or maxResults cap.
        """
        url = f'{self.server}/rest/api/3/search/jql'
        all_issues: list = []
        page_token: str | None = None

        while True:
            body: dict = {'jql': jql, 'maxResults': 200, 'fields': ['key']}
            if page_token:
                body['nextPageToken'] = page_token
            try:
                response = requests.post(
                    url,
                    auth=(self.email, self.access_token),
                    headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
                    json=body,
                    timeout=30,
                )
                response.raise_for_status()
            except Exception as exc:
                logger.error(f'[search_jql_paginated] POST failed: {exc}')
                return None

            data = response.json()
            page_issues = data.get('issues', [])
            all_issues.extend(page_issues)
            page_token = data.get('nextPageToken')
            if not page_token:
                break

        logger.info(f'[search_jql_paginated] JQL returned {len(all_issues)} issues total: {jql!r}')
        return {'issues': all_issues, 'total': len(all_issues)}

    def get_users(self):
        # Get list of Jira users
        url = f'{self.server}/rest/api/2/user/search?query&maxResults=1000'

        try:
            response = self.jira_api_call('GET', url)
            return response
            
        except Exception as e:
            logger.error(f"Unexpected error while retrieving user list: {str(e)}")

    def get_attachment(self, url):
        # Get attachment info by ID
        #url = f'{self.server}/rest/api/3/attachment/content/{attachment_id}'

        try:
            response = self.jira_api_call('GET', url)
            return response
            
        except Exception as e:
            logger.error(f"Unexpected error while retrieving attachments: {str(e)}")

    def get_pull_request(self, issue_key):
        # Get pull request info for an issue (the URL referenced below is stated, by Jira, to be internal use only and not documented or supported)
        url = f'{self.server}/rest/dev-status/latest/issue/details?issueId={issue_key}&applicationType=GitHub&dataType=branch'

        try:
            response = self.jira_api_call('GET', url)
            return response
            
        except Exception as e:
            logger.error(f"Unexpected error while retrieving pull requests: {str(e)}")

    def get_comments(self, issue_key):
        # Get comments and convert them from ADF to HTML
        url =  f'{self.server}/rest/api/3/issue/{issue_key}/comment?expand=renderedBody'

        try:
            response = self.jira_api_call('GET', url)
            return response
            
        except Exception as e:
            logger.error(f"Unexpected error while retrieving html comments: {str(e)}")
