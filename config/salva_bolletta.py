# salva_bolletta_9.py
# Legge mese selezionato e valori bolletta da HA DB, inietta statistiche mensili.
# Ricalcola inoltre:
#   prezzo MEDIO elettricità = somma € bollette elettriche / somma kWh bollette
#   prezzo MEDIO gas         = somma € bollette gas / somma m³ bollette
# I prezzi medi (quote fisse incluse) restano SOLO nel log come dato
# di controllo: nessun input_number viene aggiornato (salva_bolletta_8,
# helper medi rimossi in configuration_159). I prezzi A CONSUMO
# (input_number.elettricita_prezzo_kwh, aggiornato dal PUN, e
# input_number.gas_prezzo_mc, manuale) NON vengono toccati.
# (solo mesi con entrambi i valori storicizzati > 0, per ciascuna utenza).
#
# NUOVO (v5): ricalcola il prezzo €/kWh pagato dal GSE per OGNI mese con
# pagamento storicizzato:
#   prezzo(mese) = bolletta_gse_euro(mese) / kWh a rete(mese)
# I kWh a rete mensili vengono letti dalle statistiche di sensor.a_rete_mese
# (Solarman, contatore mensile). Le statistiche esistenti dello statistic_id
# sensor.gse_prezzo_kwh vengono cancellate e reinserite integralmente ad ogni
# esecuzione: i mesi passati si correggono retroattivamente. Il sensore
# template omonimo è stato rimosso da configuration.yaml.
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


def calcola_prezzo(cur, entity_qty, entity_euro):
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
    # Fallback: nessuna bolletta valida ancora storicizzata.
    # (salva_bolletta_8: il prezzo medio è solo informativo nel log,
    # nessun helper da preservare -> 0.)
    return 0.0, tot_euro, tot_qty


def valori_mensili(cur, sid):
    """Restituisce dict {YYYY-MM: valore} dalle statistiche di uno statistic_id.
    Usa MAX(max) se disponibile (sensori measurement / statistiche iniettate),
    altrimenti MAX(state) (contatori total_increasing a reset mensile,
    come sensor.a_rete_mese di Solarman)."""
    rows = cur.execute("""
        SELECT strftime('%Y-%m', datetime(start_ts,'unixepoch','localtime')) AS ym,
               IFNULL(MAX(max), MAX(state)) AS v
        FROM statistics
        WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id=?)
        GROUP BY ym
    """, (sid,)).fetchall()
    out = {}
    for ym, v in rows:
        try:
            out[ym] = float(v)
        except (TypeError, ValueError):
            pass
    return out


def calcola_prezzi_gse(cur, now_s, now_ts):
    """Per ogni mese con pagamento GSE storicizzato > 0, calcola
    prezzo = € GSE / kWh a rete del mese e inietta la statistica mensile
    sotto sensor.gse_prezzo_kwh. Cancella prima TUTTE le statistiche
    esistenti di quello statistic_id (long e short term), così i valori
    residui del vecchio sensore template non inquinano i grafici."""
    euro = valori_mensili(cur, "input_number.bolletta_gse_euro")
    kwh  = valori_mensili(cur, "sensor.a_rete_mese")

    mid = get_or_create_meta(cur, "sensor.gse_prezzo_kwh", "€/kWh", None, 1, 0)
    cur.execute("DELETE FROM statistics WHERE metadata_id=?", (mid,))
    cur.execute("DELETE FROM statistics_short_term WHERE metadata_id=?", (mid,))

    inseriti = {}
    for ym in sorted(euro):
        pagato = euro[ym]
        rete   = kwh.get(ym, 0.0)
        if pagato <= 0:
            continue
        if rete < 0.1:
            log(f"  GSE {ym}: pagato {pagato} € ma kWh a rete = {rete} – saltato")
            continue
        prezzo = round(pagato / rete, 4)
        ins(cur, mid, ym, prezzo, now_s, now_ts)
        inseriti[ym] = prezzo
        log(f"  GSE {ym}: {pagato} € / {rete} kWh = {prezzo} €/kWh")
    return inseriti


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
        ("input_number.bolletta_elec_kwh",  "kWh", "energy", None, 0, "Elettricità kWh"),
        ("input_number.bolletta_elec_euro", "€",   None,     0,    0, "Elettricità €"  ),
        ("input_number.bolletta_gas_mc",    "m³",  "volume", None, 0, "Gas m³"         ),
        ("input_number.bolletta_gas_euro",  "€",   None,     0,    0, "Gas €"          ),
        ("input_number.bolletta_acqua_mc",  "m³",  "volume", None, 0, "Acqua m³"       ),
        ("input_number.bolletta_acqua_euro","€",   None,     0,    0, "Acqua €"        ),
        ("input_number.bolletta_gse_euro",  "€",   None,     0,    0, "GSE pagato €"   ),
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
        cur, "input_number.bolletta_elec_kwh", "input_number.bolletta_elec_euro")
    log(f"Prezzo medio €/kWh ricalcolato: {prezzo_kwh} (tot €={tot_euro_elec}, tot kWh={tot_kwh})")

    prezzo_mc, tot_euro_gas, tot_mc = calcola_prezzo(
        cur, "input_number.bolletta_gas_mc", "input_number.bolletta_gas_euro")
    log(f"Prezzo medio €/m³ gas ricalcolato: {prezzo_mc} (tot €={tot_euro_gas}, tot m³={tot_mc})")

    prezzi_gse = calcola_prezzi_gse(cur, now_s, now_ts)
    conn.commit()
    log(f"Prezzi GSE ricalcolati per {len(prezzi_gse)} mesi: {prezzi_gse}")

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
        "prezzi_gse": prezzi_gse,
        # NUOVO (v9): prezzo GSE dell'ultimo mese disponibile, usato
        # dallo script HA per aggiornare input_number.gse_prezzo_kwh_stima.
        "gse_prezzo_ultimo": (prezzi_gse[sorted(prezzi_gse)[-1]]
                              if prezzi_gse else 0),
    }


if __name__ == "__main__":
    try:
        out = main()
    except Exception as e:
        log(f"ERRORE FATALE: {e}\n{traceback.format_exc()}")
        out = {"ok": False, "errore": str(e)}
    print(json.dumps(out))