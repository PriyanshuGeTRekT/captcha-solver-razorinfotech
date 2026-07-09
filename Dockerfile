FROM python:3.12-slim

WORKDIR /app

# System deps for Playwright Chromium + Tesseract OCR
RUN apt-get update && \
    apt-get install -y --no-install-recommends tesseract-ocr curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Install Python packages (PyTorch CPU first, then the rest)
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt && \
    python -m playwright install --with-deps chromium

COPY . .

# Listen on 0.0.0.0. Default port 7860 = Hugging Face Spaces convention (matches the
# app_port in README.md). Render/other PaaS inject their own $PORT at runtime, which
# overrides this image ENV, so the same image works on both.
ENV BACKLINK_HOST=0.0.0.0 \
    PORT=7860
EXPOSE 7860

CMD ["python", "web_server.py"]
