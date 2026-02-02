FROM cgr.dev/dominodatalab.com/python:3.14.2-dev AS dev
WORKDIR /app
# RUN apk add --no-cache aws-cli skopeo
RUN python -m venv venv
ENV PATH="/app/venv/bin":$PATH
COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt

FROM cgr.dev/dominodatalab.com/python:3.14.2
WORKDIR /app

COPY --from=dev /app/venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
COPY python python


ENTRYPOINT ["/bin/sh", "-c", "sleep 3600"]