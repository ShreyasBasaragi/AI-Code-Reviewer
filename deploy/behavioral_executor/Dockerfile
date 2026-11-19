# Build only on the dedicated Linux executor host. Pin this base image by digest.
ARG PYTHON_BASE_IMAGE
FROM ${PYTHON_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

WORKDIR /opt
COPY requirements.txt /tmp/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git --version \
    && python -m pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt
COPY . /opt/baseline
COPY deploy/behavioral_executor/runner.py /opt/runner/runner.py
COPY deploy/behavioral_executor/pytest_launcher.py /opt/runner/pytest_launcher.py
RUN chmod -R a-w /opt/baseline /opt/runner

# The broker passes --user=65532:65532 and a read-only root filesystem.
