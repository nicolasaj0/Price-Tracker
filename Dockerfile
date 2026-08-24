# Dockerfile ultra-leve para o PriceTracker Discord Bot
FROM python:3.11-slim

# Evita criação de arquivos .pyc e força flush imediato de stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Instala dependências do sistema mínimas se necessário
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copia e instala requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia os arquivos da aplicação
COPY db.py steam.py eshop.py chart.py itad.py bot.py .

# Cria usuário não-root e estrutura de volume persistente com permissões adequadas
RUN useradd -m -u 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser

# Executa o bot
CMD ["python", "bot.py"]
