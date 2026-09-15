FROM docker.io/library/python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fs_backend.py main.py handler.schema.json ./

ENV SIJBA_CONFIG_DIR=/data/config
ENV PORT=8081
ENV ROOT_PATH=/config-ui

EXPOSE 8081

CMD ["python", "main.py"]
