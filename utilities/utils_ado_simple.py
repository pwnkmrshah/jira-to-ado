import json
import base64
import urllib.request
from dataclasses import dataclass
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

@dataclass
class AzureDevOpsConfig:
    """Configuration for Azure DevOps connection"""
    organization: str
    credentials: str
    project: str = None
    access_token: str = None
    username: str = None

class AzureDevOpsClient:
    """Client for interacting with Azure DevOps"""
    
    def __init__(self, config: AzureDevOpsConfig, project):
        # Initialize the Azure DevOps client with configuration
        self.username = config.username
        self.access_token = config.access_token
        self.organization_url = f"https://dev.azure.com/{config.organization}"
        self.api_version = '7.0-preview.3'
        self.project = project
        self.credentials = base64.b64encode(f":{config.credentials}".encode()).decode()

    def ado_api_call(self, method, url, payload=None):
        # Makes a call to the Azure DevOps API with error handling

        credentials = self.credentials
        response = None  # Initialize response to None.

        try:
            data = json.dumps(payload)
        except TypeError:
            data = payload
        
        headers = {
            "Authorization": f"Basic {credentials}"
        }

        try:
            match method:
                case 'GET':
                    headers.update({'Content-Type': 'application/json'})
                case 'POST':
                    headers.update({'Content-Type': 'application/json'})
                case 'POSTP':           # If method is POSTP, then the content-type must be json-patch or the REST API fails
                    method = 'POST'
                    headers.update({'Content-Type': 'application/json-patch+json'})
                case 'PATCH':
                    headers.update({'Content-Type': 'application/json-patch+json'})
                case _:
                    raise ValueError("Method must be either 'GET', 'POST', or 'PATCH'")
            logging.debug(f"[ado_api_call] Making {method} request to {url}")
            req = urllib.request.Request(url, method=method, headers=headers, data=data.encode() if payload else None)
            response = urllib.request.urlopen(req)
            
        except Exception as e:
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
            return json.loads(response.read().decode())
        except ValueError:
            logging.error("[ado_api_call] Error: ", response)
            return response

    def get_item_info(self, item_id, revision_id=None):
        # Retrieves all information about a specific work item
        
        if revision_id:
            revision_id -= 1  # If revision is populated, get previous revision
            url = f'{self.organization_url}/_apis/wit/workitems/{item_id}/revisions/{revision_id}&api-version={self.api_version}'
        else:
            url = f'{self.organization_url}/_apis/wit/workitems?ids={item_id}&api-version={self.api_version}'
        response = self.ado_api_call('GET', url)

        if response is None:
            return None

        # Normalize the response from the API
        fields = response['value']
        norm_df = json.dumps(fields)

        logging.debug(f"[get_item_info] Work item info for: {item_id}")
        return norm_df

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
