FROM python:3.13-slim

ARG EXTRAS=0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    MODELSEEK_CONFIG=/app/config.yaml \
    MODELSEEK_IN_DOCKER=1

WORKDIR /app

COPY requirements.txt requirements-extra.txt ./
RUN pip install -r requirements.txt \
 && if [ "$EXTRAS" = "1" ]; then pip install curl_cffi fastembed Pillow; fi

COPY app ./app
COPY frontend ./frontend
COPY config.yaml ./config.yaml

RUN useradd --create-home --user-group modelseek \
 && mkdir -p /app/data \
 && chown -R modelseek:modelseek /app/data

ENV PIP_USER=1 \
    PYTHONUSERBASE=/app/data/python

USER modelseek
VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/sources', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
