FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    STEINER_WORKSPACE_ROOT=/srv/workspaces \
    STEINER_HOST=0.0.0.0 \
    STEINER_PORT=8088

COPY hub/requirements.txt ./hub/requirements.txt
RUN pip install --no-cache-dir -r hub/requirements.txt

COPY hub ./hub

RUN mkdir -p /srv/workspaces

EXPOSE 8088

CMD ["uvicorn", "hub.app.main:app", "--host", "0.0.0.0", "--port", "8088"]
