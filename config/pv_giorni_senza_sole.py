#!/usr/bin/env python3
# pv_giorni_senza_sole_4.py
# Conta, per ogni mese, i giorni in cui il sole non si e' visto nemmeno per
# un istante, usando l'irraggiamento della stazione meteo HP1000SE Pro e non
# la produzione fotovoltaica.
#
# PERCHE' L'IRRAGGIAMENTO E NON LA PRODUZIONE
# La produzione FV dipende da guasti dell'inverter, blocchi del pacco
# batteria, ombreggiamenti e curtailment: il 20/08/2026 produsse 2,4 kWh e
# sembrava un guasto, mentre l'irraggiamento (256 Wh/m2, picco 194 W/m2)
# dimostra che era davvero una giornata coperta. Viceversa il 26/07/2026 con
# 32,2 kWh era una giornata serena. L'irraggiamento misura il sole, la
# produzione misura l'impianto.
#
# CRITERIO
# Giorno senza sole = il massimo istantaneo della giornata non ha superato il
# 35% del RIFERIMENTO MENSILE, cioe' il terzo massimo giornaliero piu' alto
# del mese.
#
# PERCHE' IL TERZO E NON IL PRIMO (v4)
# Con il massimo assoluto un solo campione anomalo alza la soglia di tutto il
# mese. Maggio 2026 aveva 1811 W/m2 il giorno 05, contro 1040, 1003, 942, 935
# dei giorni successivi: un valore non plausibile fisicamente, perche' la
# radiazione globale su piano orizzontale al livello del mare non supera circa
# 1000-1100 W/m2 (picchi brevi fino a 1300-1400 sono possibili solo per
# riflessione dal bordo delle nuvole). La soglia di maggio risultava 634,
# quasi il doppio di quella di giugno, mese con irraggiamento equivalente.
# Col terzo massimo le soglie dei mesi da 02/26 a 09/26 diventano 205, 258,
# 332, 351, 328, 310, 322, 374: omogenee. I conteggi non cambiano su nessun
# mese, quindi la correzione non altera i risultati noti, rende solo il
# riferimento insensibile a un singolo dato anomalo.
#
# Si usa il massimo orario (colonna max delle statistiche) e non la media,
# perche' la media non registra le schiarite brevi: il 02/06/2026 ha media
# oraria sempre sotto 300 W/m2 ma massimo 772, cioe' un lampo di sole di
# pochi minuti dentro una giornata coperta. Con il massimo quel giorno NON
# viene contato come senza sole, ed e' corretto: il sole si e' visto.
#
# La soglia e' relativa al mese e non fissa in W/m2 perche' d'inverno il sole
# e' basso e anche un giorno sereno resta su valori modesti: il 25/02/2026,
# giorno piu' luminoso del mese, ha massimo 477 W/m2, meno di molti giorni
# coperti di giugno. Una soglia fissa a 500 avrebbe marcato l'intero febbraio
# come senza sole. Il 35% del massimo mensile vale circa 230 W/m2 a febbraio
# e circa 346 a giugno: segue la stagione da solo.
#
# LIMITE NOTO
# Se un mese intero fosse coperto, il suo massimo mensile sarebbe esso stesso
# un valore da cielo coperto e il conteggio risulterebbe troppo basso. Con i
# dati disponibili (da febbraio 2026) il caso non si e' presentato, ma va
# riverificato dopo il primo inverno completo.
#
# GIORNI ESCLUSI (ne' senza sole ne' con sole: stazione non attendibile)
#   - meno di 20 ore registrate nella giornata (stazione offline)
#   - totale giornaliero sotto 100 Wh/m2 (il 03/04/2026 ha 18 Wh/m2 con
#     massimo 23: non e' meteo, e' la stazione ferma)
#
# MESE CORRENTE ESCLUSO (v3)
# La soglia e' una frazione del massimo mensile, che a mese in corso non e'
# ancora noto: se il mese comincia coperto il massimo resta basso, la soglia
# scende con lui e i giorni coperti non vengono contati. Il conteggio sarebbe
# comunque provvisorio e potrebbe diminuire col passare dei giorni. Il mese
# corrente viene quindi omesso e compare il primo giorno del mese successivo.
#
# Output: JSON su stdout -> letto dal command_line sensor in HA
# Formato mese: MM/YY, ordine cronologico, ultimi 13 mesi completi

