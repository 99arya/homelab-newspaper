"""Find network printers (IPP / AirPrint) with a tiny mDNS query. Standard library only.

    python3 discover.py        # lists printers on your network
"""
import socket
import struct
import time

MDNS = ("224.0.0.251", 5353)
SERVICES = ("_ipp._tcp.local", "_ipps._tcp.local")


def _name(buf, i):
    """Read a (possibly compressed) DNS name; returns (name, next index)."""
    labels, end, jumps = [], None, 0
    while True:
        n = buf[i]
        if n == 0:
            i += 1
            break
        if n & 0xC0 == 0xC0:  # pointer
            if end is None:
                end = i + 2
            i = ((n & 0x3F) << 8) | buf[i + 1]
            jumps += 1
            if jumps > 20:
                break
            continue
        labels.append(buf[i + 1:i + 1 + n].decode(errors="replace"))
        i += 1 + n
    return ".".join(labels), (end if end is not None else i)


def _query(name):
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    return struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + q + struct.pack(">HH", 12, 1)  # PTR, IN


def _records(buf):
    _, _, qd, an, ns, ar = struct.unpack(">HHHHHH", buf[:12])
    i = 12
    for _ in range(qd):
        _, i = _name(buf, i)
        i += 4
    for _ in range(an + ns + ar):
        name, i = _name(buf, i)
        rtype, _, _, rdlen = struct.unpack(">HHIH", buf[i:i + 10])
        i += 10
        data, start = buf[i:i + rdlen], i
        i += rdlen
        if rtype == 12:  # PTR
            yield name, "PTR", _name(buf, start)[0]
        elif rtype == 33:  # SRV
            port = struct.unpack(">H", data[4:6])[0]
            yield name, "SRV", (_name(buf, start + 6)[0], port)
        elif rtype == 16:  # TXT
            txt, j = {}, 0
            while j < len(data):
                ln = data[j]
                k, _, v = data[j + 1:j + 1 + ln].decode(errors="replace").partition("=")
                txt[k.lower()] = v
                j += 1 + ln
            yield name, "TXT", txt
        elif rtype == 1 and rdlen == 4:  # A
            yield name, "A", socket.inet_ntoa(data)


def find_printers(timeout=3.0):
    """Returns [{"name", "model", "uri", "host", "ip"}] for printers that answer."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    s.settimeout(0.3)
    for svc in SERVICES:
        s.sendto(_query(svc), MDNS)
    ptr, srv, txt, addr = {}, {}, {}, {}
    end = time.time() + timeout
    while time.time() < end:
        try:
            buf, _ = s.recvfrom(9000)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            for name, kind, val in _records(buf):
                if kind == "PTR" and name in SERVICES:
                    ptr[val] = name
                elif kind == "SRV":
                    srv[name] = val
                elif kind == "TXT":
                    txt[name] = val
                elif kind == "A":
                    addr[name] = val
        except (IndexError, struct.error):
            continue
    s.close()
    found = {}
    for inst in ptr:
        if inst not in srv:
            continue
        host, port = srv[inst]
        t = txt.get(inst, {})
        rp = t.get("rp", "ipp/print")
        label = inst.split("._ipp")[0].replace("\\032", " ")
        key = host  # the same printer often answers for both ipp and ipps
        found.setdefault(key, {"name": label, "model": t.get("ty", ""), "host": host,
                               "ip": addr.get(host, ""),
                               "uri": f"ipp://{host}{'' if port == 631 else ':%d' % port}/{rp}"})
    return list(found.values())


if __name__ == "__main__":
    printers = find_printers()
    if not printers:
        print("No printers answered. Is it on, and on the same network?")
    for p in printers:
        print(f"{p['name']}  ({p['model']})\n    {p['uri']}  {p['ip']}")
