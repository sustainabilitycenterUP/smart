import os
import re
import io
import json
import logging
from io import BytesIO

import fitz  # PyMuPDF
import psycopg2
import requests
from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS
from werkzeug.utils import secure_filename
from fpdf import FPDF
from datetime import timezone
from zoneinfo import ZoneInfo

# ==== ReportLab for PDF Generation ====
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table,
    TableStyle, Image, HRFlowable, PageBreak
)
from reportlab.lib.utils import ImageReader
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor
from reportlab.lib.units import inch
from reportlab.pdfgen.canvas import Canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ==== Local Module ====
from insight_db import init_db, log_upload, get_insight, get_submission_detail


pdfmetrics.registerFont(TTFont("ArialNova", "static/fonts/ArialNova.ttf"))
pdfmetrics.registerFont(TTFont("ArialNova-Bold", "static/fonts/ArialNova-Bold.ttf"))

pdfmetrics.registerFontFamily(
    'ArialNova',
    normal='ArialNova',
    bold='ArialNova-Bold'
)

DB_CONFIG = {
    "host": os.getenv("PGHOST"),
    "port": os.getenv("PGPORT"),
    "dbname": os.getenv("PGDATABASE"),
    "user": os.getenv("PGUSER"),
    "password": os.getenv("PGPASSWORD"),
}

# Konfigurasi logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)s:%(name)s:%(message)s"
)
log = logging.getLogger('werkzeug')
log.setLevel(logging.DEBUG)

# Inisialisasi Flask
app = Flask(__name__)
CORS(app, expose_headers=["Content-Disposition"])
UPLOAD_FOLDER = "uploads"
init_db()
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ------------------ UTILITAS PDF ------------------

def remove_illegal_chars(text):
    return re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', "", text)

def extract_text_with_fitz(pdf_path):
    with fitz.open(pdf_path) as doc:
        return "\n".join(page.get_text("text") for page in doc)

def extract_text_from_pdf(pdf_path):
    text = extract_text_with_fitz(pdf_path)
    return remove_illegal_chars(text)

def draw_header(canvas, doc):
    logo_path = "uploads/LOGO_SC.jpg"
    logo_width = 2.8 * inch  
    logo_height = logo_width * (0.55 / 2.2)  #rasio

    page_width, page_height = A4
    x = (page_width - logo_width) / 2
    y = page_height - logo_height - 0.2 * inch

    canvas.drawImage(logo_path, x, y, width=logo_width, height=logo_height, preserveAspectRatio=True)

    text = "SDG Mapping and Assessment Report"
    canvas.setFont("ArialNova-Bold", 20)  
    text_width = canvas.stringWidth(text, "ArialNova-Bold", 20)
    x = (page_width - text_width) / 2
    y = page_height - 1.5 * inch

    canvas.drawString(x, y, text)



def draw_footer(canvas, doc):
    footer_path = "uploads/footer.png"
    footer_width = doc.pagesize[0]
    footer_height = 0.9 * inch  

    x = 0
    y = 0  

    canvas.drawImage(footer_path, x, y, width=footer_width, height=footer_height, preserveAspectRatio=True, mask='auto')

def draw_first_page(canvas, doc):
    draw_header(canvas, doc)
    draw_footer(canvas, doc)


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

def classify_with_aurora(abstract):
    url = "https://aurora-sdg.labs.vu.nl/classifier/classify/elsevier-sdg-multi"
    headers = {"Content-Type": "application/json"}
    payload = json.dumps({"text": abstract})

    try:
        response = requests.post(url, headers=headers, data=payload)
        if response.status_code == 200:
            predictions = response.json().get("predictions", [])

            all_sdg_scores = {
                p["sdg"]["label"]: round(p["prediction"] * 100, 2)
                for p in predictions
            }

            # Logging top N atau semua
            logging.info("✅ SDG Classification (All):")
            for label, score in sorted(all_sdg_scores.items(), key=lambda x: x[1], reverse=True):
                logging.info(f"- {label}: {score}%")

            return all_sdg_scores  # ← dikembalikan dalam format dict langsung
        else:
            logging.error(f"❌ Gagal panggil API Aurora: {response.status_code}")
            return {}
    except Exception as e:
        logging.error(f"❌ Error saat memanggil API Aurora: {str(e)}")
        return {}


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

    sdg_list = []
    if result.get("status") == "success":
        sdg_scores = result.get("sdg", {})
        sdg_list = [int(sdg.replace("Goal ", "")) for sdg, score in sdg_scores.items() if score > 30]

    submission_id = log_upload(filename, request.remote_addr, sdg_list)

    os.remove(file_path)
    result["submission_id"] = submission_id
    return jsonify(result)


@app.route("/admin", methods=["GET"])
def admin_dashboard():
    total, last_upload, recent = get_insight()
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Platform Insight</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                margin: 40px;
                background-color: #f9f9f9;
                color: #333;
            }}
            h1 {{
                color: #4A148C;
            }}
            .section {{
                background-color: #fff;
                padding: 20px;
                margin-bottom: 30px;
                border-radius: 8px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin-top: 15px;
            }}
            th, td {{
                border: 1px solid #ddd;
                padding: 10px;
                text-align: left;
            }}
            th {{
                background-color: #f2f2f2;
                color: #555;
            }}
            tr:nth-child(even) {{
                background-color: #f8f8f8;
            }}
        </style>
    </head>
    <body>
        <div class="section">
            <h1>📊 Platform Insight</h1>
            <p><strong>Total uploads:</strong> {total}</p>
            <p><strong>Last upload:</strong> {last_upload.astimezone(ZoneInfo("Asia/Jakarta")).strftime('%Y-%m-%d %H:%M:%S') if last_upload else 'N/A'}</p>
        </div>

        <div class="section">
            <h2>🕒 Last 10 uploads:</h2>
            <table>
                <thead>
                    <tr>
                        <th>Filename</th>
                        <th>Timestamp</th>
                        <th>IP Address</th>
                        <th>Location</th>
                        <th>SDG</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(
                        f'<tr>'
                        f'<td>{f}</td>'
                        f'<td>{t.astimezone(ZoneInfo("Asia/Jakarta")).strftime("%Y-%m-%d %H:%M:%S")}</td>'
                        f'<td>{ip}</td>'
                        f'<td>{loc}</td>'
                        f'<td>{sdg if sdg else "-"}</td>'
                        f'</tr>'
                        for f, t, ip, loc, sdg in recent
                    )}
                </tbody>
            </table>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)