import sqlite3, json, sys
from collections import defaultdict
from datetime import date

DB = "/config/home-assistant_v2.db"
SENSORE = "sensor.hp1000se_pro_pro_v1_6_4_solar_radiation"

FRAZIONE_SOGLIA = 0.35   # quota del riferimento mensile sotto la quale = senza sole
POSIZIONE_RIFERIMENTO = 3  # 3 = terzo massimo giornaliero del mese
ORE_MINIME      = 20     # ore registrate sotto le quali il giorno e' scartato
WH_MINIMI       = 100    # Wh/m2 giornalieri sotto i quali il giorno e' scartato
MESI_GRAFICO    = 13

def fmt_mese(ym):
    y, m = ym.split("-")
    return f"{m}/{y[2:]}"

def main():
    try:
        conn = sqlite3.connect(DB, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()

        r = cur.execute(
            "SELECT id FROM statistics_meta WHERE statistic_id=?", (SENSORE,)
        ).fetchone()
        if not r:
            print(json.dumps({"mesi": [], "errore": "sensore irraggiamento senza statistiche"}))
            return
        mid = r[0]

        # Aggregazione per giorno: ore registrate, totale Wh/m2 (somma delle
        # medie orarie) e massimo istantaneo della giornata.
        ore = defaultdict(int)
        tot = defaultdict(float)
        picco = defaultdict(float)
        for g, mn, mx in cur.execute("""
            SELECT date(start_ts,'unixepoch','localtime'), mean, max
            FROM statistics
            WHERE metadata_id=? AND mean IS NOT NULL
        """, (mid,)):
            ore[g] += 1
            tot[g] += mn
            v = mx if mx is not None else mn
            if v > picco[g]:
                picco[g] = v

        conn.close()

        # Massimo istantaneo di ciascun mese, calcolato sui soli giorni validi:
        # un giorno con la stazione ferma non deve abbassare il riferimento.
        validi = defaultdict(list)
        scartati = defaultdict(int)
        for g in sorted(ore):
            if ore[g] >= ORE_MINIME and tot[g] >= WH_MINIMI:
                validi[g[:7]].append(g)
            else:
                scartati[g[:7]] += 1

        mese_corrente = date.today().strftime("%Y-%m")

        righe = []
        for ym in sorted(validi):
            if ym >= mese_corrente:
                continue
            giorni = validi[ym]
            # Riferimento robusto: terzo massimo giornaliero del mese. Con meno
            # di tre giorni validi si usa il piu' alto disponibile.
            ordinati = sorted((picco[g] for g in giorni), reverse=True)
            max_mese = ordinati[min(POSIZIONE_RIFERIMENTO - 1, len(ordinati) - 1)]
            soglia = max_mese * FRAZIONE_SOGLIA
            senza = [g for g in giorni if picco[g] < soglia]
            righe.append({
                "mese":     fmt_mese(ym),
                "giorni":   len(senza),
                "validi":   len(giorni),
                "scartati": scartati.get(ym, 0),
                "soglia":   round(soglia),
                "max_mese": round(max_mese),   # riferimento, non il massimo assoluto
                # v2: solo giorno e valore, senza unita' di misura: l'unita'
                # sta nell'intestazione della colonna in plancia.
                "elenco":   ", ".join(
                    f"{g[8:]} ({round(picco[g])})"
                    for g in sorted(senza, key=lambda g: picco[g])
                ),
            })

        print(json.dumps({"mesi": righe[-MESI_GRAFICO:]}, ensure_ascii=False))

    except Exception as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
