FROM python:3.11-slim

WORKDIR /app

RUN pip install fastapi uvicorn

COPY app.py .

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
