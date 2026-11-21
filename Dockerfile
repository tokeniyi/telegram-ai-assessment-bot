# Same Python version as CI (.github/workflows/tests.yml)
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System dependencies for OCR and image processing
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-eng \
    libglib2.0-0 \
    libgl1 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-ocr-extra.txt ./

RUN pip install --no-cache-dir \
    -r requirements.txt \
    -r requirements-ocr-extra.txt

COPY . .

# Run the application without root privileges
RUN useradd --create-home appuser \
    && chown -R appuser:appuser /app

USER appuser

CMD ["python", "main.py"]
