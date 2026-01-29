FROM python:alpine3.23

RUN apk add --no-cache aws-cli skopeo

COPY requirements.txt .
COPY python python

RUN pip install -r requirements.txt

WORKDIR /python

ENTRYPOINT ["/bin/sh", "-c", "sleep 3600"]