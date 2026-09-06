#!/usr/bin/env python3
# lavatrice_programmi_3.py
# Conta i cicli di lavaggio della Miele WCI870 per programma e per mese.
#
# PERCHE' ESISTE
# sensor.lavatricemielewci870_programma e' testuale: non genera statistiche a
# lungo termine e la tabella states viene ripulita dopo 7 giorni
# (recorder purge_keep_days: 7). Senza un accumulo esterno lo storico dei
# programmi sparisce. Questo script legge il DB, conta i cicli conclusi e li
# somma in un file JSON cumulativo che non dipende piu' dal recorder.
#
# COME CONTA I CICLI
# Un ciclo = una transizione a "program_ended" di sensor.lavatricemielewci870.
# Verificato sui dati reali (01-05/09/2026): 6 cicli, 6 program_ended, nessun
# falso positivo. I rimbalzi off/on di pochi secondi che il sensore programma
# produce a inizio ciclo NON generano program_ended, quindi non serve nessuna
# soglia anti-rimbalzo.
#
# ETICHETTA DEL PROGRAMMA
# Al momento del program_ended il sensore programma vale ancora il programma
# usato: passa a no_program solo all'apertura dello sportello, minuti dopo.
# Se comunque risultasse no_program/unknown/unavailable, si risale all'ultimo
# valore valido entro le 24 ore precedenti. Se non c'e' nulla: "sconosciuto".
#
# INCREMENTALITA'
# Nel JSON cumulativo viene salvato il timestamp dell'ultimo ciclo conteggiato.
# Le esecuzioni successive elaborano solo i cicli piu' recenti: rilanciare lo
# script dieci volte di fila non produce doppi conteggi.
#
# OUTPUT
#   /config/lavatrice_programmi.json  -> archivio cumulativo (scritto qui)
#   stdout                            -> tabella pronta per il sensore
#                                        command_line (redirezione nello
#                                        shell_command)

import sqlite3, json, os, sys, datetime

DB    = "/config/home-assistant_v2.db"
STORE = "/config/lavatrice_programmi.json"

STATO = "sensor.lavatricemielewci870"
PROG  = "sensor.lavatricemielewci870_programma"

MESI_TABELLA  = 12          # colonne mostrate nella plancia
FINESTRA_PROG = 24 * 3600   # quanto indietro cercare l'etichetta programma

IGNORA = {"no_program", "unknown", "unavailable", "none", ""}

# Nomi dei programmi presi dal libretto d'uso ufficiale italiano della
# WCI870 WCS (Miele M.-Nr. 11 362 430, cap. "Elenco programmi", pag. 43-49).
# Sono mappate SOLO le chiavi il cui nome italiano e' confermato dal
# libretto: tutte le altre restano in inglese, come arrivano dall'API Miele.
# Meglio una riga con scritto "powerfresh" che un nome inventato.
#
# Tre chiavi risolte con l'aiuto di Alex, che ha riconosciuto i due cicli
# di settembre (v3):
#   down_filled_items -> Piumoni. Il libretto descrive Piumoni come
#     "Giacche, sacchi a pelo, cuscini e altri capi con imbottitura in
#     piuma": e' la traduzione esatta di "down filled items".
#   down_duvets -> Trapunte & Piumini, per esclusione, coerente con la
#     descrizione del libretto ("Trapunte e cuscini in piuma o piumino").
#   outerwear -> Capi per esterno. ATTENZIONE: questo nome NON e' nel
#     libretto, e' la descrizione data da Alex del ciclo eseguito. Il
#     libretto ha un solo programma per l'esterno, "Capi outdoor", che pero'
#     corrisponde alla chiave outdoor_garments. Non verificato.
#
# Cambiare un nome qui NON altera i conteggi: nell'archivio
# /config/lavatrice_programmi.json resta sempre la chiave Miele.
NOMI = {
    "cottons":               "Cotone",
    "cottons_eco":           "Cotone eco",
    "easy_care":             "Lava/Indossa",
    "minimum_iron":          "Lava/Indossa",
    "delicates":             "Delicati",
    "woollens":              "Lana",
    "silks":                 "Seta",
    "shirts":                "Camicie",
    "quick_power_wash":      "QuickPowerWash",
    "denim":                 "Jeans/Scuri",
    "dark_jeans":            "Jeans/Scuri",
    "dark_garments":         "Jeans/Scuri",
    "eco_40_60":             "ECO 40-60",
    "proofing":              "Impermeabilizzare",
    "outdoor_garments":      "Capi outdoor",
    "express_20":            "Express 20'",
    "sportswear":            "Capi sport",
    "automatic_plus":        "Automatic plus",
    "pillows":               "Cuscini",
    "curtains":              "Tende",
    "down_filled_items":     "Piumoni",
    "down_duvets":           "Trapunte & Piumini",
    "outerwear":             "Capi per esterno",
    "first_wash":            "Biancheria nuova",
    "separate_rinse_starch": "Solo risciacquo/Inamidare",
    "drain_spin":            "Scarico/Centrifuga",
    "clean_machine":         "Pulizia macchina",
    "sconosciuto":           "Sconosciuto",
}



