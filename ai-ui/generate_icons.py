# SPDX-License-Identifier: MIT
from PIL import Image
import os

MASTER = "public/icons/icon-master_old.png"
OUTPUT_DIR = "public/icons"

SIZES = [72, 96, 128, 180, 192, 512]


def main():
    with Image.open(MASTER) as img:
        for size in SIZES:
            resized = img.resize((size, size), Image.LANCZOS)
            img_out = f"{OUTPUT_DIR}/icon-{size}x{size}.png"
            resized.save(img_out)
            print("Generated", img_out)


if __name__ == "__main__":
    main()
