@echo off
chcp 65001 >nul
REM ==== Запуск local_code_worker (с авто-перезапуском при падении) ====
cd /d "%~dp0.."
if not exist venv ( echo Сначала выполните scripts\setup.bat & pause & exit /b 1 )
call venv\Scripts\activate.bat

:loop
echo [%date% %time%] Запуск local_code_worker...
python app.py
echo [%date% %time%] Сервис остановлен, перезапуск через 5 секунд...
timeout /t 5 /nobreak >nul
goto loop
