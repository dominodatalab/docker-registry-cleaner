FROM python:alpine3.22

COPY requirements.txt .
COPY config.yaml config.yaml
COPY python python

RUN pip install -r requirements.txt

ENTRYPOINT ["sleep", "3600"]