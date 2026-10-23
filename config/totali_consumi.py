# totali_consumi_20.py
# v20 (24/10/2026): verificato sul DB. Le variazioni sopra il limite sono
#   recuperi reali: l'inverter non ha aggiornato il contatore per qualche
#   ora e poi lo ha riallineato in un colpo (03/04 ore 18: produzione +40,9,
#   immissione +31,0 kWh; 22/06 ore 12: produzione +19,2 kWh). Vanno contati.
#   L'unica anomalia e' 09/05 ore 12, immissione -1.182,7 kWh dopo 13 ore
#   senza dati: si scartano solo i cali oltre il limite. Le v18 e v19
#   scartavano anche i recuperi (aprile 405 invece di 415, giugno 606
#   invece di 625).
# v19 (24/10/2026): si tengono anche le variazioni negative piccole (entro
#   il limite orario) e si scartano solo i salti oltre il limite, in tutte e
#   due le direzioni. Con la v18 un calo seguito dal recupero contava solo
#   il recupero (giugno 2026: autoconsumo 606 kWh contro 625 della
#   differenza fra inizio e fine mese). Il salto di maggio (-1.100 kWh)
#   resta escluso.
# v18 (24/10/2026): il limite di 10 kWh vale per ogni ora trascorsa: dopo
#   un'interruzione delle statistiche (HA fermo) la prima variazione copre
#   piu' ore e con la v17 veniva scartata, perdendo l'energia di quelle ore
#   (giugno 606 kWh contro 625, luglio 750 contro 761).
# v17 (24/10/2026): risparmio FV mese per mese. Prima: autoconsumo totale x
#   prezzo di OGGI. Ora: per ogni mese autoconsumo del mese x prezzo luce
#   del mese, ricostruito dal PUN mensile (prezzi:pun_mese, prezzi_mensili.py)
#   con la formula Plenitude ((PUN + contributo) x 1,1 + dispacciamento +
#   rete + oneri + accisa) x 1,1 e le componenti attuali; per i mesi senza
#   PUN (il mese in corso) il prezzo corrente. L'autoconsumo mensile somma
#   le variazioni orarie di produzione e immissione scartando quelle
#   negative o sopra 10 kWh/ora: a maggio 2026 la statistica di immissione
#   ha un salto all'indietro di circa 1.100 kWh che con MAX(sum)-MIN(sum)
#   gonfiava l'autoconsumo. Il JSON riporta anche pv_mesi per il controllo.
# v16 (23/10/2026): statistiche cumulate anche per elettricita' EUR, gas EUR e
#   rete/GSE EUR (rilevato e bolletta). I grafici mensili della plancia
#   Consumi passano tutti a queste statistiche con stat_types change: prima
#   mostravano il MAX del sensore mensile, che il giorno 1 riportava il
#   totale del mese precedente.
# v15 (23/10/2026): il rilevato mensile (sensor.*_mese_*) non conta piu'
#   l'ora 00:00-01:00 del giorno 1. Le statistiche orarie includono il valore
#   in vigore all'inizio dell'ora, cioe' il totale del mese PRIMA
#   dell'azzeramento: il MAX del mese nuovo diventava il totale del mese
#   precedente (acqua: settembre 2026 = 32,65 m3 come agosto, vero 24,2).
#   Nessun consumo si perde: nella prima ora il contatore del mese e' a zero
#   (acqua) o quel consumo ricompare nel massimo delle ore successive
#   (utility_meter). Le serie input_number.bolletta_* (una riga al giorno 1
#   alle 00:00) restano lette come prima.
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

# NUOVO v13 (statistiche CUMULATE per i grafici annuali):
#  * Le card statistics-graph con period: year mostravano il MAX del mese
#    piu' alto, non la somma: le serie mensili (utility_meter a reset mensile
#    e input_number.bolletta_* iniettati da salva_bolletta) non hanno la
#    colonna 'sum', quindi HA non puo' calcolare 'change' sull'anno.
#  * Ora, a ogni esecuzione (avvio, ogni ora, subito dopo Salva bolletta),
#    per ogni serie in CUMULATI si ricostruisce da zero una statistica
#    ESTERNA totali_consumi:<nome> con has_sum=1: una riga al 1 di ogni mese,
#    state = valore del mese, sum = cumulato fino a quel mese incluso, piu'
#    una riga a zero nel mese precedente al primo dato.
#  * Le card annuali usano stat_types: change su queste statistiche:
#    change(anno) = sum fine anno - sum fine anno precedente = somma dei mesi.
#  * Le serie mensili originali NON vengono toccate.

import sqlite3, json, time
from datetime import datetime, timezone

DB = "/config/home-assistant_v2.db"

# ── Serie mensili (una riga per mese, valore = MAX(max) del mese) ───────────
# Ritorna dict {mese 'YYYY-MM': valore} per i soli mesi con valore > 0.
MENSILE_SQL = """
SELECT strftime('%Y-%m', datetime(start_ts, 'unixepoch', 'localtime')) AS mese,
       IFNULL(MAX(max), 0) AS v
FROM statistics
WHERE metadata_id=(SELECT id FROM statistics_meta WHERE statistic_id=?)
  AND NOT (? = 1 AND strftime('%d %H', datetime(start_ts, 'unixepoch', 'localtime')) = '01 00')
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
    # v15: per i sensori rilevati si salta la prima ora del mese (riporto
    # del totale del mese precedente); le bollette iniettate stanno proprio
    # in quell'ora e si leggono tutte.
    salta = 1 if entity.startswith("sensor.") else 0
    rows = cur.execute(MENSILE_SQL, (entity, salta)).fetchall()
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


MAX_KWH_ORA = 10.0   # impianto e contatore: oltre e' un salto dei dati


def energia_mensile(cur, entity):
    """{mese locale: kWh} dalle variazioni orarie di 'sum', scartando le
    solo i cali oltre MAX_KWH_ORA per ogni ora trascorsa (gli aumenti grandi
    sono recuperi dell'inverter e si contano)."""
    righe = cur.execute("""SELECT s.start_ts, s.sum FROM statistics s
        JOIN statistics_meta m ON s.metadata_id = m.id
        WHERE m.statistic_id = ? AND s.sum IS NOT NULL
        ORDER BY s.start_ts""", (entity,)).fetchall()
    out, prec, prec_ts = {}, None, None
    for ts, v in righe:
        if prec is not None:
            d = v - prec
            ore = max(1.0, (ts - prec_ts) / 3600)
            if d >= -MAX_KWH_ORA * ore:
                k = datetime.fromtimestamp(ts).strftime("%Y-%m")
                out[k] = out.get(k, 0.0) + d
        prec, prec_ts = v, ts
    return out


def num_stato(cur, eid):
    try:
        return float(get_state(cur, eid))
    except (TypeError, ValueError):
        return 0.0


def calc_pv_risparmio(cur, prezzo):
    """v17: somma mensile di autoconsumo x prezzo del mese.
    Ritorna (risparmio, {mese: [kWh autoconsumo, EUR/kWh]})."""
    prod = energia_mensile(cur, PV_PRODUZIONE_ENTITY)
    exp = energia_mensile(cur, PV_EXPORT_ENTITY)
    pun = serie_mensile(cur, "prezzi:pun_mese")
    c = {k: num_stato(cur, f"input_number.elettricita_{k}_kwh")
         for k in ("contributo", "dispacciamento", "rete", "oneri", "accisa")}
    totale, mesi = 0.0, {}
    for k in sorted(prod):
        auto = max(0.0, prod[k] - exp.get(k, 0.0))
        if k in pun and c["contributo"] > 0:
            p = ((pun[k] + c["contributo"]) * 1.1 + c["dispacciamento"]
                 + c["rete"] + c["oneri"] + c["accisa"]) * 1.1
        else:
            p = prezzo
        totale += auto * p
        mesi[k] = [round(auto, 1), round(p, 4)]
    return round(totale, 2), mesi


# ── Statistiche cumulate per i grafici annuali (v13) ────────────────────────
# (nome, serie mensile sorgente, unita', unit_class, nome leggibile)
CUMULATI = [
    ("gas_mc_rilevato",     "sensor.gas_mese_mc",                "m³",  "volume", "Gas m³ rilevato cumulato"),
    ("gas_mc_bolletta",     "input_number.bolletta_gas_mc",      "m³",  "volume", "Gas m³ bolletta cumulato"),
    ("elec_kwh_rilevato",   "sensor.elettricita_mese_kwh",       "kWh", "energy", "Elettricità kWh rilevato cumulato"),
    ("elec_kwh_bolletta",   "input_number.bolletta_elec_kwh",    "kWh", "energy", "Elettricità kWh bolletta cumulato"),
    ("acqua_mc_rilevato",   "sensor.acqua_mese_mc",              "m³",  "volume", "Acqua m³ rilevato cumulato"),
    ("acqua_mc_bolletta",   "input_number.bolletta_acqua_mc",    "m³",  "volume", "Acqua m³ bolletta cumulato"),
    ("acqua_euro_rilevato", "sensor.acqua_mese_euro",            "€",   None,     "Acqua € rilevato cumulato"),
    ("acqua_euro_bolletta", "input_number.bolletta_acqua_euro",  "€",   None,     "Acqua € bolletta cumulato"),
    # v16
    ("elec_euro_rilevato",  "sensor.elettricita_mese_euro",      "€",   None,     "Elettricità € rilevato cumulato"),
    ("elec_euro_bolletta",  "input_number.bolletta_elec_euro",   "€",   None,     "Elettricità € bolletta cumulato"),
    ("gas_euro_rilevato",   "sensor.gas_mese_euro",              "€",   None,     "Gas € rilevato cumulato"),
    ("gas_euro_bolletta",   "input_number.bolletta_gas_euro",    "€",   None,     "Gas € bolletta cumulato"),
    ("gse_euro_stimato",    "sensor.a_rete_mese_euro",           "€",   None,     "Rete € stimato cumulato"),
    ("gse_euro_bolletta",   "input_number.bolletta_gse_euro",    "€",   None,     "GSE € pagato cumulato"),
]
CUMULATI_SOURCE = "totali_consumi"


def meta_cumulato(cur, sid, unit, unit_class, nome):
    """id di statistics_meta per una statistica esterna con somma.
    Se esiste gia' ne riallinea unita'/flag, altrimenti la crea."""
    r = cur.execute("SELECT id FROM statistics_meta WHERE statistic_id=?",
                    (sid,)).fetchone()
    if r:
        cur.execute("""UPDATE statistics_meta
                       SET source=?, unit_of_measurement=?, unit_class=?,
                           has_mean=0, has_sum=1, name=?, mean_type=0
                       WHERE id=?""",
                    (CUMULATI_SOURCE, unit, unit_class, nome, r[0]))
        return r[0]
    cur.execute("""INSERT INTO statistics_meta
        (statistic_id, source, unit_of_measurement, unit_class,
         has_mean, has_sum, name, mean_type)
        VALUES (?, ?, ?, ?, 0, 1, ?, 0)""",
        (sid, CUMULATI_SOURCE, unit, unit_class, nome))
    return cur.lastrowid


def inizio_mese(ym):
    """(start_ts, start) al 1 del mese 00:00 UTC (stessa convenzione di
    salva_bolletta: in ora locale cade sempre nello stesso mese e anno)."""
    y, m = map(int, ym.split("-"))
    d = datetime(y, m, 1, tzinfo=timezone.utc)
    return d.timestamp(), d.strftime("%Y-%m-%dT%H:%M:%S.000000")


def mese_precedente(ym):
    y, m = map(int, ym.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def aggiorna_cumulati(cur):
    """Ricostruisce tutte le statistiche cumulate. Ritorna {nome: n_mesi}."""
    now_ts = time.time()
    now_s = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000")
    esito = {}
    for nome, sorgente, unit, uc, nome_lungo in CUMULATI:
        sid = f"{CUMULATI_SOURCE}:{nome}"
        mid = meta_cumulato(cur, sid, unit, uc, nome_lungo)
        cur.execute("DELETE FROM statistics WHERE metadata_id=?", (mid,))
        cur.execute("DELETE FROM statistics_short_term WHERE metadata_id=?", (mid,))
        serie = serie_mensile(cur, sorgente)
        if not serie:
            esito[nome] = 0
            continue
        mesi = sorted(serie)
        righe = [(mese_precedente(mesi[0]), 0.0, 0.0)]
        cumulato = 0.0
        for ym in mesi:
            cumulato += serie[ym]
            righe.append((ym, serie[ym], cumulato))
        for ym, valore, somma in righe:
            ts, s = inizio_mese(ym)
            cur.execute("""INSERT INTO statistics
                (created, created_ts, metadata_id, start, start_ts,
                 mean, mean_weight, min, max, last_reset, last_reset_ts,
                 state, sum)
                VALUES (?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,?,?)""",
                (now_s, now_ts, mid, s, ts, round(valore, 4), round(somma, 4)))
        esito[nome] = len(mesi)
    return esito


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
        pv_risparmio, pv_mesi = calc_pv_risparmio(cur, prezzo)
        out["pv_mesi"] = pv_mesi

        gse_serie = serie_mensile(cur, "input_number.bolletta_gse_euro")
        pv_gse = round(sum(gse_serie.values()), 2)

        out["pv_risparmio"]    = pv_risparmio
        out["pv_gse"]          = pv_gse
        out["pv_totale"]       = round(pv_risparmio + pv_gse, 2)
        out["pv_recupero_pct"] = round((out["pv_totale"] / COSTO_IMPIANTO) * 100, 2)

        # ── v13: statistiche cumulate per i grafici annuali ──
        # Errore qui non deve bloccare i totali gia' calcolati.
        try:
            aggiorna_cumulati(cur)
            conn.commit()
        except Exception as e:
            conn.rollback()
            out["cumulati_error"] = str(e)

        print(json.dumps(out))

    except Exception as e:
        print(json.dumps({"error": str(e)}))
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()
