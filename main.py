import os
import shutil
import uuid
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import exifread
from supabase import create_client, Client
from geopy.distance import geodesic

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Imposta le variabili d'ambiente SUPABASE_URL e SUPABASE_KEY.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def _convert_to_degrees(value):
    d = float(value.values[0].num) / float(value.values[0].den)
    m = float(value.values[1].num) / float(value.values[1].den)
    s = float(value.values[2].num) / float(value.values[2].den)
    return d + (m / 60.0) + (s / 3600.0)


def extract_gps(image_path):
    """Estrae latitudine e longitudine dai metadati EXIF."""
    try:
        with open(image_path, "rb") as f:
            tags = exifread.process_file(f, details=False)
            if "GPS GPSLatitude" in tags and "GPS GPSLongitude" in tags:
                lat_tag = tags["GPS GPSLatitude"]
                lat_ref = str(tags["GPS GPSLatitudeRef"].values).strip()
                lon_tag = tags["GPS GPSLongitude"]
                lon_ref = str(tags["GPS GPSLongitudeRef"].values).strip()

                lat = _convert_to_degrees(lat_tag)
                if lat_ref != "N":
                    lat = -lat

                lon = _convert_to_degrees(lon_tag)
                if lon_ref != "E":
                    lon = -lon

                return lat, lon
    except Exception:
        pass
    return None


@app.post("/api/segnala")
async def crea_segnalazione(categoria: str = Form(...), file: UploadFile = File(...)):
    if categoria not in ("alberi", "depavimentazione"):
        raise HTTPException(status_code=400, detail="Categoria non valida.")

    ext = os.path.splitext(file.filename or "foto.jpg")[1].lower()
    if ext not in (".jpg", ".jpeg"):
        raise HTTPException(status_code=400, detail="Sono ammessi solo file .jpg o .jpeg.")

    temp_name = f"temp_{uuid.uuid4().hex}{ext}"

    try:
        with open(temp_name, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        coords = extract_gps(temp_name)
        if not coords:
            raise HTTPException(
                status_code=400,
                detail="La foto non contiene dati GPS validi. Carica lo scatto originale.",
            )

        lat_nuova, lon_nuova = coords

        # Controllo duplicati nel raggio di 10 metri per la stessa categoria
        response = (
            supabase.table("segnalazioni")
            .select("*")
            .eq("categoria", categoria)
            .execute()
        )
        esistenti = response.data or []

        soglia_metri = 10.0
        for item in esistenti:
            distanza = geodesic(
                (lat_nuova, lon_nuova), (item["latitudine"], item["longitudine"])
            ).meters
            if distanza <= soglia_metri:
                return {
                    "status": "duplicato",
                    "message": f"Segnalazione già esistente a {distanza:.1f} metri di distanza.",
                }

        # Nome file univoco per evitare sovrascritture nello storage
        nome_univoco = f"{uuid.uuid4().hex}{ext}"
        storage_path = f"{categoria}/{nome_univoco}"

        with open(temp_name, "rb") as f:
            file_bytes = f.read()
            supabase.storage.from_("foto-segnalazioni").upload(
                storage_path,
                file_bytes,
                file_options={"content-type": "image/jpeg"},
            )

        public_url = supabase.storage.from_("foto-segnalazioni").get_public_url(
            storage_path
        )

        supabase.table("segnalazioni").insert(
            {
                "categoria": categoria,
                "latitudine": lat_nuova,
                "longitudine": lon_nuova,
                "file_url": public_url,
            }
        ).execute()

        return {
            "status": "success",
            "message": "Segnalazione caricata e validata con successo!",
        }

    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)


@app.get("/api/segnalazioni")
def get_segnalazioni():
    response = supabase.table("segnalazioni").select("*").execute()
    return response.data or []
