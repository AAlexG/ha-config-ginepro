# prezzi_mensili_2.py
# v2 (24/10/2026): indirizzo ARERA con www (arera.it senza www non si
#   risolve dalla rete di casa: gaierror -3) ed errori di rete riportati per
#   intero, non solo il tipo.
#
# Prezzi di riferimento mensili di Casa Ginepro, per i grafici e per il
# prezzo del gas a consumo. Chiamato da shell_command.prezzi_mensili
# (script.prezzi_mensili_aggiorna, config_gas): ogni mattina, all'avvio di
# HA e dopo l'importazione di una bolletta.
#
# Fonti:
#   PSV gas  - ARERA, "Valore CMEM vulnerabili": media mensile del PSV day
#              ahead in EUR/Smc, pubblicata entro i primi 2 giorni lavorativi
#              del mese successivo.
#              https://www.arera.it/area-operatori/prezzi-e-tariffe/valore-cmemm-vulnerabili
#   PUN luce - mesi gia' fatturati: dalla bolletta Plenitude (Corrispettivo
#              Luce Index / 1,1, cioe' il PUN applicato); mesi chiusi non
#              ancora fatturati: ultimo valore del mese di
#              sensor.pun_mono_orario (media del mese a fine mese).
#   Gas      - dall'ultima bolletta Plenitude in SORGENTI/BOLLETTE: prezzo
#              gas a consumo, Corrispettivo Gas Index e mese (gas_base di
#              bollette.py).
#
# Scrive nel DB due statistiche ESTERNE, ricostruite da zero a ogni
# esecuzione, una riga al 1 di ogni mese (stessa convenzione di
# salva_bolletta e totali_consumi):
#   prezzi:pun_mese  EUR/kWh  dal primo mese con elettricita' rilevata
#   prezzi:psv_mese  EUR/Smc  dal primo mese con gas rilevato
# Stampa un JSON letto da script.prezzi_mensili_aggiorna:
#   psv_smc, psv_mese        ultimo PSV ARERA pubblicato
#   psv_rif_smc              PSV ARERA del mese dell'ultima bolletta gas
#   gas_prezzo_bolletta, gas_indice_bolletta, gas_mese_bolletta
#   pun_mesi, psv_mesi       mesi scritti; errori: elenco testuale
#
# Le bollette gia' lette e l'ultima tabella ARERA restano in
# /config/.prezzi_cache.json: se ARERA non risponde si usa la copia.

import datetime as dt
import glob
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request

CONFIG = "/config"
DB = os.path.join(CONFIG, "home-assistant_v2.db")
BOLLETTE = os.path.join(CONFIG, "SORGENTI", "BOLLETTE")
CACHE = os.path.join(CONFIG, ".prezzi_cache.json")
URL_ARERA = "https://www.arera.it/area-operatori/prezzi-e-tariffe/valore-cmemm-vulnerabili"
SOURCE = "prezzi"

MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
        "agosto", "settembre", "ottobre", "novembre", "dicembre"]
RE_NUM = re.compile(r"^\d+,\d+$")
RE_ANNO = re.compile(r"^(20\d\d)$")

errori = []


def carica_cache():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def salva_cache(c):
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False)
    os.replace(tmp, CACHE)


