# IF you change the base image, you need to rebuild all images (run with --force_rebuild)
# NOTE: The native Rust `uv` binary segfaults under QEMU (amd64 on arm64 hosts).
# We install a bash shim that maps `uv venv` → `python -m venv` and `uv pip` → `pip`.
# This is required for ARM64 hosts (Mac, EC2 Graviton) running amd64 Docker images.
_DOCKERFILE_BASE = r"""
FROM --platform={platform} ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

RUN apt update && apt install -y \
wget \
build-essential \
libffi-dev \
libtiff-dev \
python3 \
python3-pip \
python-is-python3 \
jq \
curl \
locales \
locales-all \
tzdata \
&& rm -rf /var/lib/apt/lists/*

# Install the latest version of Git
RUN apt-get update && apt-get install software-properties-common -y
RUN add-apt-repository ppa:git-core/ppa -y
RUN apt-get update && apt-get install git -y

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates python3-venv software-properties-common

RUN add-apt-repository ppa:deadsnakes/ppa -y && apt-get update && \
    apt-get install -y python3.10 python3.10-venv python3.10-dev python3.12 python3.12-venv python3.12-dev || true

RUN echo '#!/bin/bash' > /usr/local/bin/uv && \
    echo 'if [ "$1" = "venv" ]; then' >> /usr/local/bin/uv && \
    echo '  shift; pv=""; td=".venv"' >> /usr/local/bin/uv && \
    echo '  while [ $# -gt 0 ]; do' >> /usr/local/bin/uv && \
    echo '    case $1 in --python) pv="$2"; shift 2;; *) td="$1"; shift;; esac' >> /usr/local/bin/uv && \
    echo '  done' >> /usr/local/bin/uv && \
    echo '  if [ -n "$pv" ]; then "python$pv" -m venv "$td"; else python3 -m venv "$td"; fi' >> /usr/local/bin/uv && \
    echo 'elif [ "$1" = "pip" ]; then' >> /usr/local/bin/uv && \
    echo '  shift; pip "$@"' >> /usr/local/bin/uv && \
    echo 'else' >> /usr/local/bin/uv && \
    echo '  echo "uv shim: unsupported: $@" >&2; exit 1' >> /usr/local/bin/uv && \
    echo 'fi' >> /usr/local/bin/uv && \
    chmod +x /usr/local/bin/uv
"""

_DOCKERFILE_REPO = r"""FROM --platform={platform} commit0.base:latest

COPY ./setup.sh /root/
RUN chmod +x /root/setup.sh
RUN /bin/bash /root/setup.sh

WORKDIR /testbed/

# Automatically activate the testbed environment
RUN echo "source /testbed/.venv/bin/activate" > /root/.bashrc
"""


def get_dockerfile_base(platform: str) -> str:
    return _DOCKERFILE_BASE.format(platform=platform)


def get_dockerfile_repo(platform: str) -> str:
    return _DOCKERFILE_REPO.format(platform=platform)


__all__ = []
