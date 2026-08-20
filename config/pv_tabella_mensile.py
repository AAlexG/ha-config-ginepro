#!/usr/bin/env python3
# pv_tabella_mensile_13.py
# Calcola per ogni mese con produzione FV:
#   - produzione (kWh)
#   - autoconsumo = produzione - export (kWh)
#   - % autoconsumo su produzione
#   - import da rete (kWh)
#   - consumo totale = autoconsumo - carica + scarica + import (kWh)
#     La carica va sottratta: l'autoconsumo (prod - export) contiene gia'
#     l'energia finita in batteria, che verrebbe altrimenti ricontata come
#     scarica. La differenza carica-scarica e' perdita di conversione, non
#     consumo, e resta correttamente fuori dal totale (v13).
#   - export in rete (kWh)
#   - carica batteria (kWh)
#   - scarica batteria (kWh)
#   - % perdita batteria = (carica - scarica) / carica * 100
# Aggiunge inoltre i consumi mensili per apparecchio (v11):
#   pompa, fancoil, deumidificatori, lavatrice, auto, sauna
#   piu' "altri" = consumo_tot meno le sei voci misurate (v13). Ricavato
#   per differenza e non dal sensore altri_apparecchi_oggi, che ha
#   statistiche solo da agosto 2026: cosi' la riga quadra su ogni mese.
# Il totale mensile NON viene duplicato: la tabella apparecchi legge
# direttamente consumo_tot, la stessa chiave della tabella "Totali" (v12).
# Prima riga = TOTALI con % ricalcolate sui totali
# Formato mese: MM/YY
# Output: JSON su stdout → letto da command_line sensor in HA

import sqlite3, json, sys

DB = "/config/home-assistant_v2.db"

def get_mid(cur, statistic_id):
    r = cur.execute(
        "SELECT id FROM statistics_meta WHERE statistic_id=?", (statistic_id,)
    ).fetchone()
    return r[0] if r else None

def monthly_delta(cur, mid):
    if not mid:
        return {}
    rows = cur.execute("""
        SELECT strftime('%Y-%m', datetime(start_ts,'unixepoch','localtime')) AS ym,
               MAX(sum) - MIN(sum) AS delta
        FROM statistics
        WHERE metadata_id=? AND sum IS NOT NULL
        GROUP BY ym
        HAVING delta > 0
        ORDER BY ym
    """, (mid,)).fetchall()
    return {ym: round(float(delta)) for ym, delta in rows}

def fmt_mese(ym):
    y, m = ym.split("-")
    return f"{m}/{y[2:]}"

