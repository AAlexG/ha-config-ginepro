#!/usr/bin/env python3
# totali_consumi_9.py
# Calcola totali storici rilevato/bollette dalla tabella statistics del DB HA.
# Stampa un JSON su stdout con i valori (pattern identico a totali_consumi_2.py).
# Aggiunge tre nuovi campi per risparmio fotovoltaico:
#   - pv_risparmio : somma mensile di (da_rete_kwh - bolletta_kwh) * prezzo_kwh
#                    solo per mesi con produzione solare > 0
#   - pv_gse       : somma di tutti i pagamenti GSE registrati nel DB
#   - pv_totale    : pv_risparmio + pv_gse

import sqlite3, json, sys

DB = "/config/home-assistant_v2.db"

RILEVATO_SQL = """
SELECT ROUND(COALESCE(SUM(x.v), 0), {decimals})
FROM (
    SELECT IFNULL(MAX(max), 0) AS v
    FROM statistics
    WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='{entity}')
    GROUP BY strftime('%Y-%m', datetime(start_ts,'unixepoch','localtime'))
    HAVING v > 0
) x
"""

BOLLETTE_SQL = """
SELECT ROUND(COALESCE(SUM(b.v), 0), {decimals})
FROM (
    SELECT strftime('%Y-%m', datetime(start_ts,'unixepoch','localtime')) AS ym
    FROM statistics
    WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='{rilevato_entity}')
    GROUP BY ym HAVING IFNULL(MAX(max),0) > 0
) r
JOIN (
    SELECT strftime('%Y-%m', datetime(start_ts,'unixepoch','localtime')) AS ym,
           IFNULL(MAX(max),0) AS v
    FROM statistics
    WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='{bolletta_entity}')
    GROUP BY ym HAVING v > 0
) b ON r.ym = b.ym
"""

# Risparmio FV per mese:
#   autoconsumo = (produzione_mese - esportato_rete_mese) * prezzo_kwh
# produzione_mese  = MAX(sum)-MIN(sum) di sensor.inverter_uflex_today_production
# esportato_mese   = MAX(sum)-MIN(sum) di sensor.inverter_uflex_today_energy_export
# Solo mesi con produzione > 0, clampato a 0 se negativo
PV_RISPARMIO_SQL = """
SELECT
    ROUND(
        COALESCE(SUM(
            MAX(0, (prod.produzione - IFNULL(exp.esportato, 0)) * IFNULL(p.prezzo, 0))
        ), 0),
    2)
FROM (
    -- produzione mensile: MAX(sum)-MIN(sum) di sensor.inverter_uflex_today_production
    SELECT strftime('%Y-%m', datetime(start_ts,'unixepoch','localtime')) AS ym,
           MAX(sum) - MIN(sum) AS produzione
    FROM statistics
    WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='sensor.inverter_uflex_today_production')
    GROUP BY ym
    HAVING produzione > 0
) prod
LEFT JOIN (
    -- esportato in rete mensile: MAX(sum)-MIN(sum) di sensor.inverter_uflex_today_energy_export
    SELECT strftime('%Y-%m', datetime(start_ts,'unixepoch','localtime')) AS ym,
           MAX(sum) - MIN(sum) AS esportato
    FROM statistics
    WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='sensor.inverter_uflex_today_energy_export')
    GROUP BY ym
) exp ON exp.ym = prod.ym
JOIN (
    -- prezzo kWh attuale
    SELECT IFNULL(MAX(max), 0) AS prezzo
    FROM statistics
    WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='input_number.elettricita_prezzo_kwh')
) p
"""

# GSE totale: somma di tutti i pagamenti GSE > 0 nel DB
PV_GSE_SQL = """
SELECT ROUND(COALESCE(SUM(x.v), 0), 2)
FROM (
    SELECT IFNULL(MAX(max), 0) AS v
    FROM statistics
    WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='input_number.bolletta_gse_euro')
    GROUP BY strftime('%Y-%m', datetime(start_ts,'unixepoch','localtime'))
    HAVING v > 0
) x
"""

