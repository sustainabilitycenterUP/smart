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
from datetime import timezone, datetime
from collections import Counter
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
    logo_height = logo_width * (0.55 / 2.2)  # rasio

    page_width, page_height = A4
    x = (page_width - logo_width) / 2
    y = page_height - logo_height - 0.2 * inch

    canvas.drawImage(logo_path, x, y, width=logo_width,
                     height=logo_height, preserveAspectRatio=True)

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

    canvas.drawImage(
        footer_path,
        x,
        y,
        width=footer_width,
        height=footer_height,
        preserveAspectRatio=True,
        mask='auto'
    )


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
        stop_after_abstract = re.search(
            stop_heading_pattern, text[abstract_start:])
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


# ------------------ KLASIFIKASI MODEL ------------------


def classify_with_model(abstract, model="elsevier"):
    """
    Memanggil model SDG berdasarkan pilihan:
      - "elsevier" → elsevier-sdg-multi (16 goals, THE)
      - "aurora"   → aurora-sdg-multi (17 goals)
    """
    if model == "aurora":
        url = "https://aurora-sdg.labs.vu.nl/classifier/classify/aurora-sdg-multi"
    else:
        # default ke Elsevier
        model = "elsevier"
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

            logging.info(f"✅ SDG Classification ({model}):")
            for label, score in sorted(all_sdg_scores.items(), key=lambda x: x[1], reverse=True):
                logging.info(f"- {label}: {score}%")

            return all_sdg_scores
        else:
            logging.error(
                f"❌ Gagal panggil API SDG model={model}: {response.status_code}")
            return {}
    except Exception as e:
        logging.error(
            f"❌ Error saat memanggil API SDG model={model}: {str(e)}")
        return {}


def process_single_pdf(pdf_path, model="elsevier"):
    try:
        full_text = extract_text_from_pdf(pdf_path)
        abstract = extract_abstract(full_text)
        sdg_result = classify_with_model(abstract, model=model)
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
    return "✅ API is running. Use /extract-abstract or /classify-text."


@app.route("/classify-text", methods=["POST"])
def classify_text_api():
    """
    Klasifikasi langsung dari teks (tanpa PDF).
    Body: JSON { "text": "...", "model": "elsevier" | "aurora" }
    Response: sama formatnya dengan /extract-abstract
    """
    data = request.get_json()
    if not data or "text" not in data:
        return jsonify({"status": "error", "message": "No text provided."}), 400

    text = data.get("text", "").strip()
    if not text:
        return jsonify({"status": "error", "message": "Text is empty."}), 400

    model = data.get("model", "elsevier")

    abstract = text
    sdg_result = classify_with_model(abstract, model=model)

    sdg_list = [
        int(sdg.replace("Goal ", ""))
        for sdg, score in sdg_result.items()
        if score > 30
    ]

    filename_label = f"TEXT_INPUT_{model.upper()}"
    submission_id = log_upload(filename_label, request.remote_addr, sdg_list)

    return jsonify({
        "status": "success",
        "abstract": abstract,
        "sdg": sdg_result,
        "submission_id": submission_id
    })


@app.route("/extract-abstract", methods=["POST"])
def extract_abstract_api():
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file uploaded."}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"status": "error", "message": "Filename is empty."}), 400

    # model dipilih dari form (Elsevier / Aurora)
    model = request.form.get("model", "elsevier")

    filename = secure_filename(file.filename)
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(file_path)

    result = process_single_pdf(file_path, model=model)

    sdg_list = []
    if result.get("status") == "success":
        sdg_scores = result.get("sdg", {})
        sdg_list = [
            int(sdg.replace("Goal ", "")) for sdg, score in sdg_scores.items() if score > 30
        ]

    submission_id = log_upload(filename, request.remote_addr, sdg_list)

    os.remove(file_path)
    result["submission_id"] = submission_id
    return jsonify(result)


