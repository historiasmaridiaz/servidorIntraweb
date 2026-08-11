FROM python:3.11-slim

WORKDIR /app

# Instalar dependencias del sistema para Playwright y Python
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install-deps || true
RUN playwright install chromium || true

COPY . .

ENV PORT=8080
ENV HCLINICAS_API_HOST=0.0.0.0

EXPOSE 8080

CMD ["python", "server.py", "--host", "0.0.0.0", "--no-browser"]
