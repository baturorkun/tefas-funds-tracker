FROM python:3.13-slim

WORKDIR /app

RUN pip install --no-cache-dir requests reportlab python-dotenv matplotlib

COPY main.py .

CMD ["python", "main.py"]
