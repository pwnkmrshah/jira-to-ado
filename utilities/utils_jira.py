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

        # Get the filter results using the JQL
        jql = response['jql']
        url = f'{self.server}/rest/api/3/search/jql'
        payload = {
            'jql': jql,
            'startAt': 0,
            'maxResults': 1000,
            'fields':'id'
        }

        try:
            response = self.jira_api_call('GET', url, payload)
            return response

        except Exception as e:
            logger.error(f"Unexpected error while executing JQL {jql}: {str(e)}")

        return None

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

