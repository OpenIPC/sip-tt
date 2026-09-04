FROM ubuntu:24.04
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq baresip \
 && rm -rf /var/lib/apt/lists/*
ENV HOME=/root
COPY baresip-dut.sh /usr/local/bin/baresip-dut
