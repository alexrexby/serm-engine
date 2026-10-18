# Production Dockerfile for SERM Distribution & OPP Telegram Bot Engine
FROM python:3.12-slim

# Prevent python from writing pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install system dependencies needed for Playwright and Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    ca-certificates \
    fonts-liberation \
    libnss3 \
    libxss1 \
    libasound2 \
    libgbm1 \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies first: this layer is rebuilt only when requirements change,
# not on every code edit (requirements.txt mirrors [project].dependencies of pyproject.toml)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium with OS dependencies
RUN playwright install --with-deps chromium

# Copy project files (.dockerignore keeps .env, data/ and own profiles out of the image)
COPY . .

# Editable install: adds the `serm` command, but the code keeps running from /app.
# A regular `pip install .` would copy the modules into site-packages, where
# BASE_DIR-relative paths (data/, profiles/, templates/, .env) point to the wrong place.
RUN pip install --no-cache-dir --no-deps -e .

# Mount points: data/ and profiles/ come from the host (see docker-compose.yml).
# The process runs as root on purpose: on a Linux host a bind-mounted ./data is created
# root:root, and a non-root user would crash-loop on the first write.
RUN mkdir -p /app/data/sessions /app/data/exports /app/profiles

# Expose admin panel port
EXPOSE 8180

CMD ["python3", "scheduler.py"]
