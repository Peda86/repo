# PACS-Server auf Basis von dcm4chee-arc-light

Dieses Repository enthaelt eine Docker-Compose-Umgebung fuer einen vollstaendigen
PACS-Server auf Basis von [dcm4chee-arc-light](https://github.com/dcm4che/dcm4chee-arc-light):

- **DICOM-Archiv** (C-STORE, C-FIND, C-MOVE, QIDO-RS/WADO-RS/STOW-RS)
- **Modality Worklist Server (MWL)** fuer die Anbindung von Modalitaeten (CT, MRT, Roentgen, ...)
- **Web-UI** zur Konfiguration, Ueberwachung und Verwaltung von Studien/Worklist-Eintraegen
- **BDT-Bridge**: liest BDT-Dateien aus einem Ordner ein und legt daraus automatisch Worklist-Eintraege an (siehe [BDT-Schnittstelle](#bdt-schnittstelle-bdt-bridge))

> Hinweis: Bei dcm4chee-arc-light ist der Worklist-Server kein separater Dienst,
> sondern Teil des Archiv-Containers (`arc`). Archiv und MWL-SCP teilen sich
> dieselbe AE Title/denselben DICOM-Port sowie dieselbe Datenbank und
> Konfiguration - das ist die von dcm4che vorgesehene Standardarchitektur.

## Architektur

| Service | Image | Zweck |
|---|---|---|
| `ldap` | `dcm4che/slapd-dcm4chee` | Konfigurationsspeicher (Devices, AE Titles, Storage, Worklist-Regeln) |
| `db` | `dcm4che/postgres-dcm4chee` | PostgreSQL-Datenbank (Metadaten, Studien, Worklist-Eintraege) |
| `arc` | `dcm4che/dcm4chee-arc-psql` | Archiv-Anwendung: DICOM-Services, MWL-SCP, REST-API, Web-UI |
| `bdt-bridge` | eigenes Image (`./bdt-bridge`) | Liest BDT-Dateien aus `./bdt_poll` und legt MWL-Eintraege per REST-API im Archiv an |

## Voraussetzungen

- Docker Engine + Docker Compose Plugin
- Ausreichend Speicherplatz fuer DICOM-Bilddaten (Volume `arc_storage`)
- Freigegebene Ports (siehe unten) in Firewall/Netzwerk

## Schnellstart

```bash
cp .env.example .env
# .env anpassen: Passwoerter setzen, ARCHIVE_HOST auf die tatsaechliche
# Server-IP/den Hostnamen setzen, unter dem Modalitaeten den PACS erreichen.

docker compose up -d
```

Web-UI danach erreichbar unter: `http://<ARCHIVE_HOST>:8080/dcm4chee-arc/ui2`

Standard-Login des Archiv-UI: `admin` / `admin` (unbedingt nach dem ersten
Login in **Configuration > Users** aendern).

## Ports

| Port | Protokoll | Zweck |
|---|---|---|
| 8080 / 8443 | HTTP/HTTPS | Web-UI, REST-API (QIDO-RS/WADO-RS/STOW-RS) |
| 9990 / 9993 | HTTP/HTTPS | Wildfly-Admin-Konsole |
| 11112 | DICOM | C-STORE, C-FIND, C-MOVE **und** Modality Worklist C-FIND |
| 2762 | DICOM/TLS | wie 11112, aber TLS-verschluesselt |
| 2575 / 12575 | HL7 (MLLP) | HL7-Nachrichten, z.B. Order-Import aus RIS/KIS zur automatischen Worklist-Erzeugung |
| 389 / 636 | LDAP/LDAPS | Interner Konfigurationsspeicher (i.d.R. nicht extern freigeben) |
| 5432 | PostgreSQL | Datenbankzugriff (i.d.R. nicht extern freigeben) |

## Modalitaet an den PACS anbinden

1. Web-UI oeffnen -> **Configuration > Devices** -> neues Device fuer die
   Modalitaet anlegen (AE Title, Hostname/IP, Port).
2. In den Verbindungseinstellungen des Geraets (CT/MRT/Ultraschall) als
   PACS/Worklist-Server eintragen:
   - Host: `<ARCHIVE_HOST>`
   - Port: `11112`
   - Called AE Title: die im Archiv konfigurierte AE Title (Standard: `DCM4CHEE`)
   - Calling AE Title: die AE Title der Modalitaet (muss im Archiv als Device
     angelegt sein)
3. Worklist-Abfrage (C-FIND SOP Class "Modality Worklist Information Model")
   nutzt denselben Host/Port.

## Worklist-Eintrag anlegen (Test)

Ueber die Web-UI: **Monitoring > MWL** -> "Create new MWL Item".

Oder per Skript ueber das im Archiv-Container enthaltene `mkwl`-Tool:

```bash
./scripts/create-sample-mwl.sh <container-name> ACC123 PAT001
```

`<container-name>` ist der Name des laufenden `arc`-Containers, z.B. mit
`docker compose ps` ermittelbar.

## Test mit DICOM-Toolkit (dcm4che-tools / dcmtk)

```bash
# Worklist abfragen
findscu -c CTAET@<ARCHIVE_HOST>:11112 -W

# Bild speichern (C-STORE)
storescu -c DCM4CHEE@<ARCHIVE_HOST>:11112 pfad/zu/datei.dcm
```

## BDT-Schnittstelle (BDT-Bridge)

Der Dienst `bdt-bridge` ueberwacht den Ordner `./bdt_poll` (Bind-Mount,
liegt im Projektverzeichnis) und verarbeitet dort abgelegte `*.bdt`-Dateien
automatisch zu Modality-Worklist-Eintraegen im Archiv - z.B. als
Anbindung eines Praxisverwaltungssystems (PVS/KIS), das Auftraege als
BDT-Datei exportiert.

**Ablauf:**

1. Eine Praxissoftware (oder ein manueller Test) legt eine `.bdt`-Datei in
   `bdt_poll/` ab.
2. Die Bridge erkennt die Datei beim naechsten Poll-Zyklus (Standard: alle
   5 Sekunden, `BDT_POLL_INTERVAL_SECONDS`), wartet bis die Dateigroesse
   stabil ist (kein laufender Schreibvorgang mehr) und parst sie.
3. Aus den erkannten Feldern wird ein MWL-Datensatz gebaut und per
   `POST /dcm4chee-arc/aets/{AET}/rs/mwlitems` (DICOM+JSON) im Archiv
   angelegt.
4. Die Datei wird verschoben nach:
   - `bdt_poll/verarbeitet/` bei Erfolg,
   - `bdt_poll/fehler/` bei einem dauerhaften Fehler (z.B. fehlende
     Pflichtfelder oder vom Archiv inhaltlich abgelehnter Datensatz) -
     zusaetzlich wird eine `.err`-Datei mit der Fehlermeldung abgelegt.
   Ist das Archiv nur voruebergehend nicht erreichbar, bleibt die Datei im
   Poll-Ordner liegen und wird automatisch erneut versucht.

### Unterstuetzte BDT-Feldkennungen

BDT (Behandlungsdaten-Datentraeger) definiert selbst keine Felder fuer
bildgebende Auftraege (Modalitaet, Geraet, Termin) - dafuer nutzt die
Bridge zusaetzliche, frei belegbare Feldkennungen, die das exportierende
System mit ausgeben muss. Alle Zuordnungen lassen sich in
`bdt-bridge/bdt_bridge.py` (`FIELD_MAP`) an das jeweilige Quellsystem
anpassen.

| Feldkennung | Bedeutung | Pflicht |
|---|---|---|
| 3000 | Patienten-Nr. | ja |
| 3101 | Nachname | ja |
| 3102 | Vorname | nein |
| 3103 | Geburtsdatum (TTMMJJJJ) | nein |
| 3110 | Geschlecht (1=maennlich, 2=weiblich, 3=divers) | nein |
| 6220 *(Erweiterung)* | Auftragsnummer/Accession Number | nein (wird sonst generiert) |
| 6221 *(Erweiterung)* | Modalitaet (z.B. CT, MR, CR) | nein (Default: `DEFAULT_MODALITY`) |
| 6222 *(Erweiterung)* | Ziel-AE-Title der Modalitaet | nein (Default: `DEFAULT_STATION_AET`) |
| 6223 *(Erweiterung)* | Termin, Format JJJJMMTTHHMM | nein (Default: jetzt) |
| 6224 *(Erweiterung)* | Beschreibung der Untersuchung | nein |
| 6200 | Diagnose (wiederholbar) | nein |

Eine Beispieldatei liegt unter `bdt-bridge/examples/beispiel.bdt`.

### Konfiguration (.env)

| Variable | Standard | Bedeutung |
|---|---|---|
| `ARC_AE_TITLE` | `DCM4CHEE` | AE Title, unter der die MWL-Eintraege im Archiv angelegt werden |
| `DEFAULT_MODALITY` | `OT` | Modalitaet, falls Feld 6221 fehlt |
| `DEFAULT_STATION_AET` | *(leer)* | Ziel-AE-Title, falls Feld 6222 fehlt |
| `BDT_ENCODING` | `cp850` | Zeichensatz der BDT-Dateien (Fallback: `latin-1`) |
| `BDT_POLL_INTERVAL_SECONDS` | `5` | Abstand zwischen zwei Ordner-Scans |

### Testen

```bash
docker compose up -d --build bdt-bridge
cp bdt-bridge/examples/beispiel.bdt bdt_poll/
docker compose logs -f bdt-bridge
```

Danach in der Web-UI unter **Monitoring > MWL** pruefen, oder per
DICOM-Toolkit abfragen:

```bash
findscu -c CTAET@<ARCHIVE_HOST>:11112 -W
```

## Datenpersistenz

Alle Daten liegen in benannten Docker-Volumes (`db_data`, `arc_storage`,
`ldap_data`, `ldap_confdata`, `arc_wildfly`) und bleiben bei
`docker compose down` erhalten. Fuer ein vollstaendiges Zuruecksetzen:

```bash
docker compose down -v
```

## Sicherheitshinweise

- Alle Standardpasswoerter in `.env` **vor** dem produktiven Einsatz aendern.
- LDAP- (389/636) und PostgreSQL-Port (5432) sollten nicht ins oeffentliche
  Netz exponiert werden - im produktiven Betrieb ggf. aus dem `ports:`-Mapping
  entfernen und nur intern (Docker-Netzwerk) nutzen.
- Fuer produktive Umgebungen TLS fuer DICOM (Port 2762) und HTTPS (Port 8443)
  mit eigenen Zertifikaten konfigurieren.
- Patientendaten (PHI) unterliegen Datenschutzbestimmungen (z.B. DSGVO) -
  Zugriffskontrollen, Backups und Verschluesselung entsprechend einrichten.
