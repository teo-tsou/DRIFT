FROM python:3.12-slim

WORKDIR /app

ENV PYTHONPATH="/app:/app/cluster_api:${PYTHONPATH}"

# System deps: kubectl via binary, ansible installed via pip
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    postgresql-client \
    sshpass \
    openssh-client \
    git \
    curl \
    && curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/$(dpkg --print-architecture)/kubectl" \
    && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
    && rm kubectl \
    && rm -rf /var/lib/apt/lists/*

# Copy root requirements.txt (contains ansible + all backend deps)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ansible collections needed by playbooks
RUN ansible-galaxy collection install kubernetes.core community.general ansible.posix

# Copy backend application code
COPY backend/ .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
