FROM python:3.13-slim

# 1: curl_cffi, fastembed and Pillow are part of the image; 0: smaller image without them.
# Playwright is left out: browser mode needs a visible window, which a container lacks.
ARG EXTRAS=1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    MODELSEEK_CONFIG=/app/config.yaml \
    MODELSEEK_IN_DOCKER=1 \
    MODELSEEK_PORT=8000 \
    MODELSEEK_PUBLIC_HOST=0.0.0.0 \
    MODELSEEK_PUBLIC_PORT=8001

WORKDIR /app

COPY requirements.txt requirements-extra.txt ./
RUN pip install -r requirements.txt \
 && if [ "$EXTRAS" = "1" ]; then grep -iv "^playwright" requirements-extra.txt > /tmp/extras.txt \
      && pip install -r /tmp/extras.txt && rm /tmp/extras.txt; fi

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
# 8000: admin profile (publish on 127.0.0.1 only); 8001: public profile, when public.enabled
EXPOSE 8000 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/sources', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "app.serve"]
