FROM python:alpine3.22

COPY requirements.txt .
COPY scripts scripts

RUN pip install -r requirements.txt

ENTRYPOINT ["sleep", "3600"]