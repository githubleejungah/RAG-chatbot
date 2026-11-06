"""서버 진입점. App Service의 시작 명령이 `main:app`을 불러온다."""

import logging

from dotenv import load_dotenv

from api import create_app

load_dotenv()
logging.basicConfig(level=logging.INFO)

app = create_app()
