# .devcontainer/Dockerfile
FROM golang:1.24-bookworm

# Python 3.11 is default on Bookworm
RUN apt-get update && apt-get install -y --no-install-recommends  python3 python3-venv python3-pip git ca-certificates curl python-is-python3 vim && rm -rf /var/lib/apt/lists/*

# Create non-root user like the devcontainers images
ARG USER=vscode UID=1000 GID=1000
RUN groupadd -g ${GID} ${USER} \
 && useradd -s /bin/bash -m -u ${UID} -g ${GID} ${USER}
USER ${USER}
ENV HOME=/home/${USER} PATH=/home/${USER}/.local/bin:$PATH

# Optional: preinstall uv to speed up first use
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
