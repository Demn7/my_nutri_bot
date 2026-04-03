FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir matplotlib==3.8.2

COPY . .

CMD ["python", "main.py"]
