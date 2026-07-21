#!/usr/bin/env python3
"""Poll-Schnittstelle: liest BDT-Dateien aus einem Ordner (POLL_DIR) und legt
daraus Modality-Worklist-Eintraege (MWL) im dcm4chee-arc PACS an ueber dessen
REST-Schnittstelle (POST /aets/{aet}/rs/mwlitems, application/dicom+json).

BDT (Behandlungsdaten-Datentraeger) definiert selbst keine Felder fuer
bildgebende Auftraege (Modalitaet, Geraete-AE-Title, Termin). Dafuer werden
hier zusaetzliche, frei belegbare Feldkennungen (6200, 6220-6224) verwendet,
die das exportierende System beim Schreiben der BDT-Datei mit ausgeben muss.
Die komplette Zuordnung ist ueber FIELD_MAP anpassbar, falls das
Quellsystem andere Feldkennungen nutzt.
"""
import json
import logging
import os
import shutil
import time
import uuid
from datetime import datetime

import requests

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("bdt-bridge")

POLL_DIR = os.environ.get("POLL_DIR", "/bdt_poll")
PROCESSED_DIR = os.environ.get("PROCESSED_DIR", os.path.join(POLL_DIR, "verarbeitet"))
ERROR_DIR = os.environ.get("ERROR_DIR", os.path.join(POLL_DIR, "fehler"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
BDT_ENCODING = os.environ.get("BDT_ENCODING", "cp850")

ARC_BASE_URL = os.environ.get("ARC_BASE_URL", "http://arc:8080/dcm4chee-arc").rstrip("/")
ARC_AE_TITLE = os.environ.get("ARC_AE_TITLE", "DCM4CHEE")

DEFAULT_MODALITY = os.environ.get("DEFAULT_MODALITY", "OT")
DEFAULT_STATION_AET = os.environ.get("DEFAULT_STATION_AET", "")

# BDT-Feldkennung -> interner Schluessel.
FIELD_MAP = {
    # Standard-BDT-Patientenstammdaten
    "3000": "patient_id",
    "3101": "nachname",
    "3102": "vorname",
    "3103": "geburtsdatum",  # TTMMJJJJ
    "3110": "geschlecht",  # 1=maennlich, 2=weiblich, 3=divers
    # Bridge-spezifische Erweiterungsfelder fuer bildgebende Auftraege
    # (kein Bestandteil des offiziellen BDT-Feldkatalogs)
    "6220": "auftragsnummer",
    "6221": "modalitaet",
    "6222": "stations_aet",
    "6223": "termin",  # JJJJMMTTHHMM
    "6224": "beschreibung",
    "6200": "diagnose",  # wiederholbar, wird zusammengefasst
}

SEX_MAP = {"1": "M", "2": "F", "3": "O"}


class BdtValidationError(Exception):
    pass


def parse_bdt(path):
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode(BDT_ENCODING)
    except UnicodeDecodeError:
        log.warning("Konnte %s nicht als %s dekodieren, versuche latin-1", path, BDT_ENCODING)
        text = raw.decode("latin-1", errors="replace")

    fields = {}
    diagnosen = []
    for line in text.splitlines():
        line = line.rstrip()
        if len(line) < 7:
            continue
        feld_id = line[3:7]
        value = line[7:].strip()
        key = FIELD_MAP.get(feld_id)
        if key is None:
            continue
        if key == "diagnose":
            if value:
                diagnosen.append(value)
        else:
            fields[key] = value
    if diagnosen:
        fields["diagnose"] = "; ".join(diagnosen)
    return fields


def format_dicom_date(bdt_date):
    """BDT-Datum TTMMJJJJ -> DICOM DA JJJJMMTT."""
    bdt_date = (bdt_date or "").strip()
    if len(bdt_date) != 8 or not bdt_date.isdigit():
        return None
    tag, monat, jahr = bdt_date[0:2], bdt_date[2:4], bdt_date[4:8]
    return f"{jahr}{monat}{tag}"


def format_dicom_datetime(bdt_datetime):
    """Erwartet JJJJMMTTHHMM (Erweiterungsfeld 6223) -> (DA, TM)."""
    bdt_datetime = (bdt_datetime or "").strip()
    if len(bdt_datetime) < 8 or not bdt_datetime[:8].isdigit():
        return None, None
    date_part = bdt_datetime[0:8]
    minutes_part = bdt_datetime[8:12] if len(bdt_datetime) >= 12 else ""
    time_part = f"{minutes_part}00" if minutes_part else None
    return date_part, time_part


def generate_uid():
    # UUID-abgeleitete OID gem. DICOM PS3.5 Annex B gehoert der Anwendung
    # selbst und benoetigt keine registrierte Root.
    return f"2.25.{uuid.uuid4().int}"


def build_mwl_item(fields):
    missing = [k for k in ("patient_id", "nachname") if not fields.get(k)]
    if missing:
        raise BdtValidationError(f"Pflichtfelder fehlen: {', '.join(missing)}")

    nachname = fields.get("nachname", "").strip()
    vorname = fields.get("vorname", "").strip()
    patient_name = f"{nachname}^{vorname}" if vorname else nachname

    birth_date = format_dicom_date(fields.get("geburtsdatum"))
    sex = SEX_MAP.get(fields.get("geschlecht", "").strip())

    accession = fields.get("auftragsnummer") or f"BDT{uuid.uuid4().hex[:12].upper()}"
    modality = fields.get("modalitaet") or DEFAULT_MODALITY
    station_aet = fields.get("stations_aet") or DEFAULT_STATION_AET
    sps_date, sps_time = format_dicom_datetime(fields.get("termin"))
    if sps_date is None:
        now = datetime.now()
        sps_date = now.strftime("%Y%m%d")
        sps_time = now.strftime("%H%M%S")
    description = fields.get("beschreibung") or fields.get("diagnose") or ""

    def tag(vr, value):
        return {"vr": vr, "Value": value}

    dataset = {
        "00080050": tag("SH", [accession]),  # AccessionNumber
        "00100010": tag("PN", [{"Alphabetic": patient_name}]),  # PatientName
        "00100020": tag("LO", [fields["patient_id"]]),  # PatientID
        "0020000D": tag("UI", [generate_uid()]),  # StudyInstanceUID
        "00321060": tag("LO", [description[:64]] if description else []),  # RequestedProcedureDescription
        "00401001": tag("SH", [accession]),  # RequestedProcedureID
        "00400100": {  # ScheduledProcedureStepSequence
            "vr": "SQ",
            "Value": [
                {
                    "00080060": tag("CS", [modality]),  # Modality
                    "00400001": tag("AE", [station_aet] if station_aet else []),  # ScheduledStationAETitle
                    "00400002": tag("DA", [sps_date]),  # ScheduledProcedureStepStartDate
                    "00400003": tag("TM", [sps_time] if sps_time else []),  # ScheduledProcedureStepStartTime
                    "00400007": tag("LO", [description[:64]] if description else []),  # SPS Description
                    "00400009": tag("SH", ["1"]),  # ScheduledProcedureStepID
                    "00400400": tag(
                        "LT", [fields["diagnose"][:10240]] if fields.get("diagnose") else []
                    ),  # CommentsOnScheduledProcedureStep
                }
            ],
        },
    }
    if birth_date:
        dataset["00100030"] = tag("DA", [birth_date])
    if sex:
        dataset["00100040"] = tag("CS", [sex])

    return dataset


def push_to_arc(dataset):
    url = f"{ARC_BASE_URL}/aets/{ARC_AE_TITLE}/rs/mwlitems"
    resp = requests.post(
        url,
        data=json.dumps(dataset).encode("utf-8"),
        headers={"Content-Type": "application/dicom+json"},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Archiv lehnte MWL-Eintrag ab ({resp.status_code}): {resp.text[:500]}")
    return resp


def move_with_timestamp(path, target_dir):
    os.makedirs(target_dir, exist_ok=True)
    base = os.path.basename(path)
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    target = os.path.join(target_dir, f"{ts}_{base}")
    shutil.move(path, target)
    return target


def process_file(path):
    log.info("Verarbeite %s", path)
    try:
        fields = parse_bdt(path)
        dataset = build_mwl_item(fields)
    except Exception as exc:
        # Datenproblem in der Datei selbst -> permanenter Fehler
        log.exception("Fehler beim Parsen von %s", path)
        target = move_with_timestamp(path, ERROR_DIR)
        with open(target + ".err", "w", encoding="utf-8") as f:
            f.write(str(exc))
        return

    try:
        push_to_arc(dataset)
    except requests.exceptions.RequestException as exc:
        # Archiv (noch) nicht erreichbar -> Datei belassen, naechster Zyklus versucht es erneut
        log.warning("Archiv nicht erreichbar, versuche %s spaeter erneut: %s", path, exc)
        return
    except Exception as exc:
        # Archiv hat den Datensatz inhaltlich abgelehnt -> permanenter Fehler
        log.exception("Archiv lehnte %s ab", path)
        target = move_with_timestamp(path, ERROR_DIR)
        with open(target + ".err", "w", encoding="utf-8") as f:
            f.write(str(exc))
        return

    target = move_with_timestamp(path, PROCESSED_DIR)
    log.info(
        "Worklist-Eintrag angelegt: Patient=%s Accession=%s -> %s",
        fields.get("patient_id"),
        dataset["00080050"]["Value"][0],
        target,
    )


def is_file_stable(path, wait=0.5):
    try:
        size1 = os.path.getsize(path)
        time.sleep(wait)
        size2 = os.path.getsize(path)
        return size1 == size2
    except OSError:
        return False


def main():
    os.makedirs(POLL_DIR, exist_ok=True)
    os.makedirs(PROCESSED_DIR, exist_ok=True)
    os.makedirs(ERROR_DIR, exist_ok=True)
    log.info("Ueberwache %s auf *.bdt Dateien (Intervall %ss)", POLL_DIR, POLL_INTERVAL)
    log.info("Ziel-Archiv: %s/aets/%s/rs/mwlitems", ARC_BASE_URL, ARC_AE_TITLE)

    while True:
        try:
            for name in sorted(os.listdir(POLL_DIR)):
                if not name.lower().endswith(".bdt"):
                    continue
                path = os.path.join(POLL_DIR, name)
                if not os.path.isfile(path) or not is_file_stable(path):
                    continue
                process_file(path)
        except Exception:
            log.exception("Fehler im Poll-Zyklus")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
