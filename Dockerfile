FROM python:3.11-slim

WORKDIR /app

# Installation des dépendances système nécessaires pour curl_cffi et SQLite
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Port par défaut pour le Web
ENV PORT=8080
EXPOSE 8080

CMD ["python", "run_all.py"]
