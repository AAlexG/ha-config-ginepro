# totali_consumi_14.py
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
        pv_risparmio = calc_pv_risparmio(cur, prezzo)

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
