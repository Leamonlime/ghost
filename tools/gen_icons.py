"""Generate Ghost PWA icons (192 + 512) with no image deps — pure stdlib PNG (zlib+struct).
Draws the established Ghost silhouette (the pink #eab8c0 ghost from the pages' favicon) on the
dark #150e14 ground, centred in the maskable safe zone. Supersampled 3x for clean edges."""
import math, struct, zlib, sys

GROUND = (0x15, 0x0e, 0x14)   # #150e14
GHOST  = (0xea, 0xb8, 0xc0)   # #eab8c0 (--ghost-title)

def inside_ghost(gx, gy):
    """Ghost mask in the favicon's 32-box coords. Dome centred (16,15) r10; body walls x6..26
    down to a wavy hem; two eyes knocked out."""
    # dome (upper semicircle)
    if gy <= 15.0:
        if (gx - 16.0) ** 2 + (gy - 15.0) ** 2 <= 10.0 ** 2:
            base_in = True
        else:
            base_in = False
    else:
        frac = (gx - 6.0) / 20.0
        bottom = 24.0 + 1.6 * math.cos(2 * math.pi * 4 * frac)   # wavy hem 22.4..25.6
        base_in = (6.0 <= gx <= 26.0) and (gy <= bottom)
    if not base_in:
        return False
    # eyes (knock out)
    if (gx - 12.4) ** 2 + (gy - 14.6) ** 2 <= 1.9 ** 2:
        return False
    if (gx - 19.6) ** 2 + (gy - 14.6) ** 2 <= 1.9 ** 2:
        return False
    return True

def render(N, ss=3):
    box = 0.60 * N                 # ghost bbox side (maskable safe zone ~60%)
    gscale = box / 20.6            # px per ghost-unit (ghost sub-box h ~20.6)
    gw, gh = 20.0 * gscale, 20.6 * gscale
    ox, oy = (N - gw) / 2.0, (N - gh) / 2.0
    M = N * ss
    row = bytearray()
    out = bytearray()
    inv = 1.0 / ss
    for py in range(N):
        out.append(0)              # PNG filter byte (none)
        for px in range(N):
            hit = 0
            for sy in range(ss):
                fy = py + (sy + 0.5) * inv
                gy = 5.0 + (fy - oy) / gscale
                for sx in range(ss):
                    fx = px + (sx + 0.5) * inv
                    gx = 6.0 + (fx - ox) / gscale
                    if inside_ghost(gx, gy):
                        hit += 1
            a = hit / (ss * ss)
            r = round(GROUND[0] + (GHOST[0] - GROUND[0]) * a)
            g = round(GROUND[1] + (GHOST[1] - GROUND[1]) * a)
            b = round(GROUND[2] + (GHOST[2] - GROUND[2]) * a)
            out += bytes((r, g, b))
    return bytes(out)

def write_png(path, N):
    raw = render(N)
    def chunk(typ, data):
        c = struct.pack(">I", len(data)) + typ + data
        return c + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff)
    ihdr = struct.pack(">IIBBBBB", N, N, 8, 2, 0, 0, 0)   # 8-bit RGB
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    print(f"wrote {path} ({N}x{N}, {len(png)} bytes)")

if __name__ == "__main__":
    dest = sys.argv[1].rstrip("/")
    write_png(f"{dest}/ghost-icon-192.png", 192)
    write_png(f"{dest}/ghost-icon-512.png", 512)
