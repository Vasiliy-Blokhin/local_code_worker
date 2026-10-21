@echo off
chcp 65001 >nul
REM ==== Удаление автозапуска local_code_worker ====
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\local_code_worker.bat" 2>nul && echo Автозапуск удалён. || echo Автозапуск не был установлен.
pause
