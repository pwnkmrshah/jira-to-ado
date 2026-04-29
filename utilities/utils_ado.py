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
                logging.error("[ado_api_call] Error: ", e)
                raise e

        try:
            return response.json()
        except ValueError:
            logging.error("[ado_api_call] Error: ", response)
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
    
    def add_attachment(self, item_id, attachment_url, author, created_date):
        # Add an attachment to a work item
        url = f'{self.organization_url}/{self.project}/_apis/wit/workitems/{item_id}?api-version={self.api_version}'
        created_date = self.format_date(created_date)
        comment = f'Created by {author} on {created_date}'
        payload = [
            {
                'op': 'add',
                'path': '/relations/-',
                'value': {
                    'rel': 'AttachedFile',
                    'url': attachment_url,
                    'attributes': {
                        'comment': comment
                    }
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
        description = response.at[0, 'fields.System.Description']

        # Add the new text to the description
        add_text = ''.join((description, '<br />', add_text))
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

