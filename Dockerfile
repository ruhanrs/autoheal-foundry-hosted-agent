FROM python:3.12-slim

# Create a non-root user for security
RUN groupadd --gid 1001 agent \
 && useradd --uid 1001 --gid agent --shell /bin/sh --create-home agent

WORKDIR /app

# Install dependencies first (separate layer — cache-friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip==24.3.1 \
 && pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY autoheal/ autoheal/
COPY main.py .

# Drop privileges
USER agent

EXPOSE 8088

# Healthcheck: verify the Python interpreter is alive and imports succeed
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import autoheal.github, autoheal.tools, autoheal.instructions" || exit 1

CMD ["python", "main.py"]
