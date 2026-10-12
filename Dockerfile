FROM python:3.11-slim

WORKDIR /opt/vps-cluster-manager

# 安装底层系统依赖 (OpenSSH client、curl等)
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5001

ENV PORT=5001
ENV HOST=0.0.0.0

CMD ["python", "app.py"]
