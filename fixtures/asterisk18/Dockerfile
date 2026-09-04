FROM ubuntu:22.04
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
      asterisk tcpdump iproute2 >/dev/null 2>&1 && rm -rf /var/lib/apt/lists/*
