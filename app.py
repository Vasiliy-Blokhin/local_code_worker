"""Точка входа: python app.py → http://127.0.0.1:5000 (адрес/порт — в .env)."""
import uvicorn

from lcw import config

if __name__ == "__main__":
    if config.HOST not in ("127.0.0.1", "localhost") and not config.PASSWORD:
        print("ВНИМАНИЕ: сервис доступен по сети без пароля, а агент выполняет команды "
              "на этой машине. Задайте LCW_PASSWORD в .env.")
    uvicorn.run("lcw.server:app", host=config.HOST, port=config.PORT, log_level="warning")