CONFIGS = [
    {"key": "gas_rilevato",
     "sql": RILEVATO_SQL, "entity": "sensor.gas_mese_mc", "decimals": 3},
    {"key": "gas_bollette",
     "sql": BOLLETTE_SQL, "rilevato_entity": "sensor.gas_mese_mc",
     "bolletta_entity": "input_number.bolletta_gas_mc", "decimals": 3},
    {"key": "elec_rilevato",
     "sql": RILEVATO_SQL, "entity": "sensor.elettricita_mese_kwh", "decimals": 2},
    {"key": "elec_bollette",
     "sql": BOLLETTE_SQL, "rilevato_entity": "sensor.elettricita_mese_kwh",
     "bolletta_entity": "input_number.bolletta_elec_kwh", "decimals": 2},
    {"key": "acqua_rilevato",
     "sql": RILEVATO_SQL, "entity": "sensor.acqua_mese_mc", "decimals": 3},
    {"key": "acqua_bollette",
     "sql": BOLLETTE_SQL, "rilevato_entity": "sensor.acqua_mese_mc",
     "bolletta_entity": "input_number.bolletta_acqua_mc", "decimals": 3},
]

def calc_pv_risparmio(cur):
    # Legge prezzo kWh da states (non storicizzato in statistics)
    mid = cur.execute(
        "SELECT metadata_id FROM states_meta WHERE entity_id='input_number.elettricita_prezzo_kwh'"
    ).fetchone()
    if not mid:
        return 0.0
    row = cur.execute(
        "SELECT state FROM states WHERE metadata_id=? AND state NOT IN ('unknown','unavailable') ORDER BY last_updated_ts DESC LIMIT 1",
        (mid[0],)
    ).fetchone()
    if not row:
        return 0.0
    prezzo = float(row[0])

    # Produzione mensile: MAX(sum)-MIN(sum) per mese
    mid_prod = cur.execute(
        "SELECT id FROM statistics_meta WHERE statistic_id='sensor.inverter_uflex_today_production'"
    ).fetchone()
    if not mid_prod:
        return 0.0

    # Export mensile: MAX(sum)-MIN(sum) per mese
    mid_exp = cur.execute(
        "SELECT id FROM statistics_meta WHERE statistic_id='sensor.inverter_uflex_today_energy_export'"
    ).fetchone()

    prod_rows = cur.execute(
        "SELECT strftime('%Y-%m',datetime(start_ts,'unixepoch','localtime')) as ym, MAX(sum)-MIN(sum) as produzione "
        "FROM statistics WHERE metadata_id=? AND sum IS NOT NULL GROUP BY ym HAVING produzione > 0 ORDER BY ym",
        (mid_prod[0],)
    ).fetchall()

    totale = 0.0
    for ym, produzione in prod_rows:
        esportato = 0.0
        if mid_exp:
            r = cur.execute(
                "SELECT MAX(sum)-MIN(sum) FROM statistics WHERE metadata_id=? AND sum IS NOT NULL "
                "AND strftime('%Y-%m',datetime(start_ts,'unixepoch','localtime'))=?",
                (mid_exp[0], ym)
            ).fetchone()
            if r and r[0] is not None:
                esportato = float(r[0])
        autoconsumo = max(0.0, float(produzione) - esportato)
        totale += autoconsumo * prezzo

    return round(totale, 2)

def main():
    try:
        conn = sqlite3.connect(DB, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        cur  = conn.cursor()
        out  = {}

        # Calcoli esistenti (gas, elettricità, acqua)
        for cfg in CONFIGS:
            query  = cfg["sql"].format(**{k:v for k,v in cfg.items() if k not in ("sql","key")})
            result = cur.execute(query).fetchone()
            out[cfg["key"]] = float(result[0]) if result and result[0] is not None else 0.0

        # Risparmio FV (calcolato in Python)
        pv_risparmio = calc_pv_risparmio(cur)

        # GSE totale
        r = cur.execute(PV_GSE_SQL).fetchone()
        pv_gse = float(r[0]) if r and r[0] is not None else 0.0

        COSTO_IMPIANTO = 25500.0

        out["pv_risparmio"] = pv_risparmio
        out["pv_gse"]       = pv_gse
        out["pv_totale"]    = round(pv_risparmio + pv_gse, 2)
        out["pv_recupero_pct"] = round((out["pv_totale"] / COSTO_IMPIANTO) * 100, 2)

        conn.close()
        print(json.dumps(out))
    except Exception as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()