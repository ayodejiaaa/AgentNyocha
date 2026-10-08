# One image, two containers: the API (this image's default command) and the
# worker (command: python -m webapp.api.worker). Both need Tesseract as a native
# program, which is why this half of the app is a container on a real server and
# not a serverless function.
#
# Build from the REPO ROOT as the context (docker compose does this for you), and
# note .dockerignore there: it keeps training data, sample PDFs and the local dev
# database out of the image.
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

# Not root. _local_data is only written while STORAGE_BACKEND=local.
RUN useradd --system --uid 10001 app \
    && mkdir -p /app/webapp/api/_local_data/files \
    && chown -R app /app/webapp/api/_local_data
USER app

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000
CMD ["uvicorn", "webapp.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
