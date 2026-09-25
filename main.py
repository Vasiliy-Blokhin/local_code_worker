import os
import zipfile
import requests
from io import BytesIO
import shutil
import random
import string

from params.settings import ARCHIVE_URL
from modules.ai_worker import AIRequestHandler

class CodeReworker:
    def __init__(self):
        self.github_url = ARCHIVE_URL()
        self.output_zip = f'results/{self._generate_random_text()}.zip'
        self.temp_dir = self._generate_random_text()

    def __call__(self, *args, **kwds):
        # Извлекаем ZIP-архив
        self.get_zip(self._download_zip())
        
        # Получаем изменения от ИИ
        content = self._generate_content_context(self.temp_dir)
        ai_handler = AIRequestHandler()
        changes = ai_handler.send_request(system_prompt="добавь ко всем методам докстринги", content=content)
        
        # Вносим изменения
        self.make_changes(changes)
        
        # Создаем обновленный ZIP-архив
        self.create_zip()

        print(f'Обновленный ZIP-архив сохранен в {self.output_zip}')

    def _download_zip(self):
        response = requests.get(self.github_url)
        response.raise_for_status()
        return BytesIO(response.content)

    def get_zip(self, zip_path):
        os.makedirs(self.temp_dir, exist_ok=True)
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(self.temp_dir)

    def make_changes(self, changes):
        # Пример: Изменяем методы и файлы в директории
        for root, _, files in os.walk(self.temp_dir):
            for file in files:
                if file.endswith('.py'):
                    file_path = os.path.join(root, file)
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    
                    # Применяем изменения
                    for change in changes:
                        if change['file'] == file_path:
                            content = change['content']
                    
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(content)

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