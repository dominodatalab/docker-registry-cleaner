FROM python:alpine3.22

RUN apk add skopeo

COPY requirements.txt .
COPY config-example.yaml config.yaml
COPY python python

RUN pip install -r requirements.txt

ENTRYPOINT ["/bin/sh", "-c", "sleep 3600"]