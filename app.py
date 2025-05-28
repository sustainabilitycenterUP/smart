from flask import Flask, request, jsonify
import os
import fitz
import pytesseract
import cv2
import numpy as np
import re
import logging
import requests
import json
from werkzeug.utils import secure_filename

# Konfigurasi logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)s:%(name)s:%(message)s"
)
log = logging.getLogger('werkzeug')
log.setLevel(logging.DEBUG)

# Inisialisasi Flask
app = Flask(__name__)
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Path ke Tesseract (sesuaikan dengan environment Railway nanti)
# Jika pakai Linux image di Railway, tidak perlu atur ini secara manual
# pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# ------------------ UTILITAS PDF ------------------

def remove_illegal_chars(text):
    return re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', "", text)

def extract_text_with_fitz(pdf_path):
    with fitz.open(pdf_path) as doc:
        return "\n".join(page.get_text("text") for page in doc)

def extract_text_from_image(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return pytesseract.image_to_string(binary, lang="eng")

def extract_text_with_ocr(pdf_path):
    doc = fitz.open(pdf_path)
    full_text = ""
    for page in doc:
        pix = page.get_pixmap()
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
        full_text += extract_text_from_image(img) + "\n"
    return full_text

def extract_text_from_pdf(pdf_path):
    text = extract_text_with_fitz(pdf_path)
    cleaned = remove_illegal_chars(text)
    if len(cleaned.strip()) < 500:
        logging.info("Fallback to OCR karena teks terlalu pendek.")
        cleaned = remove_illegal_chars(extract_text_with_ocr(pdf_path))
    return cleaned

def extract_abstract(text):
    abstract_match = re.search(r"(?i)\bA\s*B\s*S\s*T\s*R\s*A\s*C\s*T\b", text)
    stop_heading_pattern = (
        r"(?im)^("
        r"(Keywords|Kata\s*Kunci)\s*[:\-]?\s*(.*)?$|"
        r"(Introduction|Latar\s*Belakang|Chapter\s*1|Bab\s*1|"
        r"(?:Chapter|Bab)?\s*(?:1|I)\.?\s+(?:Introduction|Latar\s*Belakang)|"
        r"Notation|Background)"
        r")\s*[:\-]?\s*$"
    )

    if abstract_match:
        abstract_start = abstract_match.end()
        stop_after_abstract = re.search(stop_heading_pattern, text[abstract_start:])
        if stop_after_abstract:
            abstract_end = abstract_start + stop_after_abstract.start()
            return text[abstract_start:abstract_end].strip()
        else:
            return " ".join(text[abstract_start:].split()[:300])
    else:
        stop_match = re.search(stop_heading_pattern, text)
        if stop_match:
            pre = text[:stop_match.start()].rstrip()
            paras = list(re.finditer(r'\n\s*\n', pre))
            if paras:
                return pre[paras[-1].end():].strip()
            else:
                return " ".join(pre.split()[-300:])
        else:
            return " ".join(text.split()[:300])

# ------------------ PROSES PDF + API AURORA ------------------

def classify_with_aurora(abstract):
    url = "https://aurora-sdg.labs.vu.nl/classifier/classify/aurora-sdg-multi"
    headers = {"Content-Type": "application/json"}
    payload = json.dumps({"text": abstract})

    try:
        response = requests.post(url, headers=headers, data=payload)
        if response.status_code == 200:
            predictions = response.json().get("predictions", [])
            filtered = [
                {
                    "label": p["sdg"]["label"],
                    "score": round(p["prediction"] * 100, 2)
                }
                for p in predictions if p["prediction"] >= 0.15
            ]
            logging.info("✅ SDG Classification Result:")
            for item in filtered:
                logging.info(f"- {item['label']}: {item['score']}%")
            return filtered
        else:
            logging.error(f"❌ Gagal panggil API Aurora: {response.status_code}")
            return []
    except Exception as e:
        logging.error(f"❌ Error saat memanggil API Aurora: {str(e)}")
        return []

def process_single_pdf(pdf_path):
    try:
        full_text = extract_text_from_pdf(pdf_path)
        abstract = extract_abstract(full_text)
        sdg_result = classify_with_aurora(abstract)
        return {
            "status": "success",
            "abstract": abstract,
            "sdg": sdg_result
        }
    except Exception as e:
        logging.error(f"❌ Error di process_single_pdf: {str(e)}")
        return {"status": "error", "message": str(e)}

# ------------------ ROUTES ------------------

@app.route("/", methods=["GET"])
def index():
    return "✅ API is running. Use /extract-abstract or /forminator-webhook."

@app.route("/extract-abstract", methods=["POST"])
def extract_abstract_api():
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file uploaded."}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"status": "error", "message": "Filename is empty."}), 400

    filename = secure_filename(file.filename)
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(file_path)

    result = process_single_pdf(file_path)
    os.remove(file_path)
    return jsonify(result)

@app.route("/forminator-webhook", methods=["POST"])
def forminator_webhook():
    data = request.json
    logging.debug("📥 Received data from Forminator: %s", data)

    file_url = data.get("upload_1")
    if not file_url:
        return jsonify({"status": "error", "message": "No file URL provided."}), 400

    try:
        response = requests.get(file_url)
        if response.status_code != 200:
            return jsonify({"status": "error", "message": "Failed to download file."}), 400

        filename = "uploaded.pdf"
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        with open(file_path, "wb") as f:
            f.write(response.content)

        result = process_single_pdf(file_path)
        os.remove(file_path)

        return jsonify(result)
    except Exception as e:
        logging.error(f"❌ Error in webhook: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ------------------ RUN ------------------

if __name__ == "__main__":
    app.run(debug=True)
