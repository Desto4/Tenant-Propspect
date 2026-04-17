FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt
RUN playwright install chromium

COPY . .

EXPOSE 8504

CMD gunicorn flask_app_new:app --bind 0.0.0.0:${PORT:-8504} --timeout 120 --workers 1 --threads 4
