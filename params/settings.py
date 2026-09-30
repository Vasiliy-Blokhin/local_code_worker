"""Настройки проекта: логгер, промпты и значения по умолчанию."""

import logging
import sys


# Описание хандлера для логгера.
handler = logging.StreamHandler(sys.stdout)
formater = logging.Formatter(
    '%(name)s, %(funcName)s, %(asctime)s, %(levelname)s - %(message)s.'
)
handler.setFormatter(formater)
logger = logging.getLogger(name=__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(handler)


# ---------------------------------------------------------------------------
# Значения по умолчанию для веб-формы (могут быть переопределены в интерфейсе)
# ---------------------------------------------------------------------------
project_url = 'https://github.com/Vasiliy-Blokhin/local_code_worker'
DEFAULT_ARCHIVE_URL = f'{project_url}/archive/refs/heads/dev.zip'
DEFAULT_API_URL = 'http://192.168.0.23:8000/'
DEFAULT_MODEL = 'qwen25-coder-14b-unc'


BASE_PROMPT = lambda role=None, task=None, instruction=None, \
    restriction=None, output=None, prompt=None: f'''
###Роль:
{role}

###Задача:
{task}

###Инструкция:
{instruction}

###Строгие ограничения:
{restriction}

###Запрос пользователя:
{prompt}

###Формат вывода:
{output}
'''
PROMPT_CODE_REWORK = lambda prompt=None: BASE_PROMPT(
    role='Ты (python)/(Type java script) senior разработчик.',
    task='Твоя задача получить репозиторий с кодом, изучить ' \
    'его. После чего получить задачу от запроса и понять какие ' \
    'изменения нужно внести в код. Изменить код и сформировать ' \
    'ответ в соответствии с **Формат вывода** в виде json словаря.',
    instruction='1. Получить репозиторий с кодом;\n2. Изучить полученный код.\n' \
    '3. Получить **Запрос пользователя**.\n4. Изучить **Запрос пользователя** ' \
    'и понять что нужно сделать.\n5. Внести изменения в полученный код ' \
    'в соответствии с полученным заданием.\n6. Сформировать ответ в виде ' \
    'json словаря как в **Формат вывода**.\n7. Передать ответ пользователю.',
    restriction='1. Исключить любой вывод кроме результата json ' \
    'словаря как в **Формат вывода**',
    prompt=prompt,
    output='[{"file": <file path>, "content": <file content>}]'
)

PROMPT_RECOVERY_JSON = lambda prompt=None: BASE_PROMPT(
    role='Ты json декодер.',
    task='Твоя задача получить json словарь, изучить ' \
    'его. После чего получить задачу от запроса и понять какие ' \
    'изменения нужно внести в json словарь. Изменить json словарь и сформировать ' \
    'ответ в соответствии с **Формат вывода** в виде json словаря.',
    instruction='1. Получить json словарь;\n2. Изучить полученный json словарь.\n' \
    '3. Получить **Запрос пользователя**.\n4. Изучить **Запрос пользователя** ' \
    'и понять что нужно сделать.\n5. Внести изменения в полученный json словарь ' \
    'в соответствии с полученным заданием.\n6. Сформировать ответ в виде ' \
    'json словаря как в **Формат вывода**.\n7. Передать ответ пользователю.',
    restriction='1. Исключить любой вывод кроме результата json ' \
    'словаря как в **Формат вывода**',
    prompt=f'Получена ошибка декодирования - "{prompt}". Исправь её.',
    output='[{"file": <file path>, "content": <file content>}]'
)