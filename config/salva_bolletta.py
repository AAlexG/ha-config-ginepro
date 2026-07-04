# salva_bolletta_4.py
# Legge mese selezionato e valori bolletta da HA DB, inietta statistiche mensili.
# Ricalcola inoltre:
#   input_number.elettricita_prezzo_kwh = somma € bollette elettriche / somma kWh bollette
#   input_number.gas_prezzo_mc          = somma € bollette gas / somma m³ bollette
# (solo mesi con entrambi i valori storicizzati > 0, per ciascuna utenza).
#
# Chiamato da shell_command.salva_bolletta tramite script HA (script.salva_bolletta).
# Log diagnostico scritto direttamente su /config/bolletta_log.txt (funzione log()).
# Su stdout viene stampato SOLO un JSON finale (una riga), letto dallo script HA
# tramite response_variable. In caso di errore, ok=False ed errore=<messaggio>,
# ma una riga JSON valida viene comunque sempre stampata.

import sqlite3, time, sys, json, traceback
from datetime import datetime, timezone

DB  = "/config/home-assistant_v2.db"
LOG = "/config/bolletta_log.txt"


def log(msg):
    with open(LOG, "a") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def get_state(cur, eid):
    cur.execute("""SELECT s.state FROM states s
                   JOIN states_meta m ON s.metadata_id = m.metadata_id
                   WHERE m.entity_id = ?
                   ORDER BY s.last_updated_ts DESC LIMIT 1""", (eid,))
    r = cur.fetchone()
    return r[0] if r else None


def get_or_create_meta(cur, sid, unit, unit_class, has_mean, has_sum):
    cur.execute("SELECT id FROM statistics_meta WHERE statistic_id=?", (sid,))
    r = cur.fetchone()
    if r:
        return r[0]
    cur.execute("""INSERT INTO statistics_meta
        (statistic_id, source, unit_of_measurement, unit_class,
         has_mean, has_sum, name, mean_type)
        VALUES (?, ?, ?, ?, ?, ?, NULL, 0)""",
        (sid, "recorder", unit, unit_class, has_mean, has_sum))
    mid = cur.lastrowid
    log(f"  [meta CREATO id={mid}] {sid}")
    return mid


