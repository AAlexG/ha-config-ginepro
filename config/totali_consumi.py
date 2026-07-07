#!/usr/bin/env python3
# totali_consumi 10.py
# Calcola totali storici rilevato/bollette dalla tabella statistics del DB HA.
# Stampa un JSON su stdout con i valori.

import sqlite3, json, sys

DB = "/config/home-assistant_v2.db"

# Query standard per Gas ed Elettricità: calcola la differenza reale (MAX - MIN) di ogni mese
RILEVATO_SQL = """
SELECT ROUND(COALESCE(SUM(x.v), 0), {decimals})
FROM (
    SELECT (IFNULL(MAX(max), 0) - IFNULL(MIN(min), 0)) AS v
    FROM statistics
    WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='{entity}')
    GROUP BY strftime('%Y-%m', datetime(start_ts,'unixepoch','localtime'))
    HAVING v >= 0
) x
"""

# Query specifica per l'Acqua (Rilevato e Bollette): somma i massimi mensili consolidati
RILEVATO_ACQUA_SQL = """
SELECT ROUND(COALESCE(SUM(x.v), 0), {decimals})
FROM (
    SELECT IFNULL(MAX(max), 0) AS v
    FROM statistics
    WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='{entity}')
    GROUP BY strftime('%Y-%m', datetime(start_ts,'unixepoch','localtime'))
) x
"""

BOLLETTE_SQL = """
SELECT ROUND(COALESCE(SUM(b.v), 0), {decimals})
FROM (
    SELECT strftime('%Y-%m', datetime(start_ts,'unixepoch','localtime')) AS ym
    FROM statistics
    WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='{rilevato_entity}')
    GROUP BY ym
) m
LEFT JOIN (
    SELECT strftime('%Y-%m', datetime(start_ts,'unixepoch','localtime')) AS ym, IFNULL(MAX(max), 0) AS v
    FROM statistics
    WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='{bolletta_entity}')
    GROUP BY ym
) b ON m.ym = b.ym
"""

# Mantenuta la query originale per le bollette dell'acqua se si desidera usare la stessa logica diretta di verifica del terminale
BOLLETTE_ACQUA_SQL = """
SELECT ROUND(COALESCE(SUM(x.v), 0), {decimals})
FROM (
    SELECT IFNULL(MAX(max), 0) AS v
    FROM statistics
    WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='{bolletta_entity}')
    GROUP BY strftime('%Y-%m', datetime(start_ts,'unixepoch','localtime'))
) x
"""

PV_RISPARMIO_PROD_SQL = """
SELECT strftime('%Y-%m', datetime(start_ts,'unixepoch','localtime')) AS ym,
       (IFNULL(MAX(max), 0) - IFNULL(MIN(min), 0)) AS prod
FROM statistics
WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='sensor.inverter_uflex_total_production')
GROUP BY ym
"""

PV_RISPARMIO_RETE_SQL = """
SELECT strftime('%Y-%m', datetime(start_ts,'unixepoch','localtime')) AS ym,
       (IFNULL(MAX(max), 0) - IFNULL(MIN(min), 0)) AS rete
FROM statistics
WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='sensor.inverter_uflex_total_from_grid')
GROUP BY ym
"""

PV_GSE_SQL = """
SELECT ROUND(COALESCE(SUM(x.v), 0), 2)
FROM (
    SELECT IFNULL(MAX(max), 0) AS v
    FROM statistics
    WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='sensor.gse_totale_contributi')
    GROUP BY strftime('%Y-%m', datetime(start_ts,'unixepoch','localtime'))
) x
"""

# Configurazione mappata con le entità e le query verificate in terminale
CONFIGS = [
    {
        "key": "gas_rilevato",
        "entity": "sensor.contatore_gas_corretto",
        "decimals": 2,
        "sql": RILEVATO_SQL
    },
    {
        "key": "gas_bollette",
        "rilevato_entity": "sensor.contatore_gas_corretto",
        "bolletta_entity": "sensor.gas_bolletta_m3",
        "decimals": 2,
        "sql": BOLLETTE_SQL
    },
    {
        "key": "elec_rilevato",
        "entity": "sensor.inverter_uflex_total_from_grid",
        "decimals": 1,
        "sql": RILEVATO_SQL
    },
    {
        "key": "elec_bollette",
        "rilevato_entity": "sensor.inverter_uflex_total_from_grid",
        "bolletta_entity": "sensor.energia_bolletta_kwh",
        "decimals": 1,
        "sql": BOLLETTE_SQL
    },
    {
        "key": "acqua_rilevato",
        "entity": "sensor.acqua_storico_mc_mensile",
        "decimals": 2,
        "sql": RILEVATO_ACQUA_SQL
    },
    {
        "key": "acqua_bollette",
        "bolletta_entity": "input_number.bolletta_acqua_mc",
        "decimals": 2,
        "sql": BOLLETTE_ACQUA_SQL
    }
]

def calc_pv_risparmio(cur):
    prod_map = {r[0]: r[1] for r in cur.execute(PV_RISPARMIO_PROD_SQL).fetchall()}
    rete_map = {r[0]: r[1] for r in cur.execute(PV_RISPARMIO_RETE_SQL).fetchall()}
    
    totale = 0.0
    for ym, produzione in prod_map.items():
        if produzione <= 0:
            continue
            
        da_rete = rete_map.get(ym, 0.0)
        
        r_boll = cur.execute(
            "SELECT IFNULL(MAX(max), 0) FROM statistics WHERE "
            "metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='sensor.energia_bolletta_kwh') "
            "AND strftime('%Y-%m',datetime(start_ts,'unixepoch','localtime'))=?",
            (ym,)
        ).fetchone()
        bolletta = float(r_boll[0]) if r_boll else 0.0
        
        r_prc = cur.execute(
            "SELECT IFNULL(MAX(max), 0) FROM statistics WHERE "
            "metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id='sensor.prezzo_kwh') "
            "AND strftime('%Y-%m',datetime(start_ts,'unixepoch','localtime'))=?",
            (ym,)
        ).fetchone()
        prezzo = float(r_prc[0]) if r_prc else 0.0
        
        mid_exp = cur.execute("SELECT id FROM statistics_meta WHERE statistic_id='sensor.inverter_uflex_total_to_grid'").fetchone()
        esportato = 0.0
        if mid_exp:
            r = cur.execute(
                "SELECT (IFNULL(MAX(max), 0) - IFNULL(MIN(min), 0)) FROM statistics WHERE metadata_id=? "
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

        for cfg in CONFIGS:
            query  = cfg["sql"].format(**{k:v for k,v in cfg.items() if k not in ("sql","key")})
            result = cur.execute(query).fetchone()
            out[cfg["key"]] = float(result[0]) if result and result[0] is not None else 0.0

        pv_risparmio = calc_pv_risparmio(cur)

        r = cur.execute(PV_GSE_SQL).fetchone()
        pv_gse = float(r[0]) if r and r[0] is not None else 0.0

        COSTO_IMPIANTO = 25500.0

        out["pv_risparmio"] = pv_risparmio
        out["pv_gse"]       = pv_gse
        out["pv_totale"]    = round(pv_risparmio + pv_gse, 2)
        out["pv_recupero_pct"] = round((out["pv_totale"] / COSTO_IMPIANTO) * 100, 2)

        print(json.dumps(out))

    except Exception as e:
        print(json.dumps({"error": str(e)}))
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    main()