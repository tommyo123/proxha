FROM python:3.14-alpine
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="ProxHA" \
      org.opencontainers.image.description="Proxmox cluster management and distributed resource scheduling" \
      org.opencontainers.image.source="https://github.com/tommyo123/proxha" \
      org.opencontainers.image.url="https://github.com/tommyo123/proxha" \
      org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.revision=$REVISION
WORKDIR /app
COPY requirements.txt .
RUN apk add --no-cache openssh-client ca-certificates \
    && pip install --no-cache-dir -r requirements.txt \
    && addgroup -g 10001 proxha && adduser -D -u 10001 -G proxha proxha
COPY app/*.py ./app/
COPY static/*.js static/*.css static/*.html ./static/
COPY static/icons/*.svg static/icons/LICENSE ./static/icons/
RUN mkdir /config && chown proxha:proxha /config && chmod 700 /config
USER proxha
ENV PROXHA_CONFIG_DIR=/config PROXHA_LISTEN_HOST=0.0.0.0 PROXHA_CONTAINER=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
VOLUME ["/config"]
EXPOSE 8080 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "app.healthcheck"]
CMD ["python", "-m", "app.server"]
