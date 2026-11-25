FROM python:3.11-slim
WORKDIR /opt/pulse
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PULSE_DB=/data/pulse.db
COPY requirements.txt .
RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn==0.32.1 pyyaml==6.0.2 opentelemetry-proto==1.29.0
COPY apm ./apm
COPY config ./config
RUN useradd -r -u 10001 pulse && mkdir -p /data && chown pulse /data
USER pulse
VOLUME ["/data"]
EXPOSE 4400
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:4400/api/health',timeout=4)"
CMD ["python", "-m", "apm.server"]
