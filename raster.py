"""Convert a PDF to PWG Raster (sgray_8), the format every IPP Everywhere printer accepts,
for printers that can't print PDF directly (many Brother lasers, for example).
Uses pypdfium2 to render pages."""
import re
import struct

import pypdfium2 as pdfium

_RUN = re.compile(rb"(.)\1*", re.S)


def _encode_line(row):
    out, lit = bytearray(), bytearray()

    def flush():
        if len(lit) == 1:
            out.append(0); out.extend(lit)
        elif lit:
            out.append(257 - len(lit)); out.extend(lit)
        lit.clear()

    for m in _RUN.finditer(row):
        n = m.end() - m.start()
        if n == 1:
            lit.extend(m.group())
            if len(lit) == 128:
                flush()
            continue
        flush()
        px = row[m.start()]
        while n:
            c = min(n, 128)
            out.append(c - 1); out.append(px)
            n -= c
    flush()
    return out


def _header(w, h, dpi, total, duplex):
    hd = bytearray(1796)
    hd[0:9] = b"PwgRaster"
    put = lambda off, val: struct.pack_into(">I", hd, off, val)
    put(272, 1 if duplex else 0)          # Duplex
    put(276, dpi); put(280, dpi)          # HWResolution
    put(340, 1)                           # NumCopies
    put(352, 612); put(356, 792)          # PageSize (points, US Letter)
    put(372, w); put(376, h)              # Width, Height
    put(384, 8); put(388, 8); put(392, w) # bits/color, bits/pixel, bytes/line
    put(400, 18)                          # ColorSpace = sGray
    put(420, 1)                           # NumColors
    put(452, total)                       # TotalPageCount
    put(456, 1); put(460, 1)              # Cross/FeedTransform
    put(472, w); put(476, h)              # ImageBox right/bottom
    put(480, 0x00FFFFFF)                  # AlternatePrimary
    hd[1732:1732 + 18] = b"na_letter_8.5x11in"
    return bytes(hd)


def pdf_to_pwg(pdf_path, dpi=600, duplex=True):
    pdf = pdfium.PdfDocument(str(pdf_path))
    total = len(pdf)
    out = bytearray(b"RaS2")
    for i in range(total):
        img = pdf[i].render(scale=dpi / 72, grayscale=True).to_pil().convert("L")
        w, h = img.size
        data = img.tobytes()
        out += _header(w, h, dpi, total, duplex and total > 1)
        y = 0
        while y < h:
            row = data[y * w:(y + 1) * w]
            rep = 0
            while y + rep + 1 < h and rep < 255 and data[(y + rep + 1) * w:(y + rep + 2) * w] == row:
                rep += 1
            out.append(rep)
            out += _encode_line(row)
            y += rep + 1
    return bytes(out), total
