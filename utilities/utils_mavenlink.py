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
class MavenlinkConfig:
    """Configuration for Mavenlink connection"""
    server: str
    #email: str
    access_token: str

def load_mavenlink_config() -> MavenlinkConfig:
    """Load configuration from environment variables or user input"""
    #config_path = "C:\\Users\\matt.baker\\CascadeProjects\\ADO-Automation\\Config\\jira_config.json" #os.path.expanduser('~\ado_config.json')
    config_path = Path(__file__).parent.parent / "config" / "mavenlink_config.json"

    # Try to load from config file
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config_data = json.load(f)

                return MavenlinkConfig(**config_data)
        except Exception as e:
            logging.error(f"[load_jira_config] Could not read config file: {e}")

class MavenlinkClient:
    def __init__(self, config: MavenlinkConfig):
        """Initialize the Mavenlink client with configuration"""
        self.server = config.server.rstrip('/')  # Remove trailing slash if present
        self.access_token = config.access_token

    def mavenlink_api_call(self, method, url, payload=None):
        #headers = {'Accept': 'application/json'}

        #logger.info(f'URL Sent = {url}')
        #logger.info(f'Payload Sent = {payload}')
        #logger.info('')

        headers = {
            'Authorization': f'Bearer {self.access_token}'
        }

        try:
            # Make GET or POST request based on the specified method.
            if method == 'GET':
                headers.update({'Content-Type': 'application/json'})
                response = requests.request('GET', url, headers=headers, params=payload)
            elif method == 'PUT':
                headers.update({'Content-Type': 'application/json'})
                response = requests.request('PUT', url, headers=headers, data=json.dumps(payload) if payload else None)
            elif method == 'POST':
                headers.update({'Content-Type': 'application/json-patch+json'})
                response = requests.request('POST', url, headers=headers, data=json.dumps(payload) if payload else None)
            else:
                raise ValueError("Method must be either 'GET' or 'POST'")

            # Raise an exception for HTTP error responses.
            response.raise_for_status()
        
        except RequestException as e:   
            logger.error(f"Failed to connect to Mavenlink: {str(e)}")

        try:
            return response.json()
        except ValueError:
            return response

    def get_mavenlink_project(self, project_id):
        # Get a single Mavenlink project by its ID.

        url = f'{self.server}/api/v1/workspaces/{project_id}'

        try:
            response = self.mavenlink_api_call('GET', url)
            #logger.info(f"Successfully retrieved project: {project_id}")

            return response

        except Exception as e:
            logger.error(f"Unexpected error while retrieving project {project_id}: {str(e)}")

    def update_project(self, project_id, field, value):
        # Update a Mavenlink project by its ID.
        url = f'{self.server}/api/v1/workspaces/{project_id}'

        payload = {
            'workspace': {
                field: value
            }
        }

        try:
            response = self.mavenlink_api_call('PUT', url, payload)
            logger.debug(f"Successfully updated project: {project_id}")

            return response

        except Exception as e:
            logger.error(f"Unexpected error while updating project {project_id}: {str(e)}")

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
