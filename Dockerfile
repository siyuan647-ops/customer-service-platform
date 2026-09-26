FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml ./
RUN mkdir -p backend src/kimi_agent_spike && \
    touch README.md backend/__init__.py src/kimi_agent_spike/__init__.py && \
    pip install --upgrade pip && \
    pip install --index-url https://download.pytorch.org/whl/cpu "torch==2.14.0+cpu" && \
    pip install .

COPY backend ./backend
COPY src ./src
COPY README.md ./
COPY migrations ./migrations
COPY knowledge_docs ./knowledge_docs
COPY alembic.ini ./
RUN pip install --no-deps --force-reinstall .

EXPOSE 8000
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
