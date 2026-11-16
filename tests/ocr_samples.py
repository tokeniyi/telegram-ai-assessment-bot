"""Synthetic pages for the OCR tests: rendered text with the damage real uploads have."""

import difflib
import io
import re

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

PARAGRAPH = [
    "Photosynthesis converts light energy into chemical energy.",
    "Chlorophyll in the chloroplasts absorbs red and blue light.",
    "Water is split and oxygen is released as a by-product.",
    "The Calvin cycle then fixes carbon dioxide into glucose.",
    "Mitochondria release that energy again during respiration.",
    "Osmosis moves water across a semi-permeable membrane.",
]
LEFT_COLUMN = [
    "The heart has four chambers.",
    "Two atria receive the blood",
    "and two ventricles pump it",
    "out to the lungs and body.",
    "Valves stop any backflow.",
]
RIGHT_COLUMN = [
    "Arteries carry blood away",
    "from the heart under high",
    "pressure while veins bring",
    "it back at low pressure.",
    "Capillaries link the two.",
]


def font(size=30):
    return ImageFont.load_default(size=size)


def page(lines=PARAGRAPH, size=(1300, 700), font_size=30, top=60, left=70, spacing=1.9) -> Image.Image:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((left, top + index * font_size * spacing), line, fill=(20, 20, 20), font=font(font_size))
    return image


def two_columns(title="Circulation of the blood in humans") -> Image.Image:
    image = Image.new("RGB", (1500, 620), "white")
    draw = ImageDraw.Draw(image)
    draw.text((330, 40), title, fill="black", font=font(44))
    for index, (left, right) in enumerate(zip(LEFT_COLUMN, RIGHT_COLUMN)):
        y = 170 + index * 58
        draw.text((70, y), left, fill="black", font=font(30))
        draw.text((800, y), right, fill="black", font=font(30))
    return image


def table() -> Image.Image:
    rows = [("Organelle", "Function"), ("Nucleus", "Stores the DNA"), ("Ribosome", "Builds proteins"),
            ("Mitochondrion", "Releases energy"), ("Chloroplast", "Makes glucose")]
    image = Image.new("RGB", (1200, 480), "white")
    draw = ImageDraw.Draw(image)
    for index, (name, role) in enumerate(rows):
        draw.text((80, 60 + index * 70), name, fill="black", font=font(30))
        draw.text((640, 60 + index * 70), role, fill="black", font=font(30))
    return image


def with_diagram() -> Image.Image:
    image = page(PARAGRAPH[:2], size=(1300, 800))
    draw = ImageDraw.Draw(image)
    draw.ellipse((420, 300, 880, 720), outline="black", width=5)
    draw.line((420, 510, 880, 510), fill="black", width=5)
    draw.rectangle((560, 380, 740, 640), outline="black", width=5)
    return image


def photographed(image: Image.Image, seed=7) -> Image.Image:
    """A phone photo of the page: shadow across it, low contrast, blur and sensor noise."""
    rng = np.random.default_rng(seed)
    pixels = np.asarray(image.filter(ImageFilter.GaussianBlur(1.1))).astype(np.float32)
    height, width = pixels.shape[:2]
    shadow = np.linspace(1.0, 0.45, width, dtype=np.float32)[None, :, None]
    pixels = (pixels * 0.7 + 40) * shadow + rng.normal(0, 9, pixels.shape)
    return Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))


def png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def jpeg(image: Image.Image, quality=85) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


def bgr(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("RGB"))[:, :, ::-1].copy()


def scanned_pdf(images: list[Image.Image]) -> bytes:
    """A PDF whose pages are pictures only, like the output of a scanner."""
    buffer = io.BytesIO()
    images[0].save(buffer, "PDF", save_all=True, append_images=images[1:], resolution=150)
    return buffer.getvalue()


def text_pdf(pages: list[list[str]]) -> bytes:
    """A minimal PDF with a real text layer, one list of lines per page."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None,
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines in pages:
        body = "BT /F1 12 Tf 50 780 Td 16 TL\n" + "\n".join(
            "(" + l.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)") + ") Tj T*" for l in lines
        ) + "\nET"
        objects.append(f"<< /Length {len(body)} >>\nstream\n{body}\nendstream")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                       f"/Resources << /Font << /F1 3 0 R >> >> /Contents {len(objects)} 0 R >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def merge_pdfs(*documents: bytes) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for document in documents:
        for pdf_page in PdfReader(io.BytesIO(document)).pages:
            writer.add_page(pdf_page)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def accuracy(expected: str | list[str], actual: str) -> float:
    """Character-level similarity (0..1) ignoring case, spacing and line breaks."""
    if not isinstance(expected, str):
        expected = " ".join(expected)

    def squash(text):
        return re.sub(r"\s+", " ", text.lower()).strip()

    return difflib.SequenceMatcher(None, squash(expected), squash(actual), autojunk=False).ratio()
