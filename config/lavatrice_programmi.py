#!/usr/bin/env python3
# lavatrice_programmi_4.py
# Conta i cicli di lavaggio della Miele WCI870 per programma e per mese.
#
# PERCHE' ESISTE
# sensor.lavatricemielewci870_programma e' testuale: non genera statistiche a
# lungo termine e la tabella states viene ripulita dopo 7 giorni
# (recorder purge_keep_days: 7). Senza un accumulo esterno lo storico dei
# programmi sparisce. Questo script legge il DB, registra i cicli conclusi e
# ne ricava la tabella mensile.
#
# COSA E' CAMBIATO IN v4 (dopo un totale sceso da 15 a 14)
# Le v1-v3 tenevano in archivio solo i totali e li incrementavano. Due difetti:
#   - nessun lock: due esecuzioni sovrapposte leggevano lo stesso archivio,
#     incrementavano entrambe e l'ultima a scrivere cancellava l'incremento
#     dell'altra. Un lavaggio perso, senza nessuna traccia.
#   - archivio illeggibile trattato come archivio vuoto: lo script ripartiva
#     da zero in silenzio, sovrascrivendo la storia con i soli cicli ancora
#     presenti nel recorder.
# Ora l'archivio e' un REGISTRO DEI SINGOLI CICLI (data, ora, programma) e i
# totali vengono RICALCOLATI dal registro a ogni esecuzione. Un totale
# derivato non puo' scendere per una scrittura andata male, ogni numero e'
# verificabile riga per riga, e un ciclo identificato dal suo timestamp non
# puo' essere registrato due volte nemmeno a esecuzioni sovrapposte.
#
# COME CONTA I CICLI
# Un ciclo = una transizione a "program_ended" di sensor.lavatricemielewci870.
# Verificato sui dati reali: ogni program_ended nel DB ha "in_use" prima e
# "off" dopo. Le disconnessioni della lavatrice (not_connected) avvengono a
# macchina spenta e non generano falsi cicli.
#
# ETICHETTA DEL PROGRAMMA
# Si cerca l'ultimo valore valido del sensore programma DENTRO il ciclo, cioe'
# tra l'inizio del ciclo e il program_ended. Se non si trova nulla si allarga
# la ricerca fino al ciclo concluso precedente, mai oltre: cosi' non e'
# possibile attribuire a un lavaggio il programma di quello prima. Se non c'e'
# nessun valore utile: "sconosciuto", che e' un'informazione, non un errore.
#
# OUTPUT
#   /config/lavatrice_programmi.json  -> registro dei cicli (scritto qui)
#   stdout                            -> tabella per il sensore command_line

import sqlite3, json, os, sys, fcntl, datetime

DB    = "/config/home-assistant_v2.db"
STORE = "/config/lavatrice_programmi.json"
LOCK  = "/config/lavatrice_programmi.lock"

STATO = "sensor.lavatricemielewci870"
PROG  = "sensor.lavatricemielewci870_programma"

MESI_TABELLA = 12   # colonne mostrate nella plancia

# Stati che fanno parte di un ciclo in corso: risalendo da program_ended,
# l'inizio del ciclo e' il primo stato che NON e' in questo insieme.
IN_CICLO = {"in_use", "programmed", "on", "pause", "waiting_to_start",
            "rinse_hold", "not_connected", "unavailable", "unknown"}

# not_connected incluso: nelle v1-v3 mancava e poteva essere preso per un
# nome di programma.
IGNORA = {"no_program", "not_connected", "unknown", "unavailable", "none", ""}

# Nomi dei programmi presi dal libretto d'uso ufficiale italiano della
# WCI870 WCS (Miele M.-Nr. 11 362 430, cap. "Elenco programmi", pag. 43-49).
# Sono mappate SOLO le chiavi il cui nome italiano e' confermato dal
# libretto: tutte le altre restano in inglese, come arrivano dall'API Miele.
# Meglio una riga con scritto "powerfresh" che un nome inventato.
#
# Tre chiavi risolte con l'aiuto di Alex, che ha riconosciuto i cicli:
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
# Cambiare un nome qui NON altera il registro: nel JSON resta la chiave Miele.
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


def prendi_lock():
    # Se un'altra istanza sta girando questa esce senza scrivere niente.
    # Il file resta aperto per tutta la durata del processo.
    f = open(LOCK, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Un'altra esecuzione e' in corso: esco senza scrivere.",
              file=sys.stderr)
        sys.exit(0)
    return f


def carica_registro():
    # File assente = primo avvio, si parte da registro vuoto.
    # File presente ma illeggibile o di formato sbagliato = ERRORE: si esce
    # senza scrivere. Mai ripartire da zero in silenzio.
    if not os.path.exists(STORE):
        return []
    with open(STORE, encoding="utf-8") as f:
        d = json.load(f)          # un JSON rotto solleva e interrompe tutto
    if not isinstance(d, dict) or "cicli" not in d:
        raise ValueError(
            f"{STORE} non e' un registro cicli (formato vecchio v1-v3). "
            "Rinominarlo o eliminarlo per ricominciare.")
    if not isinstance(d["cicli"], list):
        raise ValueError(f"{STORE}: la chiave 'cicli' non e' una lista.")
    return d["cicli"]


def salva_registro(cicli):
    cicli = sorted(cicli, key=lambda c: c["ts"])
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"cicli": cicli}, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STORE)        # sostituzione atomica


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


def inizio_ciclo(st_hist, i):
    # Risale dal program_ended in posizione i fino al primo stato che non fa
    # parte del ciclo (tipicamente "off"), e ritorna il ts del primo stato
    # appartenente al ciclo.
    j = i
    while j > 0 and st_hist[j - 1][1] in IN_CICLO:
        j -= 1
    return st_hist[j][0]


def etichetta(pr_hist, da_ts, a_ts):
    scelto = None
    for pts, pst in pr_hist:
        if pts > a_ts:
            break
        if pts >= da_ts and pst not in IGNORA:
            scelto = pst
    return scelto


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
    lock = prendi_lock()
    cicli = carica_registro()
    noti = {int(c["ts"]) for c in cicli}   # il secondo intero identifica il ciclo

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.cursor()
    st_hist = storia(cur, STATO)
    pr_hist = storia(cur, PROG)
    con.close()

    fine_precedente = 0.0
    nuovi = 0
    for i, (ts, st) in enumerate(st_hist):
        if st != "program_ended":
            continue
        if int(ts) not in noti:
            avvio = inizio_ciclo(st_hist, i)
            prog = etichetta(pr_hist, avvio, ts)
            if prog is None:
                # Niente dentro il ciclo: si allarga fino al ciclo concluso
                # precedente, mai oltre.
                prog = etichetta(pr_hist, fine_precedente, ts) or "sconosciuto"
            cicli.append({
                "ts": ts,
                "data": datetime.datetime.fromtimestamp(ts)
                                         .strftime("%Y-%m-%d %H:%M:%S"),
                "programma": prog,
            })
            noti.add(int(ts))
            nuovi += 1
        fine_precedente = ts

    if nuovi:
        salva_registro(cicli)
        print(f"Registrati {nuovi} nuovi cicli.", file=sys.stderr)

    # --- tabella ricalcolata dal registro, non incrementata ---
    conteggi = {}
    for c in cicli:
        mese = c["data"][:7]
        conteggi.setdefault(mese, {})
        conteggi[mese][c["programma"]] = conteggi[mese].get(c["programma"], 0) + 1

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
    lock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        sys.exit(1)