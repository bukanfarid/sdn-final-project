FROM python:3.9-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    iproute2 iputils-ping tcpdump bash \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "setuptools==67.6.1" wheel \
 && pip install --no-cache-dir --no-build-isolation ryu==4.34 eventlet==0.30.2
WORKDIR /app
CMD ["bash"]
