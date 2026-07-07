#!/usr/bin/env python3
# totali_consumi_11.py
# Calcola totali storici rilevato/bollette dalla tabella statistics del DB HA.
# Stampa un JSON su stdout con i valori, letto dall'automazione
# calcola_totali_consumi che aggiorna gli helper input_number della plancia.
#
# CORREZIONI v11 (dopo verifica diretta su DB):
#  * I sensori energia dell'inverter (import/produzione/export) sono
#    total_increasing: popolano le colonne 'state' e 'sum', NON 'min'/'max'.
#    Quindi MAX(max)-MIN(min) dava NULL -> 0. Ora si usa MAX(sum)-MIN(sum),
#    che è anche reset-safe (assorbe la riconfigurazione dell'inverter).
#  * Nomi entità aggiornati alla convenzione reale dell'integrazione Solarman:
#      sensor.inverter_uflex_total_energy_import   (elettricità rilevato)
#      sensor.inverter_uflex_total_energy_export   (export FV)
#      sensor.inverter_uflex_total_production       (produzione FV)
#  * Bollette gas/elettricità e GSE lette dai relativi input_number.bolletta_*
#    (i vecchi sensor.*_bolletta_* e sensor.gse_totale_contributi non esistono).
#  * Gas rilevato = valore live dell'accumulatore input_number.caldaia_gas_totale_acc
#    (non ha statistiche; letto dalla tabella states, come il template gas_mese_mc).
#  * Prezzo del risparmio FV = valore live di input_number.elettricita_prezzo_kwh
#    applicato a tutta la storia (unico prezzo disponibile). Con prezzo costante
#    il risparmio da autoconsumo si riduce a (produzione - export) * prezzo.

import sqlite3, json

DB = "/config/home-assistant_v2.db"

# ── Query ────────────────────────────────────────────────────────────────

# Energia cumulata (total_increasing): totale = MAX(sum) - MIN(sum).
# Vale per import da rete, produzione FV, export FV. Reset-safe.
ENERGIA_SUM_SQL = """
SELECT ROUND(IFNULL(MAX(sum) - MIN(sum), 0), {decimals})
FROM statistics
WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='{entity}')
"""

# Bollette e acqua rilevato: valore consolidato mensile (min==max), si somma
# il massimo di ogni mese. Vale per input_number.bolletta_* e per
# sensor.acqua_storico_mc_mensile.
SOMMA_MENSILE_SQL = """
SELECT ROUND(COALESCE(SUM(x.v), 0), {decimals})
FROM (
    SELECT IFNULL(MAX(max), 0) AS v
    FROM statistics
    WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='{entity}')
    GROUP BY strftime('%Y-%m', datetime(start_ts, 'unixepoch', 'localtime'))
) x
"""

CONFIGS = [
    # ── Gas ──
    {"key": "gas_bollette",   "entity": "input_number.bolletta_gas_mc",
     "decimals": 2, "sql": SOMMA_MENSILE_SQL},
    # ── Elettricità ──
    {"key": "elec_rilevato",  "entity": "sensor.inverter_uflex_total_energy_import",
     "decimals": 1, "sql": ENERGIA_SUM_SQL},
    {"key": "elec_bollette",  "entity": "input_number.bolletta_elec_kwh",
     "decimals": 1, "sql": SOMMA_MENSILE_SQL},
    # ── Acqua ──
    {"key": "acqua_rilevato", "entity": "sensor.acqua_storico_mc_mensile",
     "decimals": 2, "sql": SOMMA_MENSILE_SQL},
    {"key": "acqua_bollette", "entity": "input_number.bolletta_acqua_mc",
     "decimals": 2, "sql": SOMMA_MENSILE_SQL},
]

# Sorgenti FV (per il risparmio da autoconsumo).
PV_PRODUZIONE_ENTITY = "sensor.inverter_uflex_total_production"
PV_EXPORT_ENTITY     = "sensor.inverter_uflex_total_energy_export"

# Costo impianto per il calcolo della % di recupero.
COSTO_IMPIANTO = 25500.0


def get_state(cur, eid):
    """Ultimo stato live di un'entità dalla tabella states."""
    r = cur.execute(
        """SELECT s.state FROM states s
           JOIN states_meta m ON s.metadata_id = m.metadata_id
           WHERE m.entity_id = ?
           ORDER BY s.last_updated_ts DESC LIMIT 1""", (eid,)).fetchone()
    return r[0] if r else None


def energia_totale(cur, entity, decimals):
    """Totale cumulato di un sensore energia total_increasing (MAX(sum)-MIN(sum))."""
    r = cur.execute(ENERGIA_SUM_SQL.format(entity=entity, decimals=decimals)).fetchone()
    return float(r[0]) if r and r[0] is not None else 0.0


def calc_pv_risparmio(cur, prezzo):
    """Risparmio da autoconsumo = (produzione FV - export FV) * prezzo corrente.
    Con prezzo costante la somma dei contributi mensili (prod-export)*prezzo
    telescopa nel totale produzione meno totale export."""
    produzione = energia_totale(cur, PV_PRODUZIONE_ENTITY, 2)
    export     = energia_totale(cur, PV_EXPORT_ENTITY, 2)
    autoconsumo = max(0.0, produzione - export)
    return round(autoconsumo * prezzo, 2)


def main():
    conn = None
    try:
        conn = sqlite3.connect(DB, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()
        out = {}

        # ── Totali da statistiche (bollette, acqua, elettricità rilevato) ──
        for cfg in CONFIGS:
            query  = cfg["sql"].format(entity=cfg["entity"], decimals=cfg["decimals"])
            result = cur.execute(query).fetchone()
            out[cfg["key"]] = float(result[0]) if result and result[0] is not None else 0.0

        # ── Gas rilevato: valore live dell'accumulatore (nessuna statistica) ──
        gas_acc = get_state(cur, "input_number.caldaia_gas_totale_acc")
        try:
            out["gas_rilevato"] = round(float(gas_acc), 2) if gas_acc not in (None, "unknown", "unavailable") else 0.0
        except (TypeError, ValueError):
            out["gas_rilevato"] = 0.0

        # ── Prezzo corrente (unico disponibile) per il risparmio FV ──
        prezzo_str = get_state(cur, "input_number.elettricita_prezzo_kwh")
        try:
            prezzo = float(prezzo_str) if prezzo_str not in (None, "unknown", "unavailable") else 0.0
        except (TypeError, ValueError):
            prezzo = 0.0

        # ── Fotovoltaico: risparmio, GSE, totale, % recupero ──
        pv_risparmio = calc_pv_risparmio(cur, prezzo)

        r = cur.execute(SOMMA_MENSILE_SQL.format(
            entity="input_number.bolletta_gse_euro", decimals=2)).fetchone()
        pv_gse = float(r[0]) if r and r[0] is not None else 0.0

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