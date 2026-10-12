FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends     poppler-utils     libmagic1     curl     && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app/app

ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
