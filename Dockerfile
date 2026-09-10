FROM python:3.14-alpine

RUN addgroup -S forwarder \
    && adduser -S -G forwarder -h /app forwarder \
    && mkdir -p /data \
    && chown forwarder:forwarder /data

WORKDIR /app
COPY --chown=forwarder:forwarder forwarder.py healthcheck.py preflight.py ./

USER forwarder
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

ENTRYPOINT ["python", "/app/forwarder.py"]