# ----------------------------------------------------------------------------
# ARERA
# ----------------------------------------------------------------------------
def psv_arera(cache):
    """{'YYYY-MM': EUR/Smc}. Le celle della tabella diventano righe: un anno
    (riga sola, 4 cifre), poi per ogni mese il nome e tre numeri
    (EUR/MWh, EUR/GJ, EUR/Smc)."""
    try:
        req = urllib.request.Request(URL_ARERA, headers={
            "User-Agent": "Mozilla/5.0 (HomeAssistant prezzi_mensili)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            pagina = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        errori.append(f"ARERA non raggiungibile ({e!r:.150}), uso la copia")
        return cache.get("arera", {})
    testo = re.sub(r"<[^>]+>", "\n", pagina)
    righe = [html.unescape(x).strip().strip("*").strip() for x in testo.splitlines()]
    righe = [x for x in righe if x]
    out, anno, i = {}, None, 0
    while i < len(righe):
        x = righe[i]
        m = RE_ANNO.match(x)
        if m:
            anno = int(m[1])
        elif anno and x.lower() in MESI:
            nums = []
            j = i + 1
            while j < len(righe) and len(nums) < 3 and RE_NUM.match(righe[j]):
                nums.append(float(righe[j].replace(",", ".")))
                j += 1
            if len(nums) == 3:
                out[f"{anno}-{MESI.index(x.lower()) + 1:02d}"] = nums[2]
                i = j
                continue
        i += 1
    if len(out) < 6:
        errori.append(f"tabella ARERA non riconosciuta ({len(out)} mesi), uso la copia")
        return cache.get("arera", {})
    # Controllo di coerenza: EUR/Smc plausibili
    if not all(0.05 < v < 5 for v in out.values()):
        errori.append("valori ARERA fuori scala, uso la copia")
        return cache.get("arera", {})
    cache["arera"] = out
    return out


# ----------------------------------------------------------------------------
# Bollette Plenitude archiviate
# ----------------------------------------------------------------------------
def bollette_plenitude(cache):
    """Lista di dict {indici, gas_base} per ogni Plenitude leggibile."""
    sys.path.insert(0, CONFIG)
    try:
        import bollette
    except Exception as e:
        errori.append(f"bollette.py non caricabile: {e}")
        return []
    viste = cache.setdefault("bollette", {})
    out = []
    for p in sorted(glob.glob(os.path.join(BOLLETTE, "Plenitude_*.pdf"))):
        chiave = f"{os.path.basename(p)}|{int(os.path.getmtime(p))}"
        if chiave not in viste:
            try:
                d = bollette.leggi(p)
                viste[chiave] = {"indici": d.get("indici", {}),
                                 "gas_base": d.get("gas_base")}
            except Exception as e:
                viste[chiave] = {"errore": str(e)}
        if "errore" not in viste[chiave]:
            out.append(viste[chiave])
    # Via le voci di file non piu' presenti
    presenti = {f"{os.path.basename(p)}|{int(os.path.getmtime(p))}"
                for p in glob.glob(os.path.join(BOLLETTE, "Plenitude_*.pdf"))}
    for k in list(viste):
        if k not in presenti:
            del viste[k]
    return out


# ----------------------------------------------------------------------------
# Statistiche HA
# ----------------------------------------------------------------------------
def mese_locale(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m")


def primo_mese(cur, sid):
    """Primo mese (locale) con valore > 0 del sensore mensile, None se mai."""
    r = cur.execute("""SELECT MIN(s.start_ts) FROM statistics s
        JOIN statistics_meta m ON s.metadata_id = m.id
        WHERE m.statistic_id = ? AND s.max > 0""", (sid,)).fetchone()
    return mese_locale(r[0]) if r and r[0] else None


def pun_fine_mese(cur):
    """{'YYYY-MM': valore} = media dell'ultima ora del mese di
    sensor.pun_mono_orario, per i soli mesi chiusi."""
    righe = cur.execute("""SELECT s.start_ts, s.mean FROM statistics s
        JOIN statistics_meta m ON s.metadata_id = m.id
        WHERE m.statistic_id = 'sensor.pun_mono_orario' AND s.mean > 0
        ORDER BY s.start_ts""").fetchall()
    corrente = dt.datetime.now().strftime("%Y-%m")
    out = {}
    for ts, v in righe:
        k = mese_locale(ts)
        if k < corrente:
            out[k] = v          # l'ultima riga del mese vince
    return out


def meta(cur, sid, unit, nome):
    r = cur.execute("SELECT id FROM statistics_meta WHERE statistic_id=?",
                    (sid,)).fetchone()
    if r:
        cur.execute("""UPDATE statistics_meta SET source=?, unit_of_measurement=?,
                       unit_class=NULL, has_mean=1, has_sum=0, name=?, mean_type=1
                       WHERE id=?""", (SOURCE, unit, nome, r[0]))
        return r[0]
    cur.execute("""INSERT INTO statistics_meta
        (statistic_id, source, unit_of_measurement, unit_class,
         has_mean, has_sum, name, mean_type)
        VALUES (?, ?, ?, NULL, 1, 0, ?, 1)""", (sid, SOURCE, unit, nome))
    return cur.lastrowid


def scrivi_serie(cur, sid, unit, nome, serie):
    mid = meta(cur, sid, unit, nome)
    cur.execute("DELETE FROM statistics WHERE metadata_id=?", (mid,))
    cur.execute("DELETE FROM statistics_short_term WHERE metadata_id=?", (mid,))
    now_ts = time.time()
    now_s = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000")
    for k, v in sorted(serie.items()):
        y, m = map(int, k.split("-"))
        d = dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)
        cur.execute("""INSERT INTO statistics
            (created, created_ts, metadata_id, start, start_ts,
             mean, mean_weight, min, max, last_reset, last_reset_ts, state, sum)
            VALUES (?,?,?,?,?,?,1,?,?,NULL,NULL,NULL,NULL)""",
            (now_s, now_ts, mid, d.strftime("%Y-%m-%dT%H:%M:%S.000000"),
             d.timestamp(), v, v, v))
    return len(serie)


def main():
    cache = carica_cache()
    arera = psv_arera(cache)
    plen = bollette_plenitude(cache)

    # PUN dalle bollette e Gas Index, ultima bolletta gas
    pun_bollette, base = {}, None
    for b in plen:
        for k, v in b["indici"].items():
            if "pun" in v:
                pun_bollette[k] = v["pun"]
        g = b.get("gas_base")
        if g and (base is None or g["mese"] > base["mese"]):
            base = g

    esito = {"errori": errori}
    con = sqlite3.connect(DB, timeout=30)
    try:
        cur = con.cursor()
        primo_e = primo_mese(cur, "sensor.elettricita_mese_kwh")
        primo_g = primo_mese(cur, "sensor.gas_mese_mc")
        pun = pun_fine_mese(cur)
        pun.update(pun_bollette)             # la bolletta vince sul sensore
        pun = {k: round(v, 6) for k, v in pun.items() if primo_e and k >= primo_e}
        psv = {k: v for k, v in arera.items() if primo_g and k >= primo_g}
        esito["pun_mesi"] = scrivi_serie(cur, f"{SOURCE}:pun_mese", "€/kWh",
                                         "PUN mensile", pun)
        esito["psv_mesi"] = scrivi_serie(cur, f"{SOURCE}:psv_mese", "€/Smc",
                                         "PSV mensile ARERA", psv)
        con.commit()
    finally:
        con.close()

    if arera:
        ultimo = max(arera)
        esito["psv_mese"] = ultimo
        esito["psv_smc"] = arera[ultimo]
    if base:
        esito["gas_prezzo_bolletta"] = base["prezzo"]
        esito["gas_indice_bolletta"] = base["indice"]
        esito["gas_mese_bolletta"] = base["mese"]
        if base["mese"] in arera:
            esito["psv_rif_smc"] = arera[base["mese"]]
        else:
            errori.append(f"PSV ARERA di {base['mese']} non disponibile")
    else:
        errori.append("nessuna bolletta Plenitude con prezzo gas in SORGENTI/BOLLETTE")
    salva_cache(cache)
    print(json.dumps(esito, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"errori": [f"{e.__class__.__name__}: {e}"]}, ensure_ascii=False))
