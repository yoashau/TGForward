FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libmediainfo0v5 \
    && rm -rf /var/lib/apt/lists/*

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/yoashau/TGForward" \
      org.opencontainers.image.revision=${SOURCE_REVISION}

WORKDIR /app
COPY requirements/runtime.txt requirements/runtime.lock ./requirements/
RUN pip install --no-cache-dir -r requirements/runtime.txt -c requirements/runtime.lock
COPY tgforward ./tgforward

ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid ${APP_GID} bot \
    && useradd --uid ${APP_UID} --gid ${APP_GID} --create-home bot \
    && mkdir -p /app/data/state /app/data/thumbs \
    && chown -R bot:bot /app
USER bot

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -m tgforward.runtime.healthcheck
CMD ["python", "-m", "tgforward"]
