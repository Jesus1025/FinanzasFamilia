"""Entry point para desarrollo local: python run.py"""
import uvicorn

from app.config import config

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=config.APP_PORT, reload=False)
