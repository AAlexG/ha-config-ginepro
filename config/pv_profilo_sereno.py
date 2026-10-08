#!/usr/bin/env python3
# pv_profilo_sereno_1.py
#
# Profilo orario della produzione FV in una giornata serena, per il periodo
# dell'anno corrente. Serve al sensore "Condensatore Previsione Pieno"
# (config_ricarica_auto) come FORMA della curva di produzione: la SCALA la
# da' la produzione reale di oggi.
#
# Metodo:
#   1. Energia oraria prodotta = differenza della colonna "sum" delle
#      statistiche orarie di sensor.inverter_uflex_today_production (dati
#      dal 15/04/2025). Ore senza sum: differenza di "state", con gestione
#      dell'azzeramento di mezzanotte.
#   2. Finestra: giorni entro +/- 15 giorni dalla data di oggi, di qualsiasi
#      anno, oggi escluso (giornata incompleta).
#   3. Giorni sereni = i migliori per produzione totale: il 20% superiore
#      della finestra, minimo 3 giorni. Non c'e' limite di immissione in
#      rete, quindi la produzione non e' mai tagliata e il totale del giorno
#      misura davvero il sole.
#   4. Profilo = media ora per ora (ora locale Europe/Rome) dei giorni sereni.
#      Contiene da solo orientamento dei pannelli e ombre del pomeriggio.
#
# Output: JSON su stdout, letto dal command_line sensor PV Profilo Sereno.

import sqlite3, json, datetime as dt
from zoneinfo import ZoneInfo

DB = "file:/config/home-assistant_v2.db?mode=ro"
SENSORE = "sensor.inverter_uflex_today_production"
TZ = ZoneInfo("Europe/Rome")
FINESTRA_GIORNI = 15
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
    righe = cur.execute(
        "SELECT start_ts, sum, state FROM statistics WHERE metadata_id=? ORDER BY start_ts",
        (r[0],)).fetchall()

    # Energia per ora: la riga con start_ts copre [start, start+1h) e porta
    # sum/state alla fine di quell'ora.
    ore = {}
    prec = None
    for ts, s, st in righe:
        if prec is not None and ts - prec[0] <= 3600 + 60:
            e = None
            if s is not None and prec[1] is not None:
                e = s - prec[1]
            elif st is not None and prec[2] is not None:
                e = st - prec[2] if st >= prec[2] else st
            if e is not None and 0 <= e <= MAX_KWH_ORA:
                ore[ts] = e
        prec = (ts, s, st)

    giorni = {}
    for ts, e in ore.items():
        loc = dt.datetime.fromtimestamp(ts, TZ)
        giorni.setdefault(loc.date(), [0.0] * 24)[loc.hour] += e

    oggi = dt.datetime.now(TZ).date()

    def distanza(d):
        # distanza in giorni dalla data di oggi, ignorando l'anno
        try:
            rif = d.replace(year=oggi.year)
        except ValueError:          # 29 febbraio
            rif = d.replace(year=oggi.year, day=28)
        x = abs((rif - oggi).days)
        return min(x, 365 - x)

    finestra = {d: p for d, p in giorni.items()
                if d != oggi and distanza(d) <= FINESTRA_GIORNI and sum(p) > 0}
    if len(finestra) < MIN_SERENI:
        print(json.dumps({"errore": "meno di %d giorni nella finestra" % MIN_SERENI,
                          "profilo": []}))
        return

    n = max(MIN_SERENI, round(len(finestra) * QUOTA_SERENI))
    sereni = sorted(finestra, key=lambda d: sum(finestra[d]), reverse=True)[:n]
    profilo = [round(sum(finestra[d][h] for d in sereni) / n, 3) for h in range(24)]

    print(json.dumps({
        "profilo": profilo,
        "totale_sereno": round(sum(profilo), 2),
        "giorni_finestra": len(finestra),
        "giorni_sereni": sorted(d.strftime("%d/%m/%y") for d in sereni),
        "calcolato": dt.datetime.now(TZ).strftime("%d/%m/%Y %H:%M"),
    }))

if __name__ == "__main__":
    main()
