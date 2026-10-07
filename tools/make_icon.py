"""
Draws the app icon (assets/icon.png and assets/icon.ico).
Run again after changing it:  python tools/make_icon.py
"""
import os

from PIL import Image, ImageDraw, ImageFilter

S = 1024  # draw big, downscale for crisp edges
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ASSETS = os.path.join(ROOT, "assets")


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def make() -> Image.Image:
    # diagonal gradient: pink accent → purple
    top, bottom = (233, 69, 96), (91, 52, 160)
    grad = Image.new("RGB", (S, S))
    px = grad.load()
    for y in range(S):
        for x in range(S):
            px[x, y] = lerp(top, bottom, (x * 0.35 + y * 0.65) / S)

    # rounded square mask
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((40, 40, S - 40, S - 40), radius=220, fill=255)
    icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    icon.paste(grad, (0, 0), mask)

    # soft shadow under the note
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    white = (255, 255, 255, 255)

    def note(d, dx, dy, color):
        # two beamed eighth notes
        d.ellipse((250 + dx, 520 + dy, 430 + dx, 660 + dy), fill=color)   # left head
        d.ellipse((560 + dx, 460 + dy, 740 + dx, 600 + dy), fill=color)   # right head
        d.rectangle((388 + dx, 230 + dy, 430 + dx, 600 + dy), fill=color)  # left stem
        d.rectangle((698 + dx, 170 + dy, 740 + dx, 540 + dy), fill=color)  # right stem
        d.polygon([(388 + dx, 230 + dy), (740 + dx, 170 + dy),
                   (740 + dx, 260 + dy), (388 + dx, 320 + dy)], fill=color)  # beam

    note(sd, 0, 18, (0, 0, 0, 90))
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    icon = Image.alpha_composite(icon, shadow)
    d = ImageDraw.Draw(icon)
    note(d, 0, 0, white)

    # "subtitle" bars under the note
    d.rounded_rectangle((210, 750, 814, 800), radius=25, fill=white)
    d.rounded_rectangle((300, 840, 724, 890), radius=25, fill=(255, 255, 255, 190))
    return icon


def main():
    os.makedirs(ASSETS, exist_ok=True)
    icon = make()
    icon.resize((256, 256), Image.LANCZOS).save(os.path.join(ASSETS, "icon.png"))
    icon.save(os.path.join(ASSETS, "icon.ico"),
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("assets/icon.png e assets/icon.ico gerados")


if __name__ == "__main__":
    main()
