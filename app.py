"""Веб-интерфейс для local_code_worker.

Один пользователь, без аутентификации. Запуск: python app.py -> http://0.0.0.0:5000
"""

import logging
import os

from flask import Flask, abort, jsonify, render_template, request, send_file

from modules.rework_worker import CodeReworker
from params.settings import (DEFAULT_API_URL, DEFAULT_ARCHIVE_URL, DEFAULT_MODEL,
                             logger)

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(BASE_DIR, 'results')
os.makedirs(RESULTS_DIR, exist_ok=True)

MAX_STORED_ARCHIVES = 20


# ---------------------------------------------------------------------------
# Перехват логов, чтобы показывать их в интерфейсе (один пользователь — так можно)
# ---------------------------------------------------------------------------
class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(self.format(record))


list_handler = ListHandler()
list_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S'))
logger.addHandler(list_handler)


def _cleanup_results():
    """Оставить только последние MAX_STORED_ARCHIVES архивов."""
    archives = sorted(
        (os.path.join(RESULTS_DIR, f) for f in os.listdir(RESULTS_DIR) if f.endswith('.zip')),
        key=os.path.getmtime,
    )
    for old in archives[:-MAX_STORED_ARCHIVES]:
        try:
            os.remove(old)
        except OSError:
            pass


@app.route('/')
def index():
    return render_template(
        'index.html',
        default_api_url=DEFAULT_API_URL,
        default_archive_url=DEFAULT_ARCHIVE_URL,
        default_model=DEFAULT_MODEL,
    )


@app.route('/api/process', methods=['POST'])
def process():
    list_handler.records.clear()
    data = request.get_json(force=True, silent=True) or {}

    api_url = (data.get('api_url') or '').strip()
    archive_url = (data.get('archive_url') or '').strip()
    password = data.get('password') or ''
    prompt = (data.get('prompt') or '').strip()
    model = (data.get('model') or '').strip() or DEFAULT_MODEL

    field_errors = {}
    if not api_url:
        field_errors['api_url'] = 'Укажите URL ИИ API'
    if not archive_url:
        field_errors['archive_url'] = 'Укажите URL архива для переработки'
    if not password:
        field_errors['password'] = 'Введите пароль API (заголовок X-API-Password)'
    if not prompt:
        field_errors['prompt'] = 'Введите промпт — задачу для ИИ'
    if field_errors:
        return jsonify(ok=False, error='Заполните обязательные поля формы',
                       fields=field_errors, logs=[]), 400

    worker = CodeReworker(
        archive_url=archive_url,
        api_url=api_url,
        password=password,
        prompt=prompt,
        model=model,
    )
    try:
        zip_path = worker.run(results_dir=RESULTS_DIR)
    except Exception as exc:
        logger.error(f'Ошибка обработки: {exc}')
        return jsonify(ok=False, error=str(exc), logs=list_handler.records)

    _cleanup_results()
    filename = os.path.basename(zip_path)
    size = os.path.getsize(zip_path)
    logger.info(f'Архив готов к скачиванию: {filename} ({size} байт)')
    return jsonify(
        ok=True,
        filename=filename,
        size=size,
        download_url=f'/download/{filename}',
        logs=list_handler.records,
    )


@app.route('/download/<filename>')
def download(filename):
    safe_name = os.path.basename(filename)
    path = os.path.join(RESULTS_DIR, safe_name)
    if not safe_name.endswith('.zip') or not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=safe_name)


@app.route('/results')
def results_list():
    """Список готовых архивов (для повторного скачивания)."""
    archives = sorted(
        (f for f in os.listdir(RESULTS_DIR) if f.endswith('.zip')),
        key=lambda f: os.path.getmtime(os.path.join(RESULTS_DIR, f)),
        reverse=True,
    )
    return jsonify(archives=archives)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)
