#!/usr/bin/env python3
"""Wait until the device under test answers an OPTIONS ping.

A device that is not listening yet produces a wall of failures that all mean
"we were early", so prove it answers before blaming it for anything.

The ping carries `;rport`. Without it a correct UA sends its response to the
address and port in the Via's sent-by, not back to the source — so a ping from
an ephemeral port gets a reply it never sees, and a perfectly healthy device
looks dead. That mistake cost twenty minutes of blaming baresip.
"""

import socket
import sys
import time

host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 5080
tries = int(sys.argv[3]) if len(sys.argv) > 3 else 30
bind_port = int(sys.argv[4]) if len(sys.argv) > 4 else 5099

for attempt in range(1, tries + 1):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", bind_port))
        s.settimeout(2)
        s.sendto(
            f"OPTIONS sip:dut@{host} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP 127.0.0.1:{bind_port};branch=z9hG4bKping{attempt};rport\r\n"
            f"From: <sip:ci@127.0.0.1>;tag=ping\r\n"
            f"To: <sip:dut@{host}>\r\n"
            f"Call-ID: ci-ping-{attempt}\r\nCSeq: {attempt} OPTIONS\r\n"
            f"Content-Length: 0\r\n\r\n".encode(), (host, port))
        data, _ = s.recvfrom(4096)
        if data.startswith(b"SIP/2.0"):
            print(f"DUT answered: {data.splitlines()[0].decode()}")
            sys.exit(0)
    except (socket.timeout, OSError):
        pass
    finally:
        s.close()
    time.sleep(1)

print(f"no SIP response from {host}:{port} after {tries} attempts")
sys.exit(1)
