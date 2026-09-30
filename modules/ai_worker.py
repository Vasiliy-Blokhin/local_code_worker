"""Модуль взаимодействия с локальным ИИ API (OpenAI-совместимым)."""

import requests

from params.settings import logger


class AIRequestError(Exception):
    """Ошибка при обращении к ИИ API (соединение, статус, пароль и т.п)."""


class AIRequestHandler:
    def __init__(self, api_url, password, model='qwen25-coder-14b-unc',
                 system_prompt=None, content=None):
        self.api_url = api_url.rstrip('/') + '/'
        self.chat_url = self.api_url + 'v1/chat/completions'
        self.start_url = self.api_url + f'api/models/{model}/start'
        self.password = password
        self.model = model
        self.system_prompt = system_prompt
        self.content = content

    def _headers(self):
        return {
            'X-API-Password': self.password,
            'Content-Type': 'application/json',
        }

    def send_request(self, timeout=None):
        """Отправить chat-completions запрос. При ошибке поднимает AIRequestError."""
        payload = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': self.system_prompt},
                {'role': 'user', 'content': self.content},
            ],
        }
        logger.debug(f'Отправка запроса к модели {self.model}')
        try:
            response = requests.post(
                self.chat_url,
                headers=self._headers(),
                json=payload,
                timeout=timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise AIRequestError(f'Не удалось соединиться с ИИ API ({self.chat_url}): {exc}') from exc

        if response.status_code != 200:
            raise AIRequestError(
                f'ИИ API вернул ошибку {response.status_code}: {response.text[:300]}'
            )
        return response.json()

    def start_model(self, timeout=60):
        """Запустить/прогреть модель на сервере."""
        logger.debug(f'Запуск модели {self.model}')
        try:
            response = requests.post(
                self.start_url,
                headers=self._headers(),
                timeout=timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise AIRequestError(f'Не удалось запустить модель: {exc}') from exc

        if response.status_code != 200:
            raise AIRequestError(
                f'Ошибка запуска модели {response.status_code}: {response.text[:300]}'
            )
        return response.json()