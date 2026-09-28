"""Сквозной тест веб-приложения с мок-сервером ИИ API.

Запуск: python -m pytest tests/ -v   (или)   python tests/test_e2e.py
"""

import ast
import io
import json
import os
import sys
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402

PASSWORD = '12345'
MODEL = 'qwen25-coder-14b-unc'
MARKER = '"""Изменено ИИ."""'

SAMPLE_MAIN_PY = (
    'def add(a, b):\n'
    '    return a + b\n'
    '\n'
    '\n'
    'class Calculator:\n'
    '    def multiply(self, x, y):\n'
    '        return x * y\n'
)

SAMPLE_README = '# Sample repo\n'


def make_sample_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('sample-repo/main.py', SAMPLE_MAIN_PY)
        z.writestr('sample-repo/README.md', SAMPLE_README)
        z.writestr('sample-repo/utils/helpers.py', 'def helper():\n    return True\n')
    return buf.getvalue()


class MockAIHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        return self.headers.get('X-API-Password') == PASSWORD

    def do_GET(self):
        if self.path == '/repo.zip':
            data = make_sample_zip()
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._send_json(404, {'error': 'not found'})

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length) if length else b'{}'
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}

        if self.path == f'/api/models/{MODEL}/start':
            if not self._authorized():
                self._send_json(401, {'error': 'invalid password'})
                return
            self._send_json(200, {'status': 'started'})
            return

        if self.path == '/v1/chat/completions':
            if not self._authorized():
                self._send_json(401, {'error': 'invalid password'})
                return
            messages = payload.get('messages', [])
            user_content = messages[-1]['content'] if messages else ''
            self._send_json(200, {'choices': [{'message': {'role': 'assistant',
                                                          'content': self._reply(user_content)}}]})
            return

        self._send_json(404, {'error': 'not found'})

    @staticmethod
    def _reply(user_content):
        # Проверка связи — отвечаем одним словом.
        try:
            files = ast.literal_eval(user_content)
        except (ValueError, SyntaxError):
            return 'Работает'

        # Имитация переработки: добавляем маркер к каждому .py файлу.
        result = []
        for item in files:
            result.append({'file': item['file'], 'content': item['content'] + '\n\n' + MARKER + '\n'})
        return '```json\n' + json.dumps(result, ensure_ascii=False) + '\n```'


class LocalCodeWorkerWebE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), MockAIHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.port}'
        app.config['TESTING'] = True
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _payload(self, **overrides):
        payload = {
            'api_url': self.base,
            'archive_url': f'{self.base}/repo.zip',
            'model': MODEL,
            'password': PASSWORD,
            'prompt': 'добавь ко всем методам докстринги',
        }
        payload.update(overrides)
        return payload

    def test_index_page(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for element_id in ('password', 'prompt', 'api_url', 'archive_url', 'run-btn'):
            self.assertIn(f'id="{element_id}"', html)

    def test_full_cycle_download_and_verify(self):
        response = self.client.post('/api/process', json=self._payload())
        self.assertEqual(response.status_code, 200, response.get_json())
        data = response.get_json()
        self.assertTrue(data['ok'], data)
        self.assertTrue(data['download_url'].startswith('/download/'))
        self.assertGreater(data['size'], 0)
        self.assertTrue(any('ИИ агент ответил' in line for line in data['logs']))

        # Скачиваем архив и проверяем содержимое.
        response = self.client.get(data['download_url'])
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as z:
            names = z.namelist()
            self.assertIn('sample-repo/main.py', names)
            self.assertIn('sample-repo/README.md', names)
            self.assertIn('sample-repo/utils/helpers.py', names)
            self.assertIn(MARKER, z.read('sample-repo/main.py').decode('utf-8'))
            self.assertIn(MARKER, z.read('sample-repo/utils/helpers.py').decode('utf-8'))
            # Не .py файлы не трогаем.
            self.assertEqual(z.read('sample-repo/README.md').decode('utf-8'), SAMPLE_README)

    def test_wrong_password_surfaces_error(self):
        response = self.client.post('/api/process', json=self._payload(password='wrong'))
        data = response.get_json()
        self.assertFalse(data['ok'])
        self.assertIn('401', data['error'])

    def test_unavailable_archive_surfaces_error(self):
        response = self.client.post(
            '/api/process',
            json=self._payload(archive_url=f'{self.base}/missing.zip'),
        )
        data = response.get_json()
        self.assertFalse(data['ok'])
        self.assertIn('404', data['error'])

    def test_validation_empty_fields(self):
        response = self.client.post('/api/process', json=self._payload(password='', prompt=' '))
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data['ok'])
        self.assertIn('password', data['fields'])
        self.assertIn('prompt', data['fields'])

    def test_results_listing(self):
        response = self.client.get('/results')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(name.endswith('.zip') for name in response.get_json()['archives']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
