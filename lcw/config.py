"""Настройки из .env (все параметры имеют разумные значения по умолчанию)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


HOST = os.getenv("LCW_HOST", "127.0.0.1")
PORT = _int("LCW_PORT", 9009)
# Пароль веб-интерфейса. Пустой — без входа (только для 127.0.0.1!).
PASSWORD = os.getenv("LCW_PASSWORD", "")

# AI Manager (OpenAI-совместимый /v1 + управление моделями /api)
AIM_URL = os.getenv("AIM_URL", "http://127.0.0.1:8000").rstrip("/")
AIM_PASSWORD = os.getenv("AIM_PASSWORD", "")
MODEL = os.getenv("LCW_MODEL", "gpt-oss-20b")
# Имя проекта в очереди задач AI Manager (заголовок X-Project: история /api/jobs, статистика)
AIM_PROJECT = os.getenv("LCW_AIM_PROJECT", "local_code_worker")
# Размер контекста модели (должен совпадать с "context" в реестре AI Manager)
CONTEXT = _int("LCW_CONTEXT", 100000)
REASONING = os.getenv("LCW_REASONING", "low")  # low | medium | high
MAX_TOKENS = _int("LCW_MAX_TOKENS", 8192)      # лимит одного ответа модели

DATA_DIR = Path(os.getenv("LCW_DATA_DIR", str(ROOT / "data"))).resolve()
SESSIONS_DIR = DATA_DIR / "sessions"
WORKSPACES_DIR = DATA_DIR / "workspaces"
PROJECTS_FILE = DATA_DIR / "projects.json"

MAX_STEPS = _int("LCW_MAX_STEPS", 50)                # шагов (вызовов модели) на один запрос
COMMAND_TIMEOUT = _int("LCW_COMMAND_TIMEOUT", 180)   # секунд на команду `run`
TOOL_OUTPUT_CHARS = _int("LCW_TOOL_OUTPUT_CHARS", 12000)  # обрезка вывода инструментов для модели
COMPACT_AT = float(os.getenv("LCW_COMPACT_AT", "0.75"))   # доля контекста для авто-сжатия

for d in (SESSIONS_DIR, WORKSPACES_DIR):
    d.mkdir(parents=True, exist_ok=True)
