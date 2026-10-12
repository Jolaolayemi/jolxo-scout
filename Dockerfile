FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scanner.py bot.py ./
# /data is the Railway volume (cache, sqlite, last report). Runs as root so it can write the volume.
CMD ["python", "bot.py"]