def carica_store():
    try:
        with open(STORE, encoding="utf-8") as f:
            d = json.load(f)
        return float(d.get("ultimo_ts", 0)), d.get("conteggi", {})
    except (FileNotFoundError, ValueError, TypeError):
        return 0.0, {}


def salva_store(ultimo_ts, conteggi):
    # Scrittura atomica: un'interruzione a meta' non puo' corrompere
    # l'archivio, che a quel punto sarebbe irrecuperabile.
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ultimo_ts": ultimo_ts, "conteggi": conteggi},
                  f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, STORE)


def storia(cur, entity_id):
    # Ritorna [(ts, stato)] con i soli cambi di stato reali: le righe
    # duplicate generate dai soli cambi di attributo vengono scartate.
    rows = cur.execute("""
        SELECT s.last_updated_ts, s.state
        FROM states s
        JOIN states_meta m ON s.metadata_id = m.metadata_id
        WHERE m.entity_id = ? AND s.last_updated_ts IS NOT NULL
        ORDER BY s.last_updated_ts
    """, (entity_id,)).fetchall()
    out, prec = [], None
    for ts, st in rows:
        if st == prec:
            continue
        prec = st
        out.append((ts, st))
    return out


def etichetta(prog_hist, ts):
    scelto = None
    for pts, pst in prog_hist:
        if pts > ts:
            break
        if pst in IGNORA:
            continue
        if ts - pts <= FINESTRA_PROG:
            scelto = pst
    return scelto or "sconosciuto"


def elenco_mesi(n):
    oggi = datetime.date.today()
    anno, mese = oggi.year, oggi.month
    out = []
    for _ in range(n):
        out.append(f"{anno:04d}-{mese:02d}")
        mese -= 1
        if mese == 0:
            anno, mese = anno - 1, 12
    return out


def main():
    ultimo, conteggi = carica_store()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.cursor()
    st_hist = storia(cur, STATO)
    pr_hist = storia(cur, PROG)
    con.close()

    nuovo_ultimo = ultimo
    for ts, st in st_hist:
        if st != "program_ended" or ts <= ultimo:
            continue
        prog = etichetta(pr_hist, ts)
        mese = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m")
        conteggi.setdefault(mese, {})
        conteggi[mese][prog] = conteggi[mese].get(prog, 0) + 1
        if ts > nuovo_ultimo:
            nuovo_ultimo = ts

    salva_store(nuovo_ultimo, conteggi)

    # --- tabella per la plancia ---
    mesi = elenco_mesi(MESI_TABELLA)

    programmi = set()
    for m in conteggi.values():
        programmi.update(m.keys())

    righe = []
    for p in programmi:
        # Il totale e' da sempre, non solo sui mesi mostrati: se un
        # programma esce dalla finestra di 12 mesi il suo conteggio storico
        # resta visibile nella colonna Totale.
        totale = sum(conteggi[m].get(p, 0) for m in conteggi)
        valori = [conteggi.get(m, {}).get(p, 0) for m in mesi]
        righe.append({
            "programma": NOMI.get(p, p),
            "totale":    totale,
            "valori":    valori,
        })

    righe.sort(key=lambda r: (-r["totale"], r["programma"]))

    totali = {
        "programma": "TOTALE",
        "totale":    sum(r["totale"] for r in righe),
        "valori":    [sum(r["valori"][i] for r in righe) for i in range(len(mesi))],
    }

    etichette = [f"{m[5:7]}/{m[2:4]}" for m in mesi]

    print(json.dumps({"mesi": etichette, "righe": righe, "totali": totali},
                     ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        sys.exit(1)
