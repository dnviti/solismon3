FROM python:3-alpine

ARG BUILD_VERSION=latest
ARG BUILD_ARCH="aarch64|amd64"

EXPOSE 18000

LABEL maintainer="dnviti" \
  org.opencontainers.image.source="https://github.com/dnviti/solismon3" \
  org.opencontainers.image.description="Solis inverter monitor for MQTT, Prometheus, and Home Assistant" \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

ENV PYTHONUNBUFFERED=1

WORKDIR /solismon3

COPY requirements.txt ./
RUN pip install --upgrade pip \
  && pip3 install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY config ./config

CMD [ "python", "./main.py" ]
