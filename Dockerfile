FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        fonts-dejavu-core \
        intel-gpu-tools \
        intel-media-va-driver \
        libva-drm2 \
        python3 \
        python3-yaml \
        tini \
        vainfo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY src/ /app/src/

ENV CAMERA_WALL_CONFIG=/config/config.yaml \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

EXPOSE 8088

ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "camera_wall"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python3", "-m", "camera_wall.healthcheck"]
