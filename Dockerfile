FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY manage.py ./
COPY .env.example ./
COPY .env.docker.example ./

RUN mkdir -p /app/data /root/.codex

EXPOSE 8080

CMD ["python", "-m", "app.main"]
