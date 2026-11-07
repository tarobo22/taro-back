FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY api/requirements.txt /app/api/requirements.txt
COPY constraints-dev.txt /app/constraints-dev.txt
RUN pip install --no-cache-dir -r /app/api/requirements.txt -c /app/constraints-dev.txt \
    && useradd --uid 10001 --create-home yeoun \
    && mkdir -p /app/.local/private-audio \
    && chown -R yeoun:yeoun /app/.local
COPY api /app/api
COPY shared /app/shared
USER yeoun
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
