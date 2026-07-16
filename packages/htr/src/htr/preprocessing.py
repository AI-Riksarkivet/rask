"""Image preprocessing for inference."""

import numpy as np
from PIL import Image


def preprocess_rtmdet(image: Image.Image, input_size: int = 640) -> tuple[np.ndarray, float]:
    """Resize with keep_ratio + pad bottom-right (MMDet convention).
    Returns (tensor [1,3,H,W], scale).
    """
    orig_w, orig_h = image.size
    scale = min(input_size / orig_w, input_size / orig_h)
    new_w, new_h = round(orig_w * scale), round(orig_h * scale)

    resized = image.resize((new_w, new_h), Image.Resampling.BILINEAR)
    padded = Image.new("RGB", (input_size, input_size), (114, 114, 114))
    padded.paste(resized, (0, 0))

    arr = np.array(padded, dtype=np.float32) / 255.0
    tensor = arr.transpose(2, 0, 1)[np.newaxis]  # [1, 3, H, W]
    return tensor, scale


def preprocess_yolo(image: Image.Image, input_size: int = 640) -> tuple[np.ndarray, float, int, int]:
    """Letterbox resize for YOLO (centered, gray padding).
    Returns (tensor [1,3,H,W], scale, padX, padY).
    """
    orig_w, orig_h = image.size
    scale = min(input_size / orig_w, input_size / orig_h)
    new_w, new_h = round(orig_w * scale), round(orig_h * scale)
    pad_x = (input_size - new_w) // 2
    pad_y = (input_size - new_h) // 2

    padded = Image.new("RGB", (input_size, input_size), (114, 114, 114))  # Ultralytics letterbox default
    resized = image.resize((new_w, new_h), Image.Resampling.BILINEAR)
    padded.paste(resized, (pad_x, pad_y))

    arr = np.array(padded, dtype=np.float32) / 255.0
    tensor = arr.transpose(2, 0, 1)[np.newaxis]
    return tensor, scale, pad_x, pad_y


def preprocess_trocr(image: Image.Image, size: int = 384) -> np.ndarray:
    """Resize and normalize for TrOCR encoder.
    Returns tensor [1, 3, 384, 384].
    """
    resized = image.resize((size, size), Image.Resampling.BILINEAR)
    arr = np.array(resized, dtype=np.float32) / 127.5 - 1.0
    return arr.transpose(2, 0, 1)[np.newaxis]


def crop_region(image: Image.Image, x: float, y: float, w: float, h: float) -> Image.Image:
    """Crop a region from the image."""
    x1 = max(0, int(x))
    y1 = max(0, int(y))
    x2 = min(image.width, max(x1 + 1, int(x + w)))
    y2 = min(image.height, max(y1 + 1, int(y + h)))
    return image.crop((x1, y1, x2, y2))


def crop_polygon(image: Image.Image, polygon: list[list[float]], transparent: bool = False) -> Image.Image:
    """Crop a region using a polygon mask.

    Returns a rectangular image cropped to the polygon's bounding box,
    with pixels outside the polygon masked out.

    Args:
        transparent: If True, returns RGBA with transparent background.
                     If False, returns RGB with white background.
    """
    if not polygon:
        return image

    arr = np.array(image)
    h, w = arr.shape[:2]

    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    x1, x2 = max(0, int(min(xs))), min(w, int(max(xs)) + 1)
    y1, y2 = max(0, int(min(ys))), min(h, int(max(ys)) + 1)

    poly_shifted = [(px - x1, py - y1) for px, py in polygon]

    from PIL import ImageDraw

    mask_img = Image.new("L", (x2 - x1, y2 - y1), 0)
    draw = ImageDraw.Draw(mask_img)
    draw.polygon([(px, py) for px, py in poly_shifted], fill=255)
    mask = np.array(mask_img)

    cropped = arr[y1:y2, x1:x2].copy()

    if transparent:
        # RGBA with transparent background
        rgba = np.zeros((cropped.shape[0], cropped.shape[1], 4), dtype=np.uint8)
        rgba[:, :, :3] = cropped
        rgba[:, :, 3] = mask
        return Image.fromarray(rgba, "RGBA")

    # RGB with white background
    cropped[mask == 0] = 255
    return Image.fromarray(cropped)
