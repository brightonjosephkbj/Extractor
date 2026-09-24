FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Deno is required by yt-dlp for YouTube's player JS (signature deciphering).
# Without it, extraction fails with "No supported JavaScript runtime could be found".
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh
ENV PATH="/usr/local/bin:${PATH}"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PORT=8080
EXPOSE 8080

CMD gunicorn -b 0.0.0.0:${PORT:-8080} app:app --timeout 300
