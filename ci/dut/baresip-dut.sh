#!/bin/sh
# Start baresip as a device under test.
#
# A real SIP user agent makes a far better CI target than a mock: it
# negotiates properly, sends actual RTP, and disagrees with us occasionally in
# ways worth knowing about. This is the analogue of onvif-tt's
# onvif_simple_server job.
#
# baresip rather than linphonec, for one reason that cost an afternoon:
# liblinphone answers **503 Service unavailable** to every inbound INVITE when
# its core has not reached the On state, and in a container it does not — the
# log line is "Linphone core global state is not on", buried under a wall of
# ALSA errors that look like the cause and are not. From the outside it is
# indistinguishable from a device that refuses calls, which is precisely the
# kind of false verdict this tool exists to avoid. baresip is built for
# headless operation, ships a synthetic audio source (ausine) and a null sink
# (aubridge), and needs no display, no sound card and no X server.
set -eu

DIR=${DIR:-/root/.baresip}
FIFO=${FIFO:-/tmp/baresip.fifo}
LOG=${LOG:-/tmp/baresip.log}
SIP_PORT=${SIP_PORT:-5080}
USER_NAME=${USER_NAME:-dut}
PASSWORD=${PASSWORD:-camera123}
REGISTRAR=${REGISTRAR:-}      # host:port; empty means do not register

mkdir -p "$DIR"

# net_interface pins the address baresip binds and advertises. Without it,
# `sip_listen 0.0.0.0:port` still ends up bound to the default route's address
# rather than to everything, so a test aimed at 127.0.0.1 gets no answer at
# all and reads as a device that refuses calls.
#
# It is given as an *address* and not as the name "lo". baresip 1.0.0 resolves
# a name through its own interface walk, which on a GitHub runner reports
# "net: lo: could not get IPv4 address (No such device)" and then fails to
# start at all — while the same name works under Docker with --network host.
# An address needs no lookup and behaves the same everywhere.
cat > "$DIR/config" <<CONF
net_interface           ${IFACE:-127.0.0.1}
sip_listen              ${BIND:-127.0.0.1}:$SIP_PORT
audio_player            aubridge,dut
audio_source            aubridge,dut
audio_alert             aubridge,dut
rtp_ports               10000-10100
module_path             /usr/lib/baresip/modules
module                  stdio.so
module                  ausine.so
module                  aubridge.so
module                  g711.so
module_app              account.so
module_app              contact.so
module_app              menu.so
CONF

# answermode=auto makes it a terminating endpoint without anyone at a
# keyboard. regint=0 keeps it off a registrar unless one was named.
if [ -n "$REGISTRAR" ]; then
  echo "<sip:$USER_NAME@$REGISTRAR>;auth_pass=$PASSWORD;answermode=auto;regint=60;audio_codecs=pcma,pcmu" > "$DIR/accounts"
else
  echo "<sip:$USER_NAME@127.0.0.1:$SIP_PORT>;answermode=auto;regint=0;audio_codecs=pcma,pcmu" > "$DIR/accounts"
fi

rm -f "$FIFO"
mkfifo "$FIFO"
# Hold the FIFO open, or baresip sees EOF as soon as the first writer closes.
sleep infinity > "$FIFO" &

baresip -f "$DIR" < "$FIFO" > "$LOG" 2>&1 &
echo $! > /tmp/baresip.pid

for _ in $(seq 1 30); do
  grep -q "baresip is ready" "$LOG" 2>/dev/null && break
  sleep 1
done
echo "baresip up on :$SIP_PORT (log $LOG, fifo $FIFO)"
