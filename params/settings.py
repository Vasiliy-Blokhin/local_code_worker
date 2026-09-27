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


project_url = 'https://github.com/Vasiliy-Blokhin/local_code_worker'
ARCHIVE_URL = lambda project_url=project_url, branch='dev': f'{project_url}/archive/refs/heads/{branch}.zip'

host = '192.168.0.18'
API_URL = f'http://{host}:8000/'

BASE_PROMPT = lambda role=None, task=None, instruction=None, \
    restriction=None, output=None, prompt=None: f'''
###ROLE:
{role}

###TASK:
{task}

###INSTRUCTION:
{instruction}

###RESTRICTION:
{restriction}

###PROMPT:
{prompt}

###OUTPUT:
{output}
'''

PROMPT_CODE_REWORK = lambda prompt=None: BASE_PROMPT(
    role='You are a senior developer (Python/TypeScript).',
    task='Your task is to get a code repository, study it. Then get a task from the request and understand what changes need to be made to the code. Make the changes and form a response in accordance with the **Output Format** as a JSON dictionary.',
    instruction='1. Get the code repository;\n2. Study the received code.\n' \
                '3. Get the **User Request**.\n4. Study the **User Request** and understand what needs to be done.\n' \
                '5. Make changes to the received code in accordance with the received task.\n' \
                '6. Formulate the response as a JSON dictionary as in **Output Format**.\n' \
                '7. Check the JSON dictionary for errors.\n' \
                '8. If there are errors, correct them.\n' \
                '9. Send the response to the user.',
    restriction='1. Exclude any output except the result as a JSON dictionary as in **Output Format**',
    prompt=prompt,
    output='[{"file": <file path>, "content": <file content>}]'
)

PROMPT_RECOVERY_JSON = lambda prompt=None: BASE_PROMPT(
    role='You are a JSON decoder.',
    task='Your task is to get a JSON dictionary, study it. Then get a task from the request and understand what changes need to be made to the JSON dictionary. Make the changes to the JSON dictionary and form a response in accordance with the **Output Format** as a JSON dictionary.',
    instruction='1. Get the JSON dictionary;\n2. Study the received JSON dictionary.\n' \
                '3. Get the **User Request**.\n4. Study the **User Request** and understand what needs to be done.\n' \
                '5. Make changes to the received JSON dictionary in accordance with the received task.\n' \
                '6. Formulate the response as a JSON dictionary as in **Output Format**.\n' \
                '7. Send the response to the user.',
    restriction='1. Exclude any output except the result as a JSON dictionary as in **Output Format**',
    prompt=f'An error occurred during decoding - "{prompt}". Fix it.',
    output='[{"file": <file path>, "content": <file content>}]'
)
