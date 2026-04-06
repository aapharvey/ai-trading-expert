FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer — rebuilds only if requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir \
    requests \
    python-dotenv \
    APScheduler \
    ta \
    colorama \
    && rm -rf /root/.cache/pip

# Copy source code
COPY config.py logger.py main.py ./
COPY src/ ./src/

# Directories for persistent data (mounted as volumes at runtime)
RUN mkdir -p logs

# Run as non-root user
RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser

CMD ["python", "main.py"]
