FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-osd fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /tmp/video-mcp \
    && chown -R appuser:appuser /tmp/video-mcp /app
USER appuser

EXPOSE 8000

CMD ["python", "-m", "app.main"]