@app.route("/admin", methods=["GET"])
def admin_dashboard():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    cur.execute("SELECT upload_time FROM uploads_new")
    all_times = [row[0] for row in cur.fetchall() if row[0]]

    cur.execute("""
        SELECT filename, upload_time, ip, location, sdg
        FROM uploads_new
        ORDER BY upload_time DESC
        LIMIT 10
    """)
    recent = cur.fetchall()

    total = len(all_times)
    last_upload = max(all_times) if all_times else None

    from collections import Counter
    from datetime import datetime
    from zoneinfo import ZoneInfo

    month_counts = Counter(dt.strftime("%Y-%m") for dt in all_times)
    sorted_months = sorted(month_counts.keys(),
                           key=lambda x: datetime.strptime(x, "%Y-%m"))
    month_labels = [m for m in sorted_months]
    month_values = [month_counts[m] for m in sorted_months]

    cumulative_values = []
    cumulative_sum = 0
    for val in month_values:
        cumulative_sum += val
        cumulative_values.append(cumulative_sum)

    conn.close()

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Platform Insight</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
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
            canvas {{
                width: 100%;
                height: auto;
                max-height: 350px;
                display: block;
                margin: 0 auto;
            }}
            .chart-container {{
                width: 65%;
                margin: 0 auto;
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
            <h2>📈 Upload Trend per Month</h2>
            <div class="chart-container">
                <canvas id="uploadChart"></canvas>
            </div>
        </div>

        <div class="section">
            <h2>📈 Cumulative Upload Growth</h2>
            <div class="chart-container">
                <canvas id="cumulativeChart"></canvas>
            </div>
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

        <script>
            const ctx1 = document.getElementById('uploadChart').getContext('2d');
            new Chart(ctx1, {{
                type: 'line',
                data: {{
                    labels: {month_labels},
                    datasets: [{{
                        label: 'Uploads per Month',
                        data: {month_values},
                        borderColor: '#4A148C',
                        backgroundColor: 'rgba(74, 20, 140, 0.1)',
                        fill: true,
                        tension: 0.3,
                        pointRadius: 4,
                        pointBackgroundColor: '#4A148C'
                    }}]
                }},
                options: {{
                    responsive: true,
                    scales: {{
                        y: {{
                            beginAtZero: true,
                            title: {{
                                display: true,
                                text: 'Number of Uploads'
                            }}
                        }},
                        x: {{
                            title: {{
                                display: true,
                                text: 'Month-Year'
                            }}
                        }}
                    }}
                }}
            }});

            const ctx2 = document.getElementById('cumulativeChart').getContext('2d');
            new Chart(ctx2, {{
                type: 'line',
                data: {{
                    labels: {month_labels},
                    datasets: [{{
                        label: 'Cumulative Uploads',
                        data: {cumulative_values},
                        borderColor: '#00695C',
                        backgroundColor: 'rgba(0, 150, 136, 0.1)',
                        fill: true,
                        tension: 0.3,
                        pointRadius: 4,
                        pointBackgroundColor: '#00695C'
                    }}]
                }},
                options: {{
                    responsive: true,
                    scales: {{
                        y: {{
                            beginAtZero: true,
                            title: {{
                                display: true,
                                text: 'Total Uploads (Cumulative)'
                            }}
                        }},
                        x: {{
                            title: {{
                                display: true,
                                text: 'Month-Year'
                            }}
                        }}
                    }}
                }}
            }});
        </script>
    </body>
    </html>
    """
    return render_template_string(html)


@app.route('/download_result', methods=['POST'])
def download_result():
    logging.info("📥 POST /download_result called")
    data = request.get_json()
    logging.info(f"📥 Payload received: {data}")

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
    submission_date_str = upload_time.astimezone(
        ZoneInfo("Asia/Jakarta")).strftime("%Y-%m-%d %H:%M:%S")

    abstract = data.get("abstract", "")
    sdg_scores = data.get("sdg", {})

    # --- Model info from frontend (optional, default: elsevier) ---
    model = data.get("model", "elsevier")
    if model == "aurora":
        model = "aurora"
        model_full_name = "Aurora SDG multi-label mBERT model"
        model_desc = (
            "This report was generated using the Aurora SDG multi-label mBERT model, "
            "which classifies text into all 17 SDG goals with an average precision of around 70.05%."
        )
        goal_info = "multi-label classification across 17 SDG goals."
    else:
        model = "elsevier"
        model_full_name = "Elsevier SDG multi-class mBERT model"
        model_desc = (
            "This report was generated using the Elsevier SDG multi-class mBERT model, "
            "which classifies text into 16 SDG goals and is used in the THE Impact Rankings methodology."
        )
        goal_info = "multi-class classification across 16 SDG goals."

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        topMargin=1 * inch
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

    # Title space
    elements.append(Spacer(1, 42))

    # General Notes (now model-aware + jelaskan PDF vs text flow)
    elements.append(Paragraph("General Notes", heading_style))
    notes = f"""
    This application performs Sustainable Development Goal (SDG) classification based on either an abstract automatically extracted
    from a PDF document or text directly provided by the user.<br/><br/>
    <b>Model used:</b> {model_full_name}. {model_desc} It performs {goal_info}<br/><br/>
    <b>Processing flow:</b><br/>
    1. <b>PDF uploads:</b> The PDF is parsed using the fitz library (PyMuPDF). The system attempts to detect and extract the abstract
       section. If an explicit abstract is not found, a portion of the document is used as a proxy abstract. The resulting text is
       then sent to the selected SDG model for classification.<br/><br/>
    2. <b>Pasted text:</b> When users paste text or an abstract directly into the web form, this input is treated as the abstract
       without any PDF parsing. The text is sent as-is to the same selected SDG model for SDG classification.<br/><br/>
    The model returns percentage scores (0–100%) for each SDG, indicating the degree of relevance between the input text and every goal.
    Multiple SDGs can be associated with a single document depending on the model's confidence profile.<br/><br/>
    This abstract/text-based analysis enables efficient and scalable SDG classification to support sustainability reporting,
    research mapping, and strategic decision-making.
    """
    elements.append(Paragraph(notes, justified_style))
    elements.append(Spacer(1, 18))

    # Divider image
    divider_path = "uploads/divider.png"
    img_reader = ImageReader(divider_path)
    orig_width, orig_height = img_reader.getSize()

    margin = 1 * inch
    available_width = doc.pagesize[0] - margin

    scale = available_width / orig_width
    new_width = available_width
    new_height = orig_height * scale

    divider = Image(divider_path, width=new_width, height=new_height)

    elements.append(divider)
    elements.append(Spacer(1, 16))

    # Meta info (tambahkan model di sini juga biar jelas)
    elements.append(Paragraph(
        f"<b>Model Used:</b> <font color='#0000FF'>{model_full_name}</font>", justified_style))
    elements.append(Paragraph(
        f"<b>Submission ID:</b> <font color='#0000FF'>{submission_id_str}</font>", justified_style))
    elements.append(Paragraph(
        f"<b>Submission Date:</b> <font color='#0000FF'>{submission_date_str}</font>", justified_style))
    elements.append(Paragraph(
        f"<b>File Name:</b> <font color='#0000FF'>{filename}</font>", justified_style))

    if not sdg_ids:
        elements.append(Paragraph(
            "<b>SDG Detected (filter >30%):</b> <font color='#0000FF'>None</font>", justified_style))
    else:
        sdg_texts = [f"Goal {sid} – {SDG_NAMES.get(sid, 'Unknown')}" for sid in sdg_ids]
        sdg_line = "; ".join(sdg_texts)
        elements.append(Paragraph(
            f"<b>SDG Detected (filter >30%):</b> <font color='#0000FF'>{sdg_line}</font>", justified_style))

    elements.append(Spacer(1, 18))

    elements.append(PageBreak())

    # Abstract / Text section (judul diganti)
    elements.append(Paragraph("Detected Abstract / Text", heading_style))
    elements.append(Paragraph(abstract or "-", justified_style))
    elements.append(Spacer(1, 18))

    # SDG Classification Results
    elements.append(Paragraph("SDG Classification Results", heading_style))

    sorted_scores = sorted(sdg_scores.items(), key=lambda x: x[1], reverse=True)
    table_data = [["SDG", "Relevance (%)"]] + [[k, f"{v:.2f}%"] for k, v in sorted_scores]

    table = Table(table_data, colWidths=[3 * inch, 2 * inch])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#31572C")),
        ("TEXTCOLOR", (0, 0), (-1, 0), HexColor("#FFFFFF")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [HexColor("#F5F5F5"), HexColor("#FFFFFF")]),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#CCCCCC"))
    ]))

    elements.append(table)

    # Build PDF
    doc.build(elements, onFirstPage=draw_first_page,
              onLaterPages=draw_footer)
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
