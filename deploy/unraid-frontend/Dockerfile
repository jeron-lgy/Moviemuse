ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV MEDIA_DIRS=/media
ENV TRASH_DIR=/trash
ENV APP_DATA_DIR=/data
ENV WHISPER_MODEL=large-v3

COPY deploy/unraid-frontend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY frontend/dist ./frontend/dist

EXPOSE 18180

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18180"]
