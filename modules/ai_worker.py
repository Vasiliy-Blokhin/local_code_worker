import requests
from params.settings import API_URL, logger

class AIRequestHandler:
    def __init__(self, system_prompt=None, content=None):
        self.api_url = API_URL + 'v1/chat/completions'
        self.start_url = API_URL + 'api/models/qwen25-coder-14b-unc/start'
        self.system_prompt = system_prompt
        self.content = content

    def send_request(self):
        try:
            headers = {
                "X-API-Password": "12345",
                "Content-Type": "application/json"
            }
            payload = {
                'model': 'qwen25-coder-14b-unc',
                'messages': [
                    {'role': 'system', 'content': self.system_prompt},
                    {'role': 'user', 'content': self.content}
                ],
            }
            response = requests.post(
                self.api_url,
                headers=headers,
                json=payload,
                # timeout=30  # Reduced timeout value
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            return {"error": str(e)}

    def start_model(self):
        try:
            headers = {
                "X-API-Password": "12345"
            }
            response = requests.post(
                self.start_url,
                headers=headers,
                timeout=30  # Reduced timeout value
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            return {"error": str(e)}