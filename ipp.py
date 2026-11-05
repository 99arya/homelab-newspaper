"""Tiny driverless printing client (IPP Everywhere / AirPrint), standard library only.

Lets a machine (or container) without CUPS send a document straight to a network printer.
    python3 ipp.py info  ipp://192.168.1.50/ipp/print
    python3 ipp.py print ipp://192.168.1.50/ipp/print file.pdf
"""
import ssl
import struct
import sys
import urllib.request

OP_PRINT_JOB, OP_GET_PRINTER_ATTRS = 0x0002, 0x000B
TAG_OP, TAG_JOB, TAG_END = 0x01, 0x02, 0x03
T_INT, T_BOOL, T_ENUM = 0x21, 0x22, 0x23
T_TEXT, T_NAME, T_KEYWORD, T_URI, T_CHARSET, T_LANG, T_MIME = 0x41, 0x42, 0x44, 0x45, 0x47, 0x48, 0x49


def _attr(tag, name, value):
    v = value.encode() if isinstance(value, str) else value
    n = name.encode()
    return struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(v)) + v


def _request(op, uri, op_attrs=(), job_attrs=(), data=b""):
    body = struct.pack(">BBHI", 2, 0, op, 1) + bytes([TAG_OP])
    body += _attr(T_CHARSET, "attributes-charset", "utf-8")
    body += _attr(T_LANG, "attributes-natural-language", "en")
    body += _attr(T_URI, "printer-uri", uri)
    for a in op_attrs:
        body += _attr(*a)
    if job_attrs:
        body += bytes([TAG_JOB]) + b"".join(_attr(*a) for a in job_attrs)
    body += bytes([TAG_END]) + data
    http = uri.replace("ipps://", "https://", 1).replace("ipp://", "http://", 1)
    if ":" not in http.split("//", 1)[1].split("/", 1)[0]:  # default IPP port
        scheme, rest = http.split("//", 1)
        host, path = rest.split("/", 1)
        http = f"{scheme}//{host}:631/{path}"
    req = urllib.request.Request(http, data=body, headers={"Content-Type": "application/ipp"})
    ctx = ssl._create_unverified_context()  # printers use self-signed certs
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        return _parse(r.read())


def _parse(buf):
    status = struct.unpack(">H", buf[2:4])[0]
    attrs, i, last = {}, 8, None
    while i < len(buf):
        tag = buf[i]; i += 1
        if tag == TAG_END:
            break
        if tag < 0x10:  # group delimiter
            continue
        nlen = struct.unpack(">H", buf[i:i + 2])[0]; i += 2
        name = buf[i:i + nlen].decode(errors="replace"); i += nlen
        vlen = struct.unpack(">H", buf[i:i + 2])[0]; i += 2
        raw = buf[i:i + vlen]; i += vlen
        if tag in (T_INT, T_ENUM) and vlen == 4:
            val = struct.unpack(">i", raw)[0]
        elif tag == 0x32 and vlen == 9:  # resolution: keep the horizontal dots per inch
            val = struct.unpack(">iib", raw)[0]
        elif tag == T_BOOL:
            val = bool(raw[0])
        elif 0x40 <= tag <= 0x4F:
            val = raw.decode(errors="replace")
        else:
            val = raw
        if name:
            last = name
            attrs.setdefault(name, []).append(val)
        elif last:  # additional value of a multi-valued attribute
            attrs[last].append(val)
    return status, attrs


def printer_info(uri):
    status, attrs = _request(OP_GET_PRINTER_ATTRS, uri, op_attrs=[(T_KEYWORD, "requested-attributes", "all")])
    if status >= 0x0400:
        raise RuntimeError(f"printer refused request (status 0x{status:04x})")
    return attrs


def print_pdf(uri, pdf_path, job_name="Newspaper", two_sided=True):
    return print_document(uri, open(pdf_path, "rb").read(), "application/pdf", job_name, two_sided)


def print_document(uri, data, doc_format, job_name="Newspaper", two_sided=True):
    job = []
    if two_sided:
        job.append((T_KEYWORD, "sides", "two-sided-long-edge"))
    status, attrs = _request(
        OP_PRINT_JOB, uri,
        op_attrs=[(T_NAME, "requesting-user-name", "newsbot"), (T_NAME, "job-name", job_name),
                  (T_MIME, "document-format", doc_format)],
        job_attrs=job, data=data)
    if status >= 0x0400:
        raise RuntimeError(f"print job rejected (status 0x{status:04x}): {attrs.get('status-message', [''])[0]}")
    return attrs.get("job-id", ["?"])[0]


if __name__ == "__main__":
    cmd, uri = sys.argv[1], sys.argv[2]
    if cmd == "info":
        a = printer_info(uri)
        for k in ("printer-make-and-model", "printer-state", "document-format-supported",
                  "sides-supported", "media-default", "printer-state-reasons"):
            print(f"{k}: {a.get(k)}")
    elif cmd == "print":
        print("job id:", print_pdf(uri, sys.argv[3]))
