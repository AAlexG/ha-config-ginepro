#!/usr/bin/env python3
# totali_consumi_12.py
# Calcola totali storici rilevato/bollette dalla tabella statistics del DB HA.
# Stampa un JSON su stdout con i valori, letto dall'automazione
# calcola_totali_consumi che aggiorna gli helper input_number della plancia.
#
# CORREZIONI v12 (confronto rilevato/bollette sui SOLI mesi comuni):
#  * PRIMA (v11): il "rilevato" era un cumulato totale (import inverter
#    MAX(sum)-MIN(sum)), il gas un valore live dell'accumulatore, l'acqua la
#    somma di TUTTI i mesi di acqua_storico; le "bollette" sommavano TUTTI i
#    mesi di bolletta_*_mc. Le due parti coprivano periodi diversi (rilevato
#    pochi mesi, bollette anni) -> numeri non confrontabili.
#  * ORA: per gas/elettricita/acqua il rilevato mensile viene dai utility_meter
#    mensili (sensor.gas_mese_mc, sensor.elettricita_mese_kwh,
#    sensor.acqua_mese_mc): per ogni mese si prende il massimo raggiunto
#    (valore di fine mese, prima dell'azzeramento del ciclo). Si calcola
#    l'insieme dei mesi in cui il rilevato ha un valore > 0 e si sommano
#    rilevato e bolletta SOLO su quei mesi comuni. I mesi di sola bolletta
#    (anni arretrati senza rilevato) sono esclusi da entrambi i totali.
#  * Fotovoltaico (risparmio/GSE/totale/recupero) e prezzi: invariati da v11.
#
# CONVENZIONE COLONNE (verificata su DB):
#  * utility_meter mensili e input_number.bolletta_* -> valore consolidato in
#    colonna 'max' (min==max nel consolidato). Per ogni mese si usa MAX(max).
#  * sensori energia total_increasing dell'inverter -> colonne 'state'/'sum';
#    per il fotovoltaico si usa MAX(sum)-MIN(sum) (reset-safe).

import sqlite3, json

DB = "/config/home-assistant_v2.db"

# ── Serie mensili (una riga per mese, valore = MAX(max) del mese) ───────────
# Ritorna dict {mese 'YYYY-MM': valore} per i soli mesi con valore > 0.
MENSILE_SQL = """
SELECT strftime('%Y-%m', datetime(start_ts, 'unixepoch', 'localtime')) AS mese,
       IFNULL(MAX(max), 0) AS v
FROM statistics
WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id=?)
GROUP BY mese
HAVING v > 0
"""

# ── Energia cumulata (total_increasing): totale = MAX(sum)-MIN(sum) ─────────
ENERGIA_SUM_SQL = """
SELECT ROUND(IFNULL(MAX(sum) - MIN(sum), 0), 2)
FROM statistics
WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id=?)
"""

# Per ciascuna utenza: entita rilevato (utility_meter mensile) e bolletta.
UTENZE = [
    {"nome": "gas",   "decimals": 2,
     "rilevato": "sensor.gas_mese_mc",
     "bolletta": "input_number.bolletta_gas_mc"},
    {"nome": "elec",  "decimals": 1,
     "rilevato": "sensor.elettricita_mese_kwh",
     "bolletta": "input_number.bolletta_elec_kwh"},
    {"nome": "acqua", "decimals": 2,
     "rilevato": "sensor.acqua_mese_mc",
     "bolletta": "input_number.bolletta_acqua_mc"},
]

# Sorgenti FV (per il risparmio da autoconsumo).
PV_PRODUZIONE_ENTITY = "sensor.inverter_uflex_total_production"
PV_EXPORT_ENTITY     = "sensor.inverter_uflex_total_energy_export"

# Costo impianto per il calcolo della % di recupero.
COSTO_IMPIANTO = 25500.0


def serie_mensile(cur, entity):
    """dict {mese: valore} dei soli mesi con valore > 0."""
    rows = cur.execute(MENSILE_SQL, (entity,)).fetchall()
    return {mese: float(v) for mese, v in rows}


def energia_totale(cur, entity):
    """Totale cumulato di un sensore energia total_increasing (MAX(sum)-MIN(sum))."""
    r = cur.execute(ENERGIA_SUM_SQL, (entity,)).fetchone()
    return float(r[0]) if r and r[0] is not None else 0.0


def get_state(cur, eid):
    """Ultimo stato live di un'entità dalla tabella states."""
    r = cur.execute(
        """SELECT s.state FROM states s
           JOIN states_meta m ON s.metadata_id = m.metadata_id
           WHERE m.entity_id = ?
           ORDER BY s.last_updated_ts DESC LIMIT 1""", (eid,)).fetchone()
    return r[0] if r else None


def calc_pv_risparmio(cur, prezzo):
    """Risparmio da autoconsumo = (produzione FV - export FV) * prezzo corrente."""
    produzione = energia_totale(cur, PV_PRODUZIONE_ENTITY)
    export     = energia_totale(cur, PV_EXPORT_ENTITY)
    autoconsumo = max(0.0, produzione - export)
    return round(autoconsumo * prezzo, 2)


def main():
    conn = None
    try:
        conn = sqlite3.connect(DB, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()
        out = {}

        # ── Totali rilevato/bollette sui SOLI mesi comuni ──
        for u in UTENZE:
            rilevato = serie_mensile(cur, u["rilevato"])
            bolletta = serie_mensile(cur, u["bolletta"])
            # Mesi comuni: presenti (con valore > 0) in entrambe le serie.
            mesi_comuni = set(rilevato) & set(bolletta)
            tot_ril = round(sum(rilevato[m] for m in mesi_comuni), u["decimals"])
            tot_bol = round(sum(bolletta[m] for m in mesi_comuni), u["decimals"])
            out[f"{u['nome']}_rilevato"] = tot_ril
            out[f"{u['nome']}_bollette"] = tot_bol

        # ── Prezzo corrente (unico disponibile) per il risparmio FV ──
        prezzo_str = get_state(cur, "input_number.elettricita_prezzo_kwh")
        try:
            prezzo = float(prezzo_str) if prezzo_str not in (None, "unknown", "unavailable") else 0.0
        except (TypeError, ValueError):
            prezzo = 0.0

        # ── Fotovoltaico: risparmio, GSE, totale, % recupero ──
        pv_risparmio = calc_pv_risparmio(cur, prezzo)

        gse_serie = serie_mensile(cur, "input_number.bolletta_gse_euro")
        pv_gse = round(sum(gse_serie.values()), 2)

        out["pv_risparmio"]    = pv_risparmio
        out["pv_gse"]          = pv_gse
        out["pv_totale"]       = round(pv_risparmio + pv_gse, 2)
        out["pv_recupero_pct"] = round((out["pv_totale"] / COSTO_IMPIANTO) * 100, 2)

        print(json.dumps(out))

    except Exception as e:
        print(json.dumps({"error": str(e)}))
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()
