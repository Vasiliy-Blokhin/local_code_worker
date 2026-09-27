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
    role='You are a senior Python developer.',
    task='Get a code repository, study it. Then get a task from the request, '
         'understand what changes need to be made, implement them, '
         'and return the result as a valid JSON dictionary.',
    instruction=(
        '1. Study the received code.\n'
        '2. Understand the User Request.\n'
        '3. Make the required changes.\n'
        '4. Output a JSON dictionary in the format: '
        '[{"file": "<file path>", "content": "<file content>"}]\n'
        '5. Strict JSON requirements:\n'
        '   - Double quotes for all keys and string values.\n'
        '   - Correct commas between fields; NO trailing commas.\n'
        '   - ALL backslashes in strings MUST be doubled: \\\\ not \\.\n'
        '   - Use only valid escapes: \\", \\\\, \\/, \\n, \\r, \\t, \\b, \\f, \\uXXXX.\n'
        '   - Raw JSON only: no markdown code fences, no explanatory text.\n'
        '6. Validate the JSON yourself before outputting: check quoting, '
        'escaping, commas, and balanced brackets.\n'
        '7. If content contains LaTeX or Windows paths, escape every backslash explicitly.'
    ),
    restriction=(
        '1. Output ONLY the raw JSON. NO markdown, NO text outside JSON.\n'
        '2. The result must be parseable by json.loads() without modifications.\n'
        '3. If valid JSON cannot be produced, return {"error": "unable to produce valid JSON"}'
    ),
    prompt=prompt,
    output='[{"file": "<file path>", "content": "<file content>"}]'
)
