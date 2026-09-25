import requests

from params.settings import API_URL

class AIRequestHandler:
    def __init__(
            self, system_prompt=None, content=None
        ):
        self.api_url = API_URL
        self.system_prompt = system_prompt
        self.content = content

    def send_request(self, system_prompt, content):
        try:
            response = requests.post(
                self.api_url,
                json= {
                    "system_prompt": system_prompt,
                    "content": content
                }
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            return {"error": str(e)}