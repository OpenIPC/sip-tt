FROM ubuntu:18.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq build-essential wget ca-certificates \
      libedit-dev libjansson-dev libxml2-dev libsqlite3-dev uuid-dev libssl-dev \
      libncurses5-dev pkg-config tcpdump iproute2 >/dev/null
WORKDIR /usr/src
RUN wget -q https://downloads.asterisk.org/pub/telephony/asterisk/old-releases/asterisk-13.19.1.tar.gz \
 && tar xzf asterisk-13.19.1.tar.gz
WORKDIR /usr/src/asterisk-13.19.1
RUN ./configure --with-jansson-bundled --disable-xmldoc >/tmp/conf.log 2>&1 || (tail -30 /tmp/conf.log; false)
RUN make menuselect.makeopts >/dev/null 2>&1 \
 && menuselect/menuselect --enable chan_sip --enable res_rtp_asterisk --disable BUILD_NATIVE menuselect.makeopts
RUN make -j"$(nproc)" >/tmp/build.log 2>&1 || (tail -40 /tmp/build.log; false)
RUN make install >/dev/null 2>&1 && make samples >/dev/null 2>&1
CMD ["asterisk", "-f", "-vvv"]
