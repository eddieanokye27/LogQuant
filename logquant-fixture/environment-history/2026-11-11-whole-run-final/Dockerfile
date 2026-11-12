FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
WORKDIR /work
COPY requirements.txt requirements.lock .
RUN pip install --no-cache-dir --no-deps -r requirements.lock && pip check
COPY fixture fixture
COPY tests tests
COPY scripts scripts
COPY batch.example.json Dockerfile compose.yaml .
ENV PYTHONUNBUFFERED=1
