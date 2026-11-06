"""Generate the full set of MSIX tile / logo assets for Midum from gui/assets/icon.ico.

Partner Center rejects packages whose tile icons include a default / placeholder image. Every
tile size and scale below is rendered from Midum's own icon, so each one uniquely represents
the product.

Usage (from the midum_pkg folder):
    pip install pillow
    python packaging/make_msix_assets.py

Output: packaging/msix_assets/*.png (and a copy in dist/MidumAppxPackage/assets if that exists).
"""
import os
import shutil

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ICON = os.path.join(ROOT, "gui", "assets", "icon.ico")
OUT = os.path.join(HERE, "msix_assets")
PKG_ASSETS = os.path.join(ROOT, "dist", "MidumAppxPackage", "assets")

BG = (245, 245, 243, 255)  # same off-white as the icon's own background


def load_source() -> Image.Image:
    im = Image.open(ICON)
    sizes = im.info.get("sizes") or {im.size}
    im.size = max(sizes)
    im.load()
    return im.convert("RGBA")


def square(src: Image.Image, px: int) -> Image.Image:
    return src.resize((px, px), Image.LANCZOS)


def wide(src: Image.Image, w: int, h: int) -> Image.Image:
    canvas = Image.new("RGBA", (w, h), BG)
    side = int(h * 0.9)
    logo = square(src, side)
    canvas.alpha_composite(logo, ((w - side) // 2, (h - side) // 2))
    return canvas


def save(img: Image.Image, name: str) -> None:
    img.save(os.path.join(OUT, name), "PNG")


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    src = load_source()
    scales = (100, 125, 150, 200, 400)

    # Square44x44Logo: scale variants + unplated target sizes (used by taskbar / Start / search)
    for s in scales:
        save(square(src, round(44 * s / 100)), f"Square44x44Logo.scale-{s}.png")
    for t in (16, 24, 32, 48, 256):
        save(square(src, t), f"Square44x44Logo.targetsize-{t}.png")
        save(square(src, t), f"Square44x44Logo.targetsize-{t}_altform-unplated.png")
        save(square(src, t), f"Square44x44Logo.altform-lightunplated_targetsize-{t}.png")

    # Square71x71, Square150x150, Square310x310 tiles
    for base, name in ((71, "SmallTile"), (150, "Square150x150Logo"), (310, "LargeTile")):
        for s in scales:
            save(square(src, round(base * s / 100)), f"{name}.scale-{s}.png")

    # Wide310x150
    for s in scales:
        save(wide(src, round(310 * s / 100), round(150 * s / 100)), f"Wide310x150Logo.scale-{s}.png")

    # StoreLogo (50x50)
    for s in scales:
        save(square(src, round(50 * s / 100)), f"StoreLogo.scale-{s}.png")

    # Splash screen (620x300)
    for s in (100, 125, 150, 200, 400):
        save(wide(src, round(620 * s / 100), round(300 * s / 100)), f"SplashScreen.scale-{s}.png")

    # Unscaled names referenced by the manifest
    save(square(src, 44), "logo44.png")
    save(square(src, 150), "logo150.png")
    save(square(src, 71), "logo71.png")
    save(square(src, 310), "logo310.png")
    save(wide(src, 310, 150), "wide310x150.png")
    save(square(src, 50), "store_logo.png")

    print(f"Wrote {len(os.listdir(OUT))} assets to {OUT}")

    if os.path.isdir(os.path.dirname(PKG_ASSETS)):
        os.makedirs(PKG_ASSETS, exist_ok=True)
        for fn in os.listdir(OUT):
            shutil.copy2(os.path.join(OUT, fn), os.path.join(PKG_ASSETS, fn))
        print(f"Copied into {PKG_ASSETS}")


if __name__ == "__main__":
    main()
