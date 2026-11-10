FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY camara_mocks.py .

# CMD is overridden per-service in docker-compose.yml to select which app/port to serve.
CMD ["uvicorn", "camara_mocks:qod_app", "--host", "0.0.0.0", "--port", "9001"]
