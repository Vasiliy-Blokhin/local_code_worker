import os
import zipfile
import requests
from io import BytesIO
import shutil
import random
import string

from params.settings import ARCHIVE_URL


class CodeReworker:
    def __init__(self):
        self.github_url = ARCHIVE_URL()
        self.output_zip = f'results/{self._generate_random_text()}.zip'
        self.temp_dir = self._generate_random_text()

    def __call__(self, *args, **kwds):          
        # Извлекаем ZIP-архив
        self.get_zip(self._download_zip())
        
        # Вносим изменения
        self.make_changes()
        
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

    def make_changes(self):
        # Пример: Изменяем методы и файлы в директории
        for root, _, files in os.walk(self.temp_dir):
            for file in files:
                if file.endswith('.py'):
                    file_path = os.path.join(root, file)
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    
                    # Пример изменений: Добавляем комментарий в начало файла
                    modified_content = "# Это изменение было внесено автоматически\n" + content
                    
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(modified_content)

    def create_zip(self):
        with zipfile.ZipFile(self.output_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(self.temp_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, self.temp_dir)
                    zipf.write(file_path, arcname)

        shutil.rmtree(self.temp_dir)

    # ------------------------------------------------------------------------------------------------   
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