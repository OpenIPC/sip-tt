#!/usr/bin/env python3
"""Minimal STUN binding responder (RFC 5389). Exists so a softphone on this
host advertises a media address the camera can actually reach: liblinphone
picks the default-route interface otherwise, which is the public one."""
import socket, struct, sys

MAGIC = 0x2112A442
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(('0.0.0.0', 3478))
print('stun on 0.0.0.0:3478', flush=True)
while True:
    data, peer = sock.recvfrom(2048)
    if len(data) < 20:
        continue
    mtype, mlen, magic = struct.unpack('!HHI', data[:8])
    tid = data[8:20]
    if magic != MAGIC or (mtype & 0x3EEF) != 0x0001:
        continue
    ip = struct.unpack('!I', socket.inet_aton(peer[0]))[0] ^ MAGIC
    port = peer[1] ^ (MAGIC >> 16)
    # XOR-MAPPED-ADDRESS (0x0020), IPv4
    attr = struct.pack('!HHBBH I', 0x0020, 8, 0, 1, port, ip)
    resp = struct.pack('!HHI', 0x0101, len(attr), MAGIC) + tid + attr
    sock.sendto(resp, peer)
    print('bind %s:%d' % peer, flush=True)
