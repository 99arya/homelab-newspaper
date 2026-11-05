FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir weasyprint pypdfium2 segno
WORKDIR /app
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python3", "newsbot.py"]