def main():
    try:
        conn = sqlite3.connect(DB, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()

        mid_prod    = get_mid(cur, "sensor.inverter_uflex_today_production")
        mid_export  = get_mid(cur, "sensor.inverter_uflex_today_energy_export")
        mid_import  = get_mid(cur, "sensor.inverter_uflex_today_energy_import")
        mid_load    = get_mid(cur, "sensor.inverter_uflex_today_load_consumption")
        mid_bcharge = get_mid(cur, "sensor.inverter_uflex_total_battery_charge")
        mid_bdisch  = get_mid(cur, "sensor.inverter_uflex_total_battery_discharge")

        # Apparecchi: stesse entita' del grafico "Tutti apparecchi, kWh/mese"
        mid_pompa     = get_mid(cur, "sensor.panasonic_consumata_oggi")
        mid_fancoil   = get_mid(cur, "sensor.consumo_fancoil_totale_oggi")
        mid_deumid    = get_mid(cur, "sensor.consumo_deumidificatori_oggi")
        mid_lavatrice = get_mid(cur, "sensor.lavatrice_energia")
        mid_auto      = get_mid(cur, "sensor.auto_energia_oggi")
        mid_sauna     = get_mid(cur, "sensor.sauna_energia_oggi")

        prod    = monthly_delta(cur, mid_prod)
        export  = monthly_delta(cur, mid_export)
        imp     = monthly_delta(cur, mid_import)
        load    = monthly_delta(cur, mid_load)
        bcharge = monthly_delta(cur, mid_bcharge)
        bdisch  = monthly_delta(cur, mid_bdisch)

        pompa     = monthly_delta(cur, mid_pompa)
        fancoil   = monthly_delta(cur, mid_fancoil)
        deumid    = monthly_delta(cur, mid_deumid)
        lavatrice = monthly_delta(cur, mid_lavatrice)
        auto      = monthly_delta(cur, mid_auto)
        sauna     = monthly_delta(cur, mid_sauna)

        conn.close()

        mesi = [ym for ym in sorted(prod.keys()) if ym >= '2026-03']
        rows = []
        t_prod = t_auto = t_imp = t_cons = t_exp = t_bc = t_bd = 0.0
        t_pompa = t_fancoil = t_deumid = t_lavatrice = 0.0
        t_eauto = t_sauna = t_altri = 0.0

        for ym in mesi:
            p  = prod.get(ym, 0.0)
            ex = export.get(ym, 0.0)
            im = imp.get(ym, 0.0)
            bc = bcharge.get(ym, 0.0)
            bd = bdisch.get(ym, 0.0)

            autocons    = round(max(0.0, p - ex), 3)
            pct_auto    = round(autocons / p * 100, 1) if p > 0 else 0.0
            consumo_tot = round(max(0.0, autocons - bc + bd + im))
            pct_batt_loss = round((bc - bd) / bc * 100, 1) if bc > 0 else 0.0

            t_prod += p
            t_auto += autocons
            t_imp  += im
            t_cons += consumo_tot
            t_exp  += ex
            t_bc   += bc
            t_bd   += bd

            ap_pompa     = pompa.get(ym, 0.0)
            ap_fancoil   = fancoil.get(ym, 0.0)
            ap_deumid    = deumid.get(ym, 0.0)
            ap_lavatrice = lavatrice.get(ym, 0.0)
            ap_auto      = auto.get(ym, 0.0)
            ap_sauna     = sauna.get(ym, 0.0)

            # "Altri" per differenza: quanto resta del consumo di casa una
            # volta tolte le sei utenze misurate singolarmente.
            ap_altri = max(0.0, consumo_tot - (ap_pompa + ap_fancoil
                           + ap_deumid + ap_lavatrice + ap_auto + ap_sauna))

            t_pompa     += ap_pompa
            t_fancoil   += ap_fancoil
            t_deumid    += ap_deumid
            t_lavatrice += ap_lavatrice
            t_eauto     += ap_auto
            t_sauna     += ap_sauna
            t_altri     += ap_altri

            rows.append({
                "mese":          fmt_mese(ym),
                "produzione":    round(p),
                "autoconsumo":   round(autocons),
                "pct_auto":      pct_auto,
                "import":        round(im),
                "consumo_tot":   round(consumo_tot),
                "export":        round(ex),
                "batt_carica":   round(bc),
                "batt_scarica":  round(bd),
                "pct_batt_loss": pct_batt_loss,
                # --- apparecchi (v11) ---
                "pompa":         round(ap_pompa),
                "fancoil":       round(ap_fancoil),
                "deumid":        round(ap_deumid),
                "lavatrice":     round(ap_lavatrice),
                "auto":          round(ap_auto),
                "sauna":         round(ap_sauna),
                "altri":         round(ap_altri),
            })

        # Riga totali con % ricalcolate sui totali
        tot_pct_auto     = round(t_auto / t_prod * 100, 1) if t_prod > 0 else 0.0
        tot_pct_batt_loss = round((t_bc - t_bd) / t_bc * 100, 1) if t_bc > 0 else 0.0

        totale = {
            "mese":          "TOTALE",
            "produzione":    round(t_prod),
            "autoconsumo":   round(t_auto),
            "pct_auto":      tot_pct_auto,
            "import":        round(t_imp),
            "consumo_tot":   round(t_cons),
            "export":        round(t_exp),
            "batt_carica":   round(t_bc),
            "batt_scarica":  round(t_bd),
            "pct_batt_loss": tot_pct_batt_loss,
            # --- apparecchi (v11) ---
            "pompa":         round(t_pompa),
            "fancoil":       round(t_fancoil),
            "deumid":        round(t_deumid),
            "lavatrice":     round(t_lavatrice),
            "auto":          round(t_eauto),
            "sauna":         round(t_sauna),
            "altri":         round(t_altri),
        }

        # Totali in prima posizione, poi mesi dal più recente
        output = list(reversed(rows)) + [totale]
        print(json.dumps({"mesi": output}, ensure_ascii=False))

    except Exception as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()