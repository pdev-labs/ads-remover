# Reproducible run: Python + a JRE, no host setup.
#   docker build -t ads-remover .
#   docker run --rm -v "$PWD:/data" ads-remover /data/app.apk -o /data/app-noads.apk -y
# The helper jars are downloaded into /app/tools on first run (and verified).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends default-jre-headless \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY remove_ads.py ./
COPY LICENSE README.md ./

# Unprivileged by default; /data is the mount point for APKs in/out.
RUN useradd -m -u 10001 user && chown -R user:user /app
USER user

ENTRYPOINT ["python", "/app/remove_ads.py"]
CMD ["--help"]
