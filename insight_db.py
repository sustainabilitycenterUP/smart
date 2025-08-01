import psycopg2
from datetime import datetime
import os
import requests

# Konfigurasi koneksi ke database PostgreSQL dari environment variables
DB_CONFIG = {
    "host": os.getenv("PGHOST"),
    "port": os.getenv("PGPORT"),
    "dbname": os.getenv("PGDATABASE"),
    "user": os.getenv("PGUSER"),
    "password": os.getenv("PGPASSWORD"),
}


def get_connection():
    return psycopg2.connect(**DB_CONFIG)


def init_db():
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS uploads_new (
                    id SERIAL PRIMARY KEY,
                    filename TEXT,
                    upload_time TIMESTAMP,
                    ip TEXT,
                    location TEXT,
                    sdg INTEGER[]
                )
            ''')
        conn.commit()

def get_location_from_ip(ip_address):
    try:
        response = requests.get(f"http://ip-api.com/json/{ip_address}")
        data = response.json()
        if data["status"] == "success":
            return {
                "country": data.get("country"),
                "region": data.get("regionName"),
                "city": data.get("city"),
                "isp": data.get("isp")
            }
    except:
        pass
    return {}


def log_upload(filename, ip_address, sdg):
    location_data = get_location_from_ip(ip_address)
    location_str = ""
    if location_data:
        parts = [location_data.get("city"), location_data.get("region"), location_data.get("country")]
        location_str = ", ".join([p for p in parts if p])

    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO uploads_new (filename, upload_time, ip, location, sdg)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (filename, datetime.now(), ip_address, location_str, sdg)
            )
            submission_id = cursor.fetchone()[0]
        conn.commit()
        return submission_id



def get_insight():
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*), MAX(upload_time) FROM uploads_new")
            total, latest = cursor.fetchone()

            cursor.execute("SELECT filename, upload_time, ip, location, SDG FROM uploads_new ORDER BY upload_time DESC LIMIT 10")
            recent = cursor.fetchall()

    return total, latest, recent

def get_submission_detail(submission_id):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, filename, upload_time, sdg FROM uploads_new WHERE id = %s",
                (submission_id,)
            )
            row = cursor.fetchone()
            if row:
                return {
                    "id": row[0],
                    "filename": row[1],
                    "created_at": row[2],  # alias upload_time
                    "sdg": row[3]
                }
    return None

