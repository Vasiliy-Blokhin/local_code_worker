# Это изменение было внесено автоматически
import os
import zipfile
import requests
from io import BytesIO

def download_zip(url):
    response = requests.get(url)
    response.raise_for_status()
    return BytesIO(response.content)

def extract_zip(zip_path, extract_to):
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(extract_to)

def make_changes(directory):
    # Пример: Изменяем методы и файлы в директории
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith('.py'):
                file_path = os.path.join(root, file)
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                # Пример изменений: Добавляем комментарий в начало файла
                modified_content = "# Это изменение было внесено автоматически\n" + content
                
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(modified_content)

def create_zip(directory, output_zip):
    with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(directory):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, directory)
                zipf.write(file_path, arcname)

def main():
    # Пример входных данных
    zip_url = 'https://github.com/Vasiliy-Blokhin/local_code_worker/archive/refs/heads/main.zip'
    output_zip = 'modified_project.zip'
    
    # Скачиваем ZIP-архив
    zip_content = download_zip(zip_url)
    
    # Создаем временную директорию для извлечения
    temp_dir = 'temp_extract'
    os.makedirs(temp_dir, exist_ok=True)
    
    # Извлекаем ZIP-архив
    extract_zip(zip_content, temp_dir)
    
    # Вносим изменения
    make_changes(temp_dir)
    
    # Создаем обновленный ZIP-архив
    create_zip(temp_dir, output_zip)
    
    # Очищаем временную директорию
    os.rmdir(temp_dir)
    
    print(f'Обновленный ZIP-архив сохранен в {output_zip}')

if __name__ == '__main__':
    main()