from PIL import Image, ImageDraw
import os

OUT = "web/icons"
os.makedirs(OUT, exist_ok=True)

def make(size, maskable=False):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 圆角渐变底：橙 -> 粉 -> 紫
    pad = 0 if maskable else int(size * 0.06)
    radius = int(size * 0.22)
    grad = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / max(1, size - 1)
        if t < 0.55:
            k = t / 0.55
            c = (int(255 - 12 * k), int(154 - 62 * k), int(98 + 40 * k), 255)
        else:
            k = (t - 0.55) / 0.45
            c = (int(243 - 119 * k), int(92 - 0 * k), int(138 + 117 * k), 255)
        gd.line([(0, y), (size, y)], fill=c)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([pad, pad, size - pad, size - pad], radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)
    # 白色山形 + 圆点（和页面 logo 一致）
    s = size
    width = max(3, int(s * 0.055))
    pts = [(s*0.20, s*0.66), (s*0.38, s*0.40), (s*0.52, s*0.57), (s*0.68, s*0.30), (s*0.80, s*0.44)]
    d.line(pts, fill=(255, 255, 255, 255), width=width, joint="curve")
    r = s * 0.045
    cx, cy = s*0.68, s*0.30
    d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=(255, 255, 255, 255))
    return img

make(192).save(f"{OUT}/icon-192.png")
make(512).save(f"{OUT}/icon-512.png")
make(512, maskable=True).save(f"{OUT}/maskable-512.png")
make(180).save(f"{OUT}/apple-touch-icon.png")
print("icons:", sorted(os.listdir(OUT)))