def ins(cur, mid, ym, val, now_s, now_ts):
    y, m = map(int, ym.split("-"))
    t   = datetime(y, m, 1, tzinfo=timezone.utc).timestamp()
    utc = datetime(y, m, 1, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000")
    cur.execute("DELETE FROM statistics WHERE metadata_id=? AND start_ts=?", (mid, t))
    cur.execute("""INSERT INTO statistics
        (created, created_ts, metadata_id, start, start_ts,
         mean, mean_weight, min, max, last_reset, last_reset_ts, state, sum)
        VALUES (?,?,?,?,?,?,1,?,?,NULL,NULL,?,NULL)""",
        (now_s, now_ts, mid, utc, t, val, val, val, val))


def calcola_prezzo(cur, entity_qty, entity_euro, entity_prezzo_attuale):
    """Prezzo medio = somma € bollette / somma quantità bollette (kWh o m³).
    Generica per qualsiasi coppia (quantità, euro) storicizzata per mese."""
    row = cur.execute("""
        SELECT
            ROUND(COALESCE(SUM(euro.v), 0), 4) AS tot_euro,
            ROUND(COALESCE(SUM(qty.v), 0), 4)  AS tot_qty
        FROM (
            SELECT strftime('%Y-%m', datetime(start_ts,'unixepoch','localtime')) AS ym,
                   IFNULL(MAX(max), 0) AS v
            FROM statistics
            WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id=?)
            GROUP BY ym HAVING v > 0
        ) qty
        JOIN (
            SELECT strftime('%Y-%m', datetime(start_ts,'unixepoch','localtime')) AS ym,
                   IFNULL(MAX(max), 0) AS v
            FROM statistics
            WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id=?)
            GROUP BY ym HAVING v > 0
        ) euro ON euro.ym = qty.ym
    """, (entity_qty, entity_euro)).fetchone()
    tot_euro = float(row[0]) if row and row[0] is not None else 0.0
    tot_qty  = float(row[1]) if row and row[1] is not None else 0.0
    if tot_qty > 0:
        return round(tot_euro / tot_qty, 4), tot_euro, tot_qty
    # Fallback: nessuna bolletta valida ancora storicizzata,
    # mantiene il prezzo attuale invece di azzerarlo.
    attuale = get_state(cur, entity_prezzo_attuale)
    try:
        attuale = float(attuale) if attuale else 0.0
    except ValueError:
        attuale = 0.0
    return attuale, tot_euro, tot_qty


def main():
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()
    now_ts = time.time()
    now_s  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000")

    # ── Legge mese selezionato ──────────────────────────────────────────────
    mese_str = get_state(cur, "input_select.bolletta_mese_sel")
    if not mese_str or mese_str == "-- seleziona --":
        log("ERRORE: nessun mese selezionato in input_select.bolletta_mese_sel")
        conn.close()
        return {"ok": False, "errore": "nessun mese selezionato"}

    try:
        m_str, y_str = mese_str.split("/")
        ym = f"{y_str}-{m_str.zfill(2)}"
    except Exception:
        log(f"ERRORE: formato mese non riconosciuto: '{mese_str}' (atteso MM/YYYY)")
        conn.close()
        return {"ok": False, "errore": "formato mese non valido"}

    log(f"Bolletta {ym}")

    # ── Definizione entità: (entity_id, unit, unit_class, has_mean, has_sum, label)
    ENTITIES = [
        ("input_number.bolletta_elec_kwh",  "kWh", "energy", None, 1, "Elettricità kWh"),
        ("input_number.bolletta_elec_euro", "€",   None,     0,    1, "Elettricità €"  ),
        ("input_number.bolletta_gas_mc",    "m³",  "volume", None, 1, "Gas m³"         ),
        ("input_number.bolletta_gas_euro",  "€",   None,     0,    1, "Gas €"          ),
        ("input_number.bolletta_acqua_mc",  "m³",  "volume", None, 1, "Acqua m³"       ),
        ("input_number.bolletta_acqua_euro","€",   None,     0,    1, "Acqua €"        ),
        ("input_number.bolletta_gse_euro",  "€",   None,     0,    1, "GSE pagato €"   ),
    ]

    saved = 0
    for eid, unit, uc, hm, hs, label in ENTITIES:
        val_str = get_state(cur, eid)
        try:
            val = float(val_str) if val_str else 0.0
        except ValueError:
            val = 0.0
        if val <= 0:
            log(f"  {label}: 0 – saltato")
            continue
        mid = get_or_create_meta(cur, eid, unit, uc, hm, hs)
        ins(cur, mid, ym, val, now_s, now_ts)
        log(f"  {label}: {val} {unit}  [id={mid}]")
        saved += 1

    if saved > 0:
        conn.commit()
        log(f"OK – {saved} valori salvati per {ym}.")
    else:
        log("Nessun valore >0 – nulla scritto nel DB.")

    prezzo_kwh, tot_euro_elec, tot_kwh = calcola_prezzo(
        cur, "input_number.bolletta_elec_kwh", "input_number.bolletta_elec_euro",
        "input_number.elettricita_prezzo_kwh")
    log(f"Prezzo €/kWh ricalcolato: {prezzo_kwh} (tot €={tot_euro_elec}, tot kWh={tot_kwh})")

    prezzo_mc, tot_euro_gas, tot_mc = calcola_prezzo(
        cur, "input_number.bolletta_gas_mc", "input_number.bolletta_gas_euro",
        "input_number.gas_prezzo_mc")
    log(f"Prezzo €/m³ gas ricalcolato: {prezzo_mc} (tot €={tot_euro_gas}, tot m³={tot_mc})")

    conn.close()
    return {
        "ok": True,
        "mese": ym,
        "saved": saved,
        "prezzo_kwh": prezzo_kwh,
        "tot_euro_elec": tot_euro_elec,
        "tot_kwh": tot_kwh,
        "prezzo_mc_gas": prezzo_mc,
        "tot_euro_gas": tot_euro_gas,
        "tot_mc_gas": tot_mc,
    }


if __name__ == "__main__":
    try:
        out = main()
    except Exception as e:
        log(f"ERRORE FATALE: {e}\n{traceback.format_exc()}")
        out = {"ok": False, "errore": str(e)}
    print(json.dumps(out))
