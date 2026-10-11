@echo off
chcp 65001 >nul
REM ==== Установка local_code_worker: venv + зависимости + .env ====
cd /d "%~dp0.."
if not exist venv (
    python -m venv venv || ( echo Не найден Python 3.11+ & pause & exit /b 1 )
)
call venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt || ( echo Ошибка установки зависимостей & pause & exit /b 1 )
if not exist .env (
    copy /y .env.example .env >nul
    REM Пароль AI Manager берём из соседнего проекта, если он есть
    if exist ..\AI_Manager\.env powershell -NoProfile -Command ^
      "$pw = (Select-String -Path '..\AI_Manager\.env' -Pattern '^AIM_PASSWORD=(.*)$').Matches.Groups[1].Value;" ^
      "if ($pw) { (Get-Content .env) -replace '^AIM_PASSWORD=.*', ('AIM_PASSWORD=' + $pw) | Set-Content -Encoding utf8 .env }"
    echo Создан .env — проверьте AIM_URL и AIM_PASSWORD.
)
echo Готово. Запуск: scripts\run.bat
pause
