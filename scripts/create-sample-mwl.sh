#!/usr/bin/env bash
# Legt einen Beispiel-Eintrag im Modality Worklist Server (MWL) an,
# um die Worklist-Funktion des dcm4chee-arc PACS zu testen.
#
# Nutzt das im Archiv-Container enthaltene "mkwl"-Tool.
set -euo pipefail

CONTAINER="${1:-repo-arc-1}"
ACCESSION_NO="${2:-ACC$(date +%s)}"
PATIENT_ID="${3:-PAT001}"

docker exec "$CONTAINER" /opt/wildfly/bin/mkwl.sh \
  --pid "$PATIENT_ID" \
  --issuer "TEST" \
  --pn "Mustermann^Max" \
  --accno "$ACCESSION_NO" \
  --sps-id "SPS1" \
  --sps-status SCHEDULED \
  --modality CT \
  --station-aet CTAET \
  --start-date "$(date +%Y%m%d%H%M)" \
  DUMMY.wl

echo "Worklist-Eintrag angelegt: Patient=$PATIENT_ID Accession=$ACCESSION_NO"
