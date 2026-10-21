@echo off
chcp 65001 >nul
REM ==== Автозапуск local_code_worker при входе в Windows ====
REM Кладёт лаунчер в папку «Автозагрузка» пользователя (права администратора не нужны).
REM Задержка 60 с — чтобы успел подняться AI_Manager. Окно сервиса стартует свёрнутым.
if not exist "%~dp0..\venv" ( echo Сначала выполните scripts\setup.bat & pause & exit /b 1 )
set "LAUNCHER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\local_code_worker.bat"
(
  echo @echo off
  echo timeout /t 60 /nobreak ^>nul
  echo start "local_code_worker" /min cmd /c ""%~dp0run.bat""
) > "%LAUNCHER%"
if exist "%LAUNCHER%" ( echo Автозапуск установлен: %LAUNCHER% ) else ( echo Ошибка установки автозапуска. )
pause
