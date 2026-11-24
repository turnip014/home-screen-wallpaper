"""Builds an EDID override that adds a half-refresh mode (e.g. 120 Hz next to 240 Hz) at the monitor's
native resolution, for monitors that only offer their maximum and 60 Hz. Used by edid_120hz.ps1.

  python make_edid_halfhz.py <EDID.bin> <out.bin>

The new mode is the monitor's own fastest native timing with half the pixel clock, so every other
timing value stays as the manufacturer set it. Supports DisplayID 1.x type I timings (where most
high-refresh monitors put their native modes); other layouts are refused rather than guessed.
"""
import sys


def timings(block):
    """(offset, pixel clock units, h active, h blank, v active, v blank) for DisplayID type I timings."""
    out = []
    if block[0] != 0x70:
        return out
    i, end = 5, 5 + block[2]
    while i < end and i + 3 <= 127:
        tag, ln = block[i], block[i + 2]
        if tag == 0x03:
            for j in range(i + 3, i + 3 + ln, 20):
                t = block[j:j + 20]
                pc = int.from_bytes(t[0:3], "little") + 1
                ha, hb = int.from_bytes(t[4:6], "little") + 1, int.from_bytes(t[6:8], "little") + 1
                va, vb = int.from_bytes(t[12:14], "little") + 1, int.from_bytes(t[14:16], "little") + 1
                out.append((j, pc, ha, hb, va, vb))
        if tag == 0 and ln == 0:
            break
        i += 3 + ln
    return out


def hz(t):
    return t[1] * 1e4 / ((t[2] + t[3]) * (t[4] + t[5]))


def main(src, dst):
    e = bytearray(open(src, "rb").read())
    for n in range(1, len(e) // 128):
        b = bytearray(e[n * 128:(n + 1) * 128])
        ts = timings(b)
        if not ts:
            continue
        native = max(ts, key=lambda t: (t[2] * t[4], hz(t)))
        if any(abs(hz(t) - hz(native) / 2) < 1 and t[2:5:2] == native[2:5:2] for t in ts):
            print(f"Already has {hz(native) / 2:.0f} Hz at {native[2]}x{native[4]}.")
            return 1
        tag_at = 5
        if b[tag_at] != 0x03 or (native[0] - tag_at - 3) // 20 >= b[tag_at + 2] // 20:
            continue
        payload = bytearray(b[5:126])
        if payload[-20:] != bytes(20):
            print("No room left in that EDID block.")
            return 1
        new = bytearray(b[native[0]:native[0] + 20])
        new[0:3] = (native[1] // 2 - 1).to_bytes(3, "little")
        new[3] &= 0x7F  # not the "preferred" mode
        at = 3 + payload[2]
        payload = payload[:at] + new + payload[at:-20]
        payload[2] += 20
        b[5:126] = payload
        b[126] = (-sum(b[1:126])) & 0xFF   # DisplayID section checksum
        b[127] = (-sum(b[0:127])) & 0xFF   # EDID block checksum
        e[n * 128:(n + 1) * 128] = b
        open(dst, "wb").write(e)
        print(f"Added {native[2]}x{native[4]} @ {hz(native) / 2:.2f} Hz (from {hz(native):.0f} Hz).")
        return 0
    print("No DisplayID type I native timing found; use CRU instead.")
    return 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
