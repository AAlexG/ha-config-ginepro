#!/usr/bin/env python3
# pv_profilo_sereno_2.py
#
# v2 (29/10/2026): ore in UTC invece che in ora locale. Con l'ora locale i
#   giorni in ora legale (CEST) e quelli in ora solare (CET) erano sfasati di
#   un'ora: il 29/10 il profilo mescolava 15/10 e 22/10 (CEST) con 27/10 (CET),
#   il pomeriggio risultava un'ora piu' lungo e la previsione stimava circa il
#   doppio della produzione residua reale. Finestra ridotta ai 10 giorni
#   precedenti (le giornate in autunno si accorciano di 2-3 minuti al giorno);
#   se nella finestra ci sono meno di 6 giorni si allarga a 20.
#
# Profilo orario della produzione FV in una giornata serena, per il periodo
# dell'anno corrente. Serve al sensore "Condensatore Previsione Pieno"
# (config_ricarica_auto) come FORMA della curva di produzione: la SCALA la
# da' la produzione reale di oggi.
#
# Metodo:
#   1. Energia oraria prodotta = differenza della colonna "sum" delle
#      statistiche orarie di sensor.inverter_uflex_today_production. Ore senza
#      sum: differenza di "state", con gestione dell'azzeramento giornaliero.
#   2. Ogni ora e' assegnata all'ora UTC di inizio e al giorno locale.
#   3. Finestra: i 10 giorni locali precedenti a oggi (oggi escluso).
#   4. Giorni sereni = il 20 % migliore per produzione totale, minimo 3.
#   5. Profilo = media ora UTC per ora UTC dei giorni sereni.
#
# Output: JSON su stdout, letto dal command_line sensor PV Profilo Sereno.
# "base": "UTC" indica che l'indice del profilo e' l'ora UTC.

import sqlite3, json, datetime as dt
from zoneinfo import ZoneInfo

DB = "file:/config/home-assistant_v2.db?mode=ro"
SENSORE = "sensor.inverter_uflex_today_production"
TZ = ZoneInfo("Europe/Rome")
UTC = dt.timezone.utc
FINESTRA_GIORNI = 10
FINESTRA_MAX = 20
MIN_GIORNI = 6
QUOTA_SERENI = 0.20
MIN_SERENI = 3
MAX_KWH_ORA = 15  # oltre e' un errore di dato (salto del contatore)

def main():
    con = sqlite3.connect(DB, uri=True)
    cur = con.cursor()
    r = cur.execute("SELECT id FROM statistics_meta WHERE statistic_id=?", (SENSORE,)).fetchone()
    if not r:
        print(json.dumps({"errore": "statistiche mancanti per " + SENSORE, "profilo": []}))
        return
    oggi = dt.datetime.now(TZ).date()
    inizio = dt.datetime.combine(oggi - dt.timedelta(days=FINESTRA_MAX + 1),
                                 dt.time(0), TZ).timestamp()
    righe = cur.execute(
        "SELECT start_ts, sum, state FROM statistics WHERE metadata_id=? AND start_ts>=? "
        "ORDER BY start_ts", (r[0], inizio)).fetchall()

    giorni = {}
    prec = None
    for ts, s, st in righe:
        if prec is not None and ts - prec[0] <= 3600 + 60:
            e = None
            if s is not None and prec[1] is not None:
                e = s - prec[1]
            elif st is not None and prec[2] is not None:
                e = st - prec[2] if st >= prec[2] else st
            if e is not None and 0 <= e <= MAX_KWH_ORA:
                giorno = dt.datetime.fromtimestamp(ts, TZ).date()
                ora_utc = dt.datetime.fromtimestamp(ts, UTC).hour
                giorni.setdefault(giorno, [0.0] * 24)[ora_utc] += e
        prec = (ts, s, st)

    def finestra(n):
        return {d: p for d, p in giorni.items()
                if 1 <= (oggi - d).days <= n and sum(p) > 0}

    usati = FINESTRA_GIORNI
    fin = finestra(usati)
    if len(fin) < MIN_GIORNI:
        usati = FINESTRA_MAX
        fin = finestra(usati)
    if len(fin) < MIN_SERENI:
        print(json.dumps({"errore": "meno di %d giorni nella finestra" % MIN_SERENI,
                          "profilo": []}))
        return

    n = max(MIN_SERENI, round(len(fin) * QUOTA_SERENI))
    sereni = sorted(fin, key=lambda d: sum(fin[d]), reverse=True)[:n]
    profilo = [round(sum(fin[d][h] for d in sereni) / n, 3) for h in range(24)]

    print(json.dumps({
        "profilo": profilo,
        "base": "UTC",
        "totale_sereno": round(sum(profilo), 2),
        "giorni_finestra": len(fin),
        "finestra_giorni": usati,
        "giorni_sereni": sorted(d.strftime("%d/%m/%y") for d in sereni),
        "calcolato": dt.datetime.now(TZ).strftime("%d/%m/%Y %H:%M"),
    }))

if __name__ == "__main__":
    main()
