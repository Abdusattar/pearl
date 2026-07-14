FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends libpq-dev tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Bishkek
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic/ alembic/
COPY alembic.ini alembic.ini
COPY app/ app/

RUN mkdir -p media/receipts

EXPOSE 8000

CMD alembic upgrade head && python app/seed.py && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