@app.route('/download_result', methods=['POST'])
def download_result():
    logging.info("📥 POST /download_result called")
    data = request.get_json()
    logging.info(f"📥 Payload received: {data}")
    
    data = request.get_json()
    submission_id = data.get("submission_id")
    if not submission_id:
        return jsonify({"status": "error", "message": "submission_id is required"}), 400
    
    record = get_submission_detail(submission_id)
    if not record:
        return jsonify({"status": "error", "message": "Submission ID not found"}), 404
    
    filename = record["filename"].rsplit(".", 1)[0]
    upload_time = record["created_at"]
    sdg_ids = record["sdg"] or []
    
    submission_id_str = f"{submission_id:05d}"
    submission_date_str = upload_time.astimezone(ZoneInfo("Asia/Jakarta")).strftime("%Y-%m-%d %H:%M:%S")
    
    # Ambil dari frontend tetap:
    abstract = data.get("abstract", "")
    sdg_scores = data.get("sdg", {})

    # Prepare PDF in memory
    buffer = BytesIO()
    doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            topMargin=1* inch  # atur agar isi tidak nabrak header
        )
    doc.title = "SMART SDG Classifier"
    doc.author = "https://super.universitaspertamina.ac.id/index.php/smart/"
    styles = getSampleStyleSheet()

    normal_style = styles["Normal"]
    normal_style.fontName = "ArialNova"
    normal_style.spaceAfter = 12

    justified_style = ParagraphStyle(
        name="Justified",
        parent=normal_style,
        alignment=TA_JUSTIFY,
        fontSize=11,
        fontName="ArialNova"
    )

    heading_style = ParagraphStyle(
        name="Heading",
        fontSize=14,
        leading=16,
        fontName="ArialNova-Bold",
        textColor=HexColor("#31572C"),
        alignment=TA_LEFT,
        spaceBefore=12,
        spaceAfter=6
    )

    elements = []
    SDG_NAMES = {
        1: "No Poverty",
        2: "Zero Hunger",
        3: "Good Health and Well-being",
        4: "Quality Education",
        5: "Gender Equality",
        6: "Clean Water and Sanitation",
        7: "Affordable and Clean Energy",
        8: "Decent Work and Economic Growth",
        9: "Industry, Innovation and Infrastructure",
        10: "Reduced Inequalities",
        11: "Sustainable Cities and Communities",
        12: "Responsible Consumption and Production",
        13: "Climate Action",
        14: "Life Below Water",
        15: "Life on Land",
        16: "Peace, Justice and Strong Institutions",
        17: "Partnerships for the Goals"
    }

    # Title
    elements.append(Spacer(1, 42))

    # General Notes
    elements.append(Paragraph("General Notes", heading_style))
    notes = """
    This application performs Sustainable Development Goal (SDG) classification based on the abstract extracted from a PDF document.
    The document is parsed using the fitz library (PyMuPDF), which allows structured reading and text extraction.<br/><br/>
    The application first attempts to detect and extract the abstract section from the PDF. If an abstract is not detected, 
    the fallback mechanism extracts the first 500 words from the document as a proxy for the abstract.<br/><br/>
    The extracted text is then analyzed using the Aurora SDG multi-label mBERT model (https://aurora-sdg.labs.vu.nl/sdg-classifier/text). 
    This model performs multi-label classification across all 17 Sustainable Development Goals (SDGs).<br/><br/>
    The output consists of percentage scores (ranging from 0% to 100%) for each SDG, indicating the degree of relevance between the input text and each goal. 
    Multiple SDGs can be associated with a single document depending on the model’s confidence levels.<br/><br/>
    This abstract-based analysis enables efficient and scalable SDG classification.
    """
    elements.append(Paragraph(notes, justified_style))
    elements.append(Spacer(1, 18))
    divider_path = "uploads/divider.png"
    img_reader = ImageReader(divider_path)
    orig_width, orig_height = img_reader.getSize()

    # Hitung ukuran baru agar lebarnya sesuai dengan lebar halaman dikurangi margin
    margin = 1 * inch  # margin kiri + kanan = 2 inch
    available_width = doc.pagesize[0] - margin

    # Hitung rasio dan sesuaikan tinggi
    scale = available_width / orig_width
    new_width = available_width
    new_height = orig_height * scale

    divider = Image(divider_path, width=new_width, height=new_height)

    elements.append(divider)
    elements.append(Spacer(1, 16))

    elements.append(Paragraph(
        f"<b>Submission ID:</b> <font color='#0000FF'>{submission_id_str}</font>", justified_style))
    elements.append(Paragraph(
        f"<b>Submission Date:</b> <font color='#0000FF'>{submission_date_str}</font>", justified_style))
    elements.append(Paragraph(
        f"<b>File Name:</b> <font color='#0000FF'>{filename}</font>", justified_style))
    
    if not sdg_ids:
        elements.append(Paragraph("<b>SDG Detected:</b> <font color='#0000FF'>None</font>", justified_style))
    else:
        sdg_texts = [f"Goal {sid} – {SDG_NAMES.get(sid, 'Unknown')}" for sid in sdg_ids]
        sdg_line = "; ".join(sdg_texts)
        elements.append(Paragraph(f"<b>SDG Detected:</b> <font color='#0000FF'>{sdg_line}</font>", justified_style))

    elements.append(Spacer(1, 18))

    elements.append(PageBreak())


    # Abstract
    elements.append(Paragraph("Detected Abstract", heading_style))
    elements.append(Paragraph(abstract, justified_style))
    elements.append(Spacer(1, 18))

    # SDG Classification Results
    elements.append(Paragraph("SDG Classification Results", heading_style))

    sorted_scores = sorted(sdg_scores.items(), key=lambda x: x[1], reverse=True)
    table_data = [["SDG", "Relevance (%)"]] + [[k, f"{v:.2f}%"] for k, v in sorted_scores]

    table = Table(table_data, colWidths=[3*inch, 2*inch])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#31572C")),
        ("TEXTCOLOR", (0, 0), (-1, 0), HexColor("#FFFFFF")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [HexColor("#F5F5F5"), HexColor("#FFFFFF")]),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#CCCCCC"))
    ]))

    elements.append(table)

    # Build and send PDF
    doc.build(elements, onFirstPage=draw_first_page, onLaterPages=draw_footer)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"{filename}_sdg_report.pdf",
        mimetype="application/pdf"
    )

# ------------------ RUN ------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
