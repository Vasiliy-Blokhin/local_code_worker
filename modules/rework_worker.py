"""Ядро обработки: скачивание архива, запрос к ИИ, применение правок, сборка архива."""

import json
import os
import random
import shutil
import string
import zipfile
from io import BytesIO

import requests

from modules.ai_worker import AIRequestError, AIRequestHandler
from params.settings import PROMPT_CODE_REWORK, PROMPT_RECOVERY_JSON, logger

# Корень проекта: modules/.. — чтобы временные пути не зависели от рабочей директории.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CodeReworker:
    def __init__(self, archive_url, api_url, password, prompt, model):
        self.archive_url = archive_url
        self.prompt = prompt
        self.output_zip = None
        self.temp_dir = os.path.join(BASE_DIR, 'trash', self._generate_random_text())
        self.ai_worker = AIRequestHandler(api_url=api_url, password=password, model=model)

    def run(self, results_dir):
        """Полный цикл обработки. Возвращает путь к готовому архиву."""
        os.makedirs(results_dir, exist_ok=True)
        os.makedirs(self.temp_dir, exist_ok=True)
        try:
            self._download_and_extract()
            self._check_ai()
            changes = self._request_changes()
            applied = self._apply_changes(changes)
            if not applied:
                logger.warning('ИИ не вернул применимых изменений, архив будет без правок')
            return self._create_zip(results_dir)
        finally:
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    def _download_and_extract(self):
        logger.info(f'Скачивание архива: {self.archive_url}')
        try:
            response = requests.get(self.archive_url, timeout=120)
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f'Не удалось скачать архив: {exc}') from exc

        logger.info(f'Архив скачан ({len(response.content)} байт), распаковка...')
        with zipfile.ZipFile(BytesIO(response.content), 'r') as zip_ref:
            self._safe_extract(zip_ref, self.temp_dir)

    @staticmethod
    def _safe_extract(zip_ref, target_dir):
        """Распаковка с защитой от Zip Slip."""
        target_real = os.path.realpath(target_dir)
        for member in zip_ref.infolist():
            member_real = os.path.realpath(os.path.join(target_dir, member.filename))
            if not (member_real == target_real or member_real.startswith(target_real + os.sep)):
                raise RuntimeError(f'Небезопасный путь в архиве: {member.filename}')
        zip_ref.extractall(target_dir)

    def _check_ai(self):
        logger.info('Проверка доступности ИИ агента...')
        try:
            self.ai_worker.start_model()
            self.ai_worker.system_prompt = 'Выполняется проверка связи. Передай только одно слово: Работает'
            self.ai_worker.content = 'Проверка передачи контента.'
            reply = self.ai_worker.send_request()['choices'][0]['message']['content']
            logger.info(f'ИИ агент ответил: {reply.strip()[:100]}')
        except (AIRequestError, KeyError, IndexError) as exc:
            raise RuntimeError(f'ИИ агент не работает: {exc}') from exc

    def _request_changes(self):
        self.ai_worker.system_prompt = PROMPT_CODE_REWORK(prompt=self.prompt)
        # Относительные (к корню проекта) пути файлов — их же вернет модель.
        self.ai_worker.content = str(self._generate_content_context())
        logger.info(f'Отправка кода ({self.ai_worker.content.__len__()} символов) в модель с промптом: {self.prompt!r}')
        response = self.ai_worker.send_request()
        changes_raw = response['choices'][0]['message']['content']
        return self._parse_response_to_dict_list(changes_raw)

    def _apply_changes(self, changes):
        """Применить правки к файлам. Возвращает количество измененных файлов."""
        if not isinstance(changes, list):
            raise RuntimeError(f'Неожиданный формат ответа ИИ: ожидался список, получено {type(changes).__name__}')

        # Маппинг: относительный путь файла (как в запросе) -> абсолютный путь на диске.
        file_map = {}
        for root, _, files in os.walk(self.temp_dir):
            for file in files:
                if file.endswith('.py'):
                    abs_path = os.path.join(root, file)
                    rel_id = os.path.relpath(abs_path, BASE_DIR).replace(os.sep, '/')
                    file_map[rel_id] = abs_path

        applied = 0
        for change in changes:
            if not isinstance(change, dict) or 'file' not in change or 'content' not in change:
                logger.warning(f'Пропуск некорректной записи изменений: {str(change)[:120]}')
                continue

            change_file = str(change['file']).replace('\\', '/')
            abs_path = file_map.get(change_file)
            if abs_path is None:
                # Модель могла вернуть путь относительно распакованной папки — ищем по хвосту.
                matches = [p for rel, p in file_map.items() if rel.endswith(change_file) or change_file.endswith(rel)]
                abs_path = matches[0] if matches else None
            if abs_path is None:
                logger.warning(f'Файл из ответа ИИ не найден в архиве: {change_file}')
                continue

            with open(abs_path, 'w', encoding='utf-8') as f:
                f.write(change['content'])
            applied += 1
            logger.info(f'Изменен файл: {change_file}')
        return applied

    def _parse_response_to_dict_list(self, response_str):
        """
        Разобрать JSON-ответ модели в список словарей.

        :param response_str: JSON строка с информацией о файлах.
        :return: Список словарей с деталями файлов.
        """
        try:
            cleaned = response_str.replace('```json', '').replace('```', '').strip()
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.error(f'Ошибка декодирования JSON: {exc}. Попытка восстановления структуры')
            self.ai_worker.content = response_str
            self.ai_worker.system_prompt = PROMPT_RECOVERY_JSON(prompt=exc)
            recovery_raw = self.ai_worker.send_request()['choices'][0]['message']['content']
            recovery = self._parse_response_to_dict_list(recovery_raw)
            return recovery if len(recovery) else []

    def _create_zip(self, results_dir):
        name = f'rework_{self._generate_random_text()}.zip'
        self.output_zip = os.path.join(results_dir, name)
        with zipfile.ZipFile(self.output_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(self.temp_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, self.temp_dir)
                    zipf.write(file_path, arcname)
        logger.info(f'Обновленный ZIP-архив сохранен: {self.output_zip}')
        return self.output_zip

    # ------------------------------------------------------------------
    def _generate_content_context(self):
        """Список .py файлов архива с содержимым (идентификаторы путей — относительные)."""
        content = []
        for root, _, files in os.walk(self.temp_dir):
            for file in files:
                if file.endswith('.py'):
                    file_path = os.path.join(root, file)
                    with open(file_path, 'r', encoding='utf-8') as f:
                        file_content = f.read()
                    content.append({
                        'file': os.path.relpath(file_path, BASE_DIR).replace(os.sep, '/'),
                        'content': file_content,
                    })
        if not content:
            logger.warning('В архиве не найдено .py файлов')
        return content

    @staticmethod
    def _generate_random_text(length=10):
        """Сгенерировать случайную строку из букв и цифр."""
        characters = string.ascii_letters + string.digits
        return ''.join(random.choice(characters) for _ in range(length))
