# One image, two Railway services from it: the API (this image's default
# command) and the worker (override the start command to
# `python -m webapp.api.worker` in Railway's service settings). Both need
# Tesseract as a native binary - not something a Vercel function could
# install - which is the whole reason this half of the app runs here.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY webapp/api/requirements.txt /app/webapp/api/requirements.txt
RUN pip install --no-cache-dir -r webapp/api/requirements.txt

COPY pdf_extractor /app/pdf_extractor
COPY webapp /app/webapp

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "webapp.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
