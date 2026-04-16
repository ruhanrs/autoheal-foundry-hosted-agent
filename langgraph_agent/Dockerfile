FROM python:3.12-slim

RUN groupadd --gid 1001 agent \
 && useradd --uid 1001 --gid agent --shell /bin/sh --create-home agent

WORKDIR /app

COPY requirements.txt .
COPY langgraph_agent/requirements.txt langgraph_requirements.txt

RUN pip install --no-cache-dir --upgrade pip==24.3.1 \
 && pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir -r langgraph_requirements.txt

COPY autoheal/ autoheal/
COPY langgraph_agent/ langgraph_agent/

USER agent

EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import langgraph_agent.host, langgraph_agent.graph" || exit 1

CMD ["python", "-m", "langgraph_agent.host"]

