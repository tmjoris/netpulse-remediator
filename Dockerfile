# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY netpulse ./netpulse
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NETPULSE_CONFIG=/etc/netpulse/netpulse.toml \
    NETPULSE_AUDIT_PATH=/var/lib/netpulse/netpulse.audit.jsonl
RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin netpulse \
    && mkdir -p /var/lib/netpulse /etc/netpulse \
    && chown netpulse /var/lib/netpulse
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
COPY config/netpulse.example.toml /etc/netpulse/netpulse.toml
USER netpulse
VOLUME ["/var/lib/netpulse"]
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2).status != 200)"]
CMD ["netpulse", "serve", "--host", "0.0.0.0", "--port", "8000", "--log-format", "json"]
