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

# Droits d'écriture pour l'utilisateur 1000 (requis par Hugging Face Spaces)
RUN useradd -m -u 1000 user && chown -R user:user /app
USER user

# Port 7860 par défaut pour Hugging Face Spaces (ou PORT d'environnement)
ENV PORT=7860
EXPOSE 7860

CMD ["python", "run_all.py"]
