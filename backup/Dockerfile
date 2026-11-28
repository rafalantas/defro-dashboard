FROM python:3.12-slim-bookworm

ARG RESTIC_VERSION=0.16.4
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl bzip2 \
    && rm -rf /var/lib/apt/lists/* \
    && case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
         amd64) RESTIC_ARCH=amd64 ;; \
         arm64) RESTIC_ARCH=arm64 ;; \
         *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_${RESTIC_ARCH}.bz2" -o /tmp/restic.bz2 \
    && bunzip2 /tmp/restic.bz2 \
    && install -m 0755 /tmp/restic /usr/local/bin/restic \
    && rm -f /tmp/restic \
    && restic version

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY backup.py /app/backup.py
ENTRYPOINT ["python", "/app/backup.py"]
