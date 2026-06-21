ARG FRONTEND_NODE_IMAGE=node:24-alpine
FROM ${FRONTEND_NODE_IMAGE} AS frontend-builder

WORKDIR /src/frontend

COPY frontend/package*.json ./
RUN npm ci

COPY frontend ./
RUN npm run build

FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV MEDIA_DIRS=/media
ENV TRASH_DIR=/trash
ENV APP_DATA_DIR=/data
ENV WHISPER_MODEL=large-v3

COPY deploy/unraid-frontend/requirements.txt ./requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY --from=frontend-builder /src/frontend/dist ./frontend/dist

EXPOSE 18180

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18180"]
