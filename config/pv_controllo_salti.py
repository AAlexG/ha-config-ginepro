#!/usr/bin/env python3
# pv_controllo_salti_1.py
# Sorveglianza della coerenza delle statistiche dell'inverter UFLEX.
#
# Motivazione (incidente maggio-giugno 2026):
#   Il 09/05 e il 01/06 i contatori interni dell'inverter si sono azzerati:
#   il campo sum delle statistiche di export, import, carica e scarica e'
#   tornato indietro. Tra i due episodi, dal 10/05 al 31/05, gli stessi
#   quattro sensori sono rimasti congelati sullo stesso valore per 22 giorni
#   mentre la produzione continuava a registrare regolarmente. La tabella
#   mensile ha mostrato giugno a zero e maggio sottostimato per tre mesi
#   prima che qualcuno se ne accorgesse.
#
# Due criteri, complementari:
#
#   A. CALO. Il campo sum di un contatore cumulativo non puo' diminuire.
#      Qualsiasi calo e' un azzeramento. Intercetta gli episodi del 09/05
#      e del 01/06.
#
#   B. CONGELAMENTO. Delta giornaliero esattamente nullo su tutti e quattro
#      i sensori di scambio nello stesso giorno, mentre la produzione supera
#      1 kWh. Se l'impianto produce e la casa non preleva, non immette, non
#      carica e non scarica per 24 ore, il dato e' fermo. Se invece l'inverter
#      e' spento anche la produzione e' a zero e il giorno viene escluso da
#      solo. Intercetta il congelamento del 10-31/05.
#      La condizione su tutti e quattro insieme e' necessaria: un singolo
#      sensore a zero puo' essere vero (export nullo in una giornata coperta,
#      import nullo in una notte coperta dalla batteria, carica e scarica
#      nulle a BMS aperto, come in agosto).
#
# Perche' servono entrambi: un congelamento non produce cali, la serie resta
# piatta e monotona; un azzeramento non produce delta nulli, il giorno del
# gradino ha delta negativo.
#
# Validazione sui dati reali (01/09/2026):
#   criterio B su 183 giorni di statistiche corrette -> 0 rilevamenti
#   criterio B sulla tabella bkp_fix_maggio (valori originali, pre-correzione)
#   -> 22 rilevamenti, dal 10/05 al 31/05, esattamente il periodo congelato.
#   Scartato un terzo criterio provato prima ("consumo giornaliero ricostruito
#   pari a zero"): durante il congelamento l'export giornaliero e' nullo,
#   quindi l'autoconsumo calcolato coincide con l'intera produzione e il
#   consumo risulta alto, non nullo. Il criterio non scattava mai.
#
# Finestra di analisi: ultimi 7 giorni completi. Il giorno corrente e' escluso
# perche' incompleto per definizione.
#
# Output: JSON su stdout, letto dal command_line sensor sensor.pv_controllo_salti
#   anomalie   = numero di anomalie trovate (0 = tutto regolare, -1 = errore)
#   dettagli   = elenco con tipo, sensore, data e ampiezza di ciascuna
#   aggiornato = data e ora dell'ultima esecuzione
# In caso di errore lo stato vale -1 e il campo errore contiene il messaggio:
# l'entita' resta valida e il guasto e' visibile, invece di sparire come
# "non disponibile".

import sqlite3
import json
import sys
from datetime import date, datetime, timedelta

DB = "/config/home-assistant_v2.db"

GIORNI_FINESTRA = 7
SOGLIA_CALO = 0.01        # kWh, tolleranza sul rumore di arrotondamento
SOGLIA_FERMO = 0.001      # kWh, sotto questa soglia il delta e' considerato nullo
SOGLIA_PRODUZIONE = 1.0   # kWh, produzione minima perche' il giorno sia valutabile

PRODUZIONE = "sensor.inverter_uflex_today_production"

SCAMBIO = {
    "export":  "sensor.inverter_uflex_today_energy_export",
    "import":  "sensor.inverter_uflex_today_energy_import",
    "carica":  "sensor.inverter_uflex_total_battery_charge",
    "scarica": "sensor.inverter_uflex_total_battery_discharge",
}


def get_mid(cur, statistic_id):
    r = cur.execute(
        "SELECT id FROM statistics_meta WHERE statistic_id=?", (statistic_id,)
    ).fetchone()
    return r[0] if r else None


def cerca_cali(cur, mid, da_ts):
    # Legge anche il giorno precedente alla finestra: un calo avvenuto sul
    # confine non sarebbe visibile senza la riga che lo precede.
    rows = cur.execute("""
        SELECT start_ts, sum FROM statistics
        WHERE metadata_id=? AND sum IS NOT NULL AND start_ts >= ?
        ORDER BY start_ts
    """, (mid, da_ts - 86400)).fetchall()

    fuori = []
    prec = None
    for ts, v in rows:
        if prec is not None and v < prec - SOGLIA_CALO and ts >= da_ts:
            fuori.append((ts, prec - v))
        prec = v
    return fuori


def delta_giornalieri(cur, mid, da_ts):
    # Ultimo valore di sum per ogni giorno, poi differenza con il giorno
    # precedente. Parte due giorni prima della finestra perche' il delta del
    # primo giorno utile ha bisogno della base del giorno che lo precede.
    massimi = {}
    for d, m in cur.execute("""
        SELECT date(datetime(start_ts,'unixepoch','localtime')), MAX(sum)
        FROM statistics
        WHERE metadata_id=? AND sum IS NOT NULL AND start_ts >= ?
        GROUP BY 1 ORDER BY 1
    """, (mid, da_ts - 172800)):
        massimi[date.fromisoformat(d)] = m

    out = {}
    prec = None
    for g in sorted(massimi):
        if prec is not None:
            out[g] = massimi[g] - massimi[prec]
        prec = g
    return out


def main():
    try:
        oggi = date.today()
        inizio = oggi - timedelta(days=GIORNI_FINESTRA)
        da_ts = datetime(inizio.year, inizio.month, inizio.day).timestamp()

        conn = sqlite3.connect(DB, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()

        tutti = dict(SCAMBIO)
        tutti["produzione"] = PRODUZIONE
        mid = {k: get_mid(cur, v) for k, v in tutti.items()}
        dettagli = []

        # --- Criterio A: cali del campo sum ---------------------------------
        for nome, m in mid.items():
            if m is None:
                dettagli.append({
                    "tipo": "sensore assente",
                    "sensore": nome,
                    "quando": oggi.strftime("%d/%m/%Y"),
                    "valore": 0.0,
                })
                continue
            for ts, ampiezza in cerca_cali(cur, m, da_ts):
                dettagli.append({
                    "tipo": "calo",
                    "sensore": nome,
                    "quando": datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M"),
                    "valore": round(ampiezza, 1),
                })

        # --- Criterio B: congelamento ---------------------------------------
        if all(mid[k] is not None for k in tutti):
            d = {k: delta_giornalieri(cur, mid[k], da_ts) for k in tutti}
            comuni = set.intersection(*(set(d[k]) for k in SCAMBIO))
            for g in sorted(comuni):
                if g < inizio or g >= oggi:
                    continue
                if d["produzione"].get(g, 0.0) <= SOGLIA_PRODUZIONE:
                    continue
                if all(abs(d[k][g]) < SOGLIA_FERMO for k in SCAMBIO):
                    dettagli.append({
                        "tipo": "congelamento",
                        "sensore": "scambio",
                        "quando": g.strftime("%d/%m/%Y"),
                        "valore": round(d["produzione"].get(g, 0.0), 1),
                    })

        conn.close()

        print(json.dumps({
            "anomalie": len(dettagli),
            "dettagli": dettagli,
            "aggiornato": datetime.now().strftime("%d/%m/%Y %H:%M"),
        }, ensure_ascii=False))

    except Exception as e:
        print(json.dumps({
            "anomalie": -1,
            "dettagli": [],
            "errore": str(e),
            "aggiornato": datetime.now().strftime("%d/%m/%Y %H:%M"),
        }, ensure_ascii=False))
        print(f"ERRORE: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
