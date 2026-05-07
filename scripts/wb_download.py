"""Download all product images from Wildberries by article ID.

Usage:
    python wb_download.py <article_id> [--out FOLDER] [--keep-webp] [--no-jpg]
"""
import argparse
import os
import sys
import urllib.request
import urllib.error
from io import BytesIO

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    return urllib.request.urlopen(req, timeout=timeout)


def find_basket(nm, vol, part):
    for i in range(1, 32):
        host = f"basket-{i:02d}.wbbasket.ru"
        url = f"https://{host}/vol{vol}/part{part}/{nm}/images/big/1.webp"
        try:
            with fetch(url, timeout=8) as r:
                if r.status == 200:
                    return i
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                continue
        except Exception:
            continue
    return None


def main():
    ap = argparse.ArgumentParser(description="Download all images for a Wildberries article.")
    ap.add_argument("article", type=int, help="Wildberries article ID (nm)")
    ap.add_argument("--out", default=None, help="Output folder (default: Desktop\\wb_<id>)")
    ap.add_argument("--keep-webp", action="store_true", help="Keep .webp originals alongside JPG")
    ap.add_argument("--no-jpg", action="store_true", help="Skip JPG conversion (save .webp only)")
    args = ap.parse_args()

    nm = args.article
    vol = nm // 100000
    part = nm // 1000
    out = args.out or os.path.join(os.path.expanduser("~"), "Desktop", f"wb_{nm}")
    os.makedirs(out, exist_ok=True)

    print(f"nm={nm} vol={vol} part={part}")
    b = find_basket(nm, vol, part)
    if b is None:
        print("Could not locate basket host.", file=sys.stderr)
        sys.exit(1)
    host = f"basket-{b:02d}.wbbasket.ru"
    print(f"basket: {b:02d}")

    convert = not args.no_jpg
    if convert:
        try:
            from PIL import Image
        except ImportError:
            print("Pillow not installed (`pip install Pillow`); saving .webp only.", file=sys.stderr)
            convert = False

    saved = 0
    for n in range(1, 100):
        url = f"https://{host}/vol{vol}/part{part}/{nm}/images/big/{n}.webp"
        try:
            with fetch(url) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                break
            print(f"  {n}.webp: HTTP {e.code}", file=sys.stderr)
            break

        if convert:
            jpg_path = os.path.join(out, f"{n}.jpg")
            Image.open(BytesIO(data)).convert("RGB").save(jpg_path, "JPEG", quality=95)
            if args.keep_webp:
                with open(os.path.join(out, f"{n}.webp"), "wb") as f:
                    f.write(data)
            print(f"  {jpg_path} ({os.path.getsize(jpg_path):,} bytes)")
        else:
            webp_path = os.path.join(out, f"{n}.webp")
            with open(webp_path, "wb") as f:
                f.write(data)
            print(f"  {webp_path} ({len(data):,} bytes)")
        saved += 1

    print(f"\nDone. {saved} images -> {out}")


if __name__ == "__main__":
    main()
