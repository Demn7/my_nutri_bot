FROM python:3.11-slim

WORKDIR /app

# Устанавливаем системные зависимости, нужные для matplotlib
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    python3-dev \
    libfreetype-dev \
    libpng-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Устанавливаем зависимости из requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Устанавливаем matplotlib отдельно (если его нет в requirements.txt)
RUN pip install --no-cache-dir matplotlib==3.8.2

COPY . .

CMD ["python", "main.py"]
