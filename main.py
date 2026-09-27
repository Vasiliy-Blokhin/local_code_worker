import os
import zipfile
import requests
from io import BytesIO
import shutil
import random
import string
import json

from params.settings import ARCHIVE_URL, logger, PROMPT_CODE_REWORK, PROMPT_RECOVERY_JSON
from modules.ai_worker import AIRequestHandler

class CodeReworker:
    def __init__(self):
        self.github_url = ARCHIVE_URL()
        self.output_zip = f'results/{self._generate_random_text()}.zip'
        self.temp_dir = f'trash/{self._generate_random_text()}'

        self.ai_worker = AIRequestHandler()

    def __call__(self, *args, **kwds):
        try:
            # Извлекаем ZIP-архив
            self.get_zip(self._download_zip())

            try:
                self.ai_worker.start_model()
                self.ai_worker.system_prompt = "Выполняется проверка связи. Передай толлько одно слово: Работает"
                self.ai_worker.content = 'Проверка передачи контента.'
                logger.debug(f'Проверка работы ИИ агента - {self.ai_worker.send_request()['choices'][0]['message']['content']}')
            except Exception:
                raise Exception('ИИ агент не работает')
            
            self.ai_worker.system_prompt = PROMPT_CODE_REWORK(prompt="добавь ко всем методам докстринги.")
            self.ai_worker.content = str(self._generate_content_context(self.temp_dir))  # Ensure content is a list
            changes = self.ai_worker.send_request()
            # Вносим изменения
            self.make_changes(changes)
            
            # Создаем обновленный ZIP-архив
            self.create_zip()

            logger.debug(f'Обновленный ZIP-архив сохранен в {self.output_zip}')
        except Exception as e:
            logger.error(f'Ошибка в работе: {e}')


    def _download_zip(self):
        response = requests.get(self.github_url)
        response.raise_for_status()
        return BytesIO(response.content)

    def get_zip(self, zip_path):
        os.makedirs(self.temp_dir, exist_ok=True)
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(self.temp_dir)

    def make_changes(self, changes):
        # Ensure changes is a list
        changes = changes['choices'][0]['message']['content']
        changes = self._parse_response_to_dict_list(changes)
        logger.debug(f'changes: "{changes}"')
        
        for root, _, files in os.walk(self.temp_dir):
            for file in files:
                if file.endswith('.py'):
                    file_path = os.path.join(root, file).replace('\\', '/')
                    
                    # Применяем изменения
                    for change in changes:
                        if change['file'] == file_path:
                            with open(file_path, 'w', encoding='utf-8') as f:
                                f.write(change['content'])
                                logger.info(f'path - {file_path}')
                                logger.info(f"content - {change['content']}")

    def _parse_response_to_dict_list(self, response_str):
        """
        Parse a JSON string response to a list of dictionaries.

        :param response_str: JSON string containing file information.
        :return: List of dictionaries with file details.
        """
        try:
            response_str = response_str.replace('```json', '').replace('```', '')
            response_data = json.loads(response_str)
            return response_data
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка декодирования JSON: {e}\nВосстановление структуры")
            self.ai_worker.content = response_str
            self.ai_worker.system_prompt = PROMPT_RECOVERY_JSON(prompt=e)
            recovery = self._parse_response_to_dict_list(
                self.ai_worker.send_request()['choices'][0]['message']['content']
            )
            return recovery if len(recovery) else []

    
    def create_zip(self):
        with zipfile.ZipFile(self.output_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(self.temp_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, self.temp_dir)
                    zipf.write(file_path, arcname)

        shutil.rmtree(self.temp_dir)

    # ------------------------------------------------------------------------------------------------   
    def _generate_content_context(self, directory):
        content = []
        for root, _, files in os.walk(directory):
            for file in files:
                if file.endswith('.py'):
                    file_path = os.path.join(root, file)
                    with open(file_path, 'r', encoding='utf-8') as f:
                        file_content = f.read()
                    content.append({
                        "file": file_path,
                        "content": file_content
                    })
        return content

    @staticmethod
    def _generate_random_text(length=10):
        """
        Generate a random text of specified length.

        :param length: The length of the random text to generate.
        :return: A string of random letters and digits.
        """
        characters = string.ascii_letters + string.digits
        random_text = ''.join(random.choice(characters) for _ in range(length))
        return random_text


if __name__ == '__main__':
    CodeReworker()()