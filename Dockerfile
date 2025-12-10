FROM python:alpine3.23

RUN apk add --no-cache aws-cli skopeo

COPY requirements.txt .
COPY config-example.yaml config.yaml
COPY python python

RUN pip install -r requirements.txt

ENTRYPOINT ["/bin/sh", "-c", "sleep 3600"]