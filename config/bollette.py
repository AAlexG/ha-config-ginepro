# bollette_3.py
# v3 (24/10/2026): Plenitude restituisce anche "indici" (per mese: PUN dal
#   Corrispettivo Luce Index / 1,1 e Corrispettivo Gas Index in EUR/Smc,
#   dalla tabella "Mese di riferimento" del box offerta) e "gas_base"
#   (prezzo gas a consumo, Gas Index e mese dell'ultimo mese fatturato).
#   Il prezzo gas NON e' piu' fra i parametri: input_number.gas_prezzo_mc
#   lo scrive ora sensor.gas_prezzo_smc_variabile (config_gas), che parte
#   da gas_base e segue il PSV mensile ARERA (prezzi_mensili.py).
# v2 (23/10/2026): IRETI con riconguaglio anno solare (escluso, righe senza
#   date) e restituzione di acconti su mesi precedenti al periodo (ripartita
#   sui suoi giorni, mesi precedenti come correzioni); Plenitude in formato
#   vecchio (prima del 2026) rifiutata con messaggio chiaro. Collaudo:
#   IRETI 07-09/2025 e 07-09/2026, Plenitude 06-07/2026.
#
# Lettura delle bollette PDF di Casa Ginepro e ripartizione per mese.
# Usato da installa.py: un PDF messo in /config/da_installare viene letto
# qui, i valori mensili vanno a salva_bolletta.py e i parametri (quote
# fisse, componenti della luce, prezzo gas) agli input_number di HA.
#
# Fornitori riconosciuti:
#   Plenitude luce+gas (Trend Casa Base), bimestrale
#     POD IT001E00585867 / PDR 00881407651205 (Via Ginepro 1)
#   IRETI servizio idrico, trimestrale
#     punto di fornitura 0701001760010470 (Via Ginepro 1)
# Bollette di altre utenze (es. Via Gioiello 98) vengono rifiutate.
#
# Regole di ripartizione (le stesse usate a mano fino al 23/10/2026):
#   Plenitude: ogni riga degli "Elementi di dettaglio" va nel mese del suo
#     periodo DAL-AL; le righe che coprono piu' mesi (quota fissa,
#     potenza, sconti) si dividono in parti uguali sui mesi. IVA per mese
#     sull'imponibile del mese (10% luce, 22% gas), con l'ultimo mese che
#     chiude sull'IVA della bolletta. Canone RAI escluso (non e' luce).
#   IRETI: ogni intervallo fra due letture porta i propri importi
#     (acquedotto, fognatura, oneri) e i propri m3, ripartiti giorno per
#     giorno; le quote fisse sui giorni del loro periodo; la restituzione
#     degli acconti sui giorni a cui si riferiva. I mesi prima del periodo
#     della bolletta sono correzioni del valore gia' in archivio (meno
#     l'acconto restituito, piu' il consumo reale); installa.py le applica.
#     IVA come rapporto IVA/imponibile. Conguagli di anni precedenti
#     ("Riconguaglio anno solare") esclusi.
#   Gas: coefficiente C = 1, quindi Smc = m3.
# Parametri (dall'ultimo mese della bolletta, IVA inclusa salvo dove detto):
#   quote fisse mensili di luce, gas e acqua; componenti a kWh della luce
#   IVA esclusa (contributo, dispacciamento, rete, oneri ASOS+ARIM, accisa)
#   per sensor.elettricita_prezzo_kwh_variabile; prezzo gas a consumo allo
#   scaglione piu' alto raggiunto; prezzo acqua medio effettivo.
# Controlli: la somma dei mesi deve tornare con i totali della bolletta
# (imponibile e IVA). Qualsiasi riga non riconosciuta o mese fuori dal
# periodo fatturato (ricalcoli di bollette vecchie) ferma la lettura: la
# bolletta va inserita a mano.
#
# Testo dei PDF con pypdf (solo Python), copiato in /config/lib.

import calendar
import datetime as dt
import re
import sys

sys.path.insert(0, "/config/lib")

POD_LUCE = "IT001E00585867"
PDR_GAS = "00881407651205"
PDF_ACQUA = "0701001760010470"

MESI = ["GENNAIO", "FEBBRAIO", "MARZO", "APRILE", "MAGGIO", "GIUGNO", "LUGLIO",
        "AGOSTO", "SETTEMBRE", "OTTOBRE", "NOVEMBRE", "DICEMBRE"]


class Errore(Exception):
    pass


def testo_pdf(percorso):
    from pypdf import PdfReader
    r = PdfReader(percorso)
    righe = []
    for p in r.pages:
        t = p.extract_text(extraction_mode="layout") or ""
        for riga in t.splitlines():
            riga = re.sub(r"\s+", " ", riga).strip()
            if riga:
                righe.append(riga)
    return righe


def num(s):
    return float(s.replace(".", "").replace(",", "."))


def data(s):
    return dt.datetime.strptime(s, "%d/%m/%Y").date()


def ym(g):
    return f"{g.year}-{g.month:02d}"


def mesi_fra(a, b):
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def giorni(a, b):
    g = a
    while g <= b:
        yield g
        g += dt.timedelta(days=1)


def chiudi(valori, totale, dec=2):
    """Arrotonda i valori mensili e mette la differenza sull'ultimo mese,
    cosi' la somma coincide con il totale della bolletta."""
    chiavi = sorted(valori)
    out = {k: round(valori[k], dec) for k in chiavi}
    if chiavi:
        out[chiavi[-1]] = round(out[chiavi[-1]] + totale - sum(out.values()), dec)
    return out


def leggi(percorso):
    righe = testo_pdf(percorso)
    tutto = "\n".join(righe)
    if "Plenitude" in tutto and "Periodo di fatturazione:" in tutto \
            and "Elementi di dettaglio" in tutto:
        return _plenitude(righe, tutto)
    if "Plenitude" in tutto:
        raise Errore("Plenitude in formato vecchio (prima del 2026, senza "
                     "Elementi di dettaglio): inserimento a mano")
    if "IRETI" in tutto and "Servizio Idrico" in tutto:
        return _ireti(righe, tutto)
    raise Errore("bolletta non riconosciuta (ne' Plenitude ne' IRETI)")


# ----------------------------------------------------------------------------
# PLENITUDE
# ----------------------------------------------------------------------------
RE_RIGA_PL = re.compile(
    r"DAL (\d\d/\d\d/\d{4}) AL (\d\d/\d\d/\d{4}) (\S+) (.*?) ?"
    r"(-?\d{1,3}(?:\.\d{3})*,\d\d) (ORD|AGE)$")


def _sezione_pl(righe, inizio, fine):
    """Righe di dettaglio fra 'Elementi di dettaglio <X>' e 'Riepilogo <x>'.
    Ritorna lista di dict con sezione, voce, periodo, unita', numeri, euro."""
    out, dentro, sezione, voce = [], False, "", ""
    for r in righe:
        if r.startswith(inizio):
            dentro = True
            continue
        if not dentro:
            continue
        if r.startswith(fine):
            break
        if r.startswith(("Spesa per", "Componente", "Totale imposte")):
            sezione = r
        if " tipo prezzo" in r:
            voce = r.split(" tipo prezzo")[0].strip()
            continue
        m = RE_RIGA_PL.search(r)
        if not m:
            continue
        a, b = data(m[1]), data(m[2])
        nums = re.findall(r"-?\d+(?:\.\d{3})*,\d+|-?\d+", m[4])
        out.append({"sezione": sezione, "voce": voce, "a": a, "b": b,
                    "unita": m[3], "nums": [num(x) for x in nums],
                    "euro": num(m[5])})
    if not out:
        raise Errore(f"nessuna riga in '{inizio}'")
    return out


def _totale(tutto, etichetta, dopo):
    i = tutto.find(dopo)
    m = re.search(re.escape(etichetta) + r" (-?\d{1,3}(?:\.\d{3})*,\d\d)", tutto[i:])
    if i < 0 or not m:
        raise Errore(f"manca '{etichetta}'")
    return num(m[1])


def _plenitude(righe, tutto):
    if POD_LUCE not in tutto or PDR_GAS not in tutto:
        raise Errore("Plenitude non di Casa Ginepro (POD/PDR diversi): non inserita")
    m = re.search(r"Periodo di fatturazione: dal (\d\d/\d\d/\d{4}) al (\d\d/\d\d/\d{4})", tutto)
    if not m:
        raise Errore("periodo di fatturazione non trovato")
    p_a, p_b = data(m[1]), data(m[2])
    mesi = mesi_fra(p_a, p_b)
    fattura = re.search(r"Fattura elettronica numero (\d+)", tutto)
    out = {"fornitore": "Plenitude", "fattura": fattura[1] if fattura else "?",
           "periodo": f"{p_a:%d/%m/%Y}-{p_b:%d/%m/%Y}",
           "mesi": {k: {} for k in mesi}, "parametri": {}, "note": []}

    for nome, inizio, fine, voce_q, iva_pct, chiave in (
            ("luce", "Elementi di dettaglio Luce", "Riepilogo luce",
             "Corrispettivo Luce Index", 0.10, "elec"),
            ("gas", "Elementi di dettaglio Gas", "Riepilogo gas",
             "Corrispettivo Gas Index", 0.22, "gas")):
        det = _sezione_pl(righe, inizio, fine)
        imp = {k: 0.0 for k in mesi}
        qta = {k: 0.0 for k in mesi}
        fissa = {k: 0.0 for k in mesi}
        for x in det:
            ms = mesi_fra(x["a"], x["b"])
            fuori = [k for k in ms if k not in imp]
            if fuori:
                raise Errore(f"{nome}: riga {x['voce']} per {', '.join(fuori)}, fuori "
                             "dal periodo (ricalcolo): inserimento manuale")
            for k in ms:
                imp[k] += x["euro"] / len(ms)
                if "/mese" in x["unita"]:
                    fissa[k] += x["euro"] / len(ms)
            if x["voce"] == voce_q:
                if len(ms) != 1:
                    raise Errore(f"{nome}: consumo su piu' mesi in una riga")
                qta[ms[0]] += x["nums"][1]
        netto = _totale(tutto, "Totale netto IVA", fine)
        iva = _totale(tutto, "IVA su imponibile di euro", fine)
        if abs(sum(imp.values()) - netto) > 0.02:
            raise Errore(f"{nome}: somma righe {sum(imp.values()):.2f} diversa da "
                         f"imponibile {netto:.2f}")
        iva_m = chiudi({k: imp[k] * iva_pct for k in mesi}, iva)
        tot = chiudi({k: imp[k] + iva_m[k] for k in mesi}, netto + iva)
        for k in mesi:
            out["mesi"][k][chiave + ("_kwh" if chiave == "elec" else "_mc")] = round(qta[k], 3)
            out["mesi"][k][chiave + "_euro"] = tot[k]
        out["note"].append(f"{nome} {netto + iva:.2f} €")

        ultimo = mesi[-1]
        in_ultimo = [x for x in det if mesi_fra(x["a"], x["b"])[-1] == ultimo]
        out["parametri"][f"input_number.{'elettricita' if nome == 'luce' else 'gas'}_quota_fissa_mese"] = \
            round(fissa[ultimo] * (1 + iva_pct), 2)
        if nome == "luce":
            out["parametri"].update(_param_luce(in_ultimo))
        else:
            pg = _prezzo_gas(in_ultimo, iva_pct)
            if pg:
                out["gas_base"] = {"prezzo": pg, "mese": ultimo}
            else:
                out["note"].append("prezzo gas non aggiornato (nessun consumo nell'ultimo mese)")
    out["indici"] = _indici(righe)
    if "gas_base" in out:
        ig = out["indici"].get(out["gas_base"]["mese"], {}).get("gas_indice")
        if ig:
            out["gas_base"]["indice"] = ig
        else:
            del out["gas_base"]
            out["note"].append("Gas Index dell'ultimo mese non trovato")
    return out


MESI_NOMI = {n.capitalize(): i + 1 for i, n in enumerate(MESI)}
RE_IND_LUCE = re.compile(r"(?:^| )([A-Z][a-z]+) (\d{4}) (\d+,\d+) €/kWh (\d+,\d+) €/kWh (\d+,\d+) €/kWh$")
RE_IND_GAS = re.compile(r"(?:^| )([A-Z][a-z]+) (\d{4}) (\d+,\d+) €/smc (\d+,\d+) €/smc$")


def _indici(righe):
    """Tabelle "Mese di riferimento" del box offerta: per ogni mese PUN
    (Corrispettivo Luce Index * 1,1 diviso 1,1) e Corrispettivo Gas Index,
    in EUR IVA esclusa."""
    out = {}
    for r in righe:
        r = r.strip()
        m = RE_IND_LUCE.search(r)
        if m and m[1] in MESI_NOMI:
            k = f"{m[2]}-{MESI_NOMI[m[1]]:02d}"
            out.setdefault(k, {})["pun"] = round(num(m[3]) / 1.1, 6)
            continue
        m = RE_IND_GAS.search(r)
        if m and m[1] in MESI_NOMI:
            k = f"{m[2]}-{MESI_NOMI[m[1]]:02d}"
            out.setdefault(k, {})["gas_indice"] = num(m[3])
    return out


def _prezzo_unitario(x):
    return x["nums"][0] if x["nums"] else 0.0


def _param_luce(righe):
    """Componenti a kWh dell'ultimo mese (IVA esclusa)."""
    def somma(cond):
        return sum(_prezzo_unitario(x) for x in righe
                   if x["unita"] == "€/kWh" and cond(x))
    p = {
        "input_number.elettricita_contributo_kwh":
            somma(lambda x: x["voce"] == "Contributo al consumo"),
        "input_number.elettricita_dispacciamento_kwh":
            somma(lambda x: x["voce"] == "Dispacciamento"),
        "input_number.elettricita_rete_kwh":
            somma(lambda x: x["sezione"].startswith("Spesa per la tariffa per l'uso della rete")
                  and x["voce"] == "Quota energia"),
        "input_number.elettricita_oneri_kwh":
            somma(lambda x: (x["sezione"].startswith("Componente")
                             or x["sezione"].startswith("Spesa per gli oneri"))
                  and x["voce"] == "Quota energia"),
        "input_number.elettricita_accisa_kwh":
            somma(lambda x: x["voce"] == "Imposta erariale" and x["nums"][1] > 0),
    }
    if p["input_number.elettricita_contributo_kwh"] <= 0 or p["input_number.elettricita_rete_kwh"] <= 0:
        raise Errore("luce: componenti a kWh non trovate")
    return {k: round(v, 6) for k, v in p.items()}


def _prezzo_gas(righe, iva_pct):
    """Prezzo marginale €/Smc dell'ultimo mese: per ogni voce a €/Smc lo
    scaglione piu' alto con consumo; IVA inclusa."""
    voci = {}
    for x in righe:
        if x["unita"] != "€/Smc" or len(x["nums"]) < 2 or x["nums"][1] <= 0:
            continue
        chiave = (x["sezione"], x["voce"])
        scaglione = x["nums"][2] if len(x["nums"]) > 2 else 0
        if chiave not in voci or scaglione > voci[chiave][0]:
            voci[chiave] = (scaglione, x["nums"][0])
    if not voci:
        return None
    return round(sum(v[1] for v in voci.values()) * (1 + iva_pct), 4)


# ----------------------------------------------------------------------------
# IRETI
# ----------------------------------------------------------------------------
RE_VAR = re.compile(r"^(.*?) (\d\d/\d\d/\d{4}) al (\d\d/\d\d/\d{4}) (.*?) ?"
                    r"(-?\d+(?:\.\d{3})*,\d\d) 10%$")
RE_FISSA = re.compile(r"^(.*Quota Fissa) (\d\d/\d\d/\d{4}) - (\d\d/\d\d/\d{4}) "
                      r"€/unità/anno (\d+,\d+) (\d+,\d+) (-?\d+,\d\d) 10%$")


def _ireti(righe, tutto):
    if PDF_ACQUA not in tutto:
        raise Errore("IRETI non di Casa Ginepro (punto di fornitura diverso): non inserita")
    m = re.search(r"([A-Z]+) - ([A-Z]+) (\d{4})", tutto)
    if not m or m[1] not in MESI or m[2] not in MESI:
        raise Errore("periodo di riferimento non trovato")
    anno = int(m[3])
    m1, m2 = MESI.index(m[1]) + 1, MESI.index(m[2]) + 1
    a1 = anno - 1 if m1 > m2 else anno
    primo = dt.date(a1, m1, 1)
    ultimo = dt.date(anno, m2, calendar.monthrange(anno, m2)[1])
    mesi = mesi_fra(primo, ultimo)
    fattura = re.search(r"Fattura n\. (\d+)", tutto)
    out = {"fornitore": "IRETI", "fattura": fattura[1] if fattura else "?",
           "periodo": f"{primo:%m/%Y}-{ultimo:%m/%Y}",
           "mesi": {k: {} for k in mesi}, "correzioni": {},
           "parametri": {}, "note": []}

    # Ripartizione giorno per giorno. reale = consumi e quote di questa
    # bolletta (acconti nuovi compresi); acconto = restituzione degli
    # acconti fatturati dalla bolletta precedente (negativa).
    reale_eur, reale_mc = {}, {}
    acc_eur, acc_mc = {}, {}
    coperti = set()              # giorni con consumo reale (non acconto nuovo)
    intervalli = {}              # (a, b) -> {"euro", "mc", "acconto"}
    fisse_prezzi = {}
    escluso = 0.0
    totale_righe = 0.0
    sezione = ""
    in_riconguaglio = False

    def spalma(d, a, b, valore):
        n = (b - a).days + 1
        for g in giorni(a, b):
            d[ym(g)] = d.get(ym(g), 0.0) + valore / n

    for r in righe:
        if r.startswith("DETTAGLIO "):
            sezione, in_riconguaglio = r, False
        if r.startswith("Riconguaglio anno solare"):
            # Conguaglio di un anno precedente: escluso. Le sue righe non
            # hanno date; vale l'importo dell'intestazione.
            mm = re.search(r"Importo € (-?[\d.]*\d,\d\d)", r)
            if not mm:
                raise Errore("importo del riconguaglio non trovato")
            escluso += num(mm[1])
            in_riconguaglio = True
            continue
        if r.startswith("TOTALE FORNITURA"):
            in_riconguaglio = False
        if in_riconguaglio:
            continue
        f = RE_FISSA.match(r)
        if f:
            a, b, euro = data(f[2]), data(f[3]), num(f[6])
            totale_righe += euro
            fisse_prezzi[f[1]] = num(f[4])
            spalma(reale_eur, a, b, euro)
            continue
        v = RE_VAR.match(r)
        if not v or "€/mc" not in r and not v[1].startswith("Restituzione"):
            continue
        desc, a, b, euro = v[1], data(v[2]), data(v[3]), num(v[5])
        totale_righe += euro
        if desc.lower().startswith("conguaglio"):
            escluso += euro
            continue
        nums = re.findall(r"-?\d+(?:,\d+)?", v[4].replace("€/mc", ""))
        if desc.startswith("Restituzione"):
            spalma(acc_eur, a, b, euro)
            # m3 restituiti contati una volta sola: riga dell'acquedotto
            # (la fognatura ripete la stessa quantita').
            if nums and sezione.startswith("DETTAGLIO Quota Acquedotto"):
                spalma(acc_mc, a, b, num(nums[-1]))
            continue
        if not any(s in desc for s in ("Acquedotto", "Fognatura", "Oneri")):
            raise Errore(f"riga non riconosciuta: {desc}")
        iv = intervalli.setdefault((a, b), {"euro": 0.0, "mc": 0.0, "acconto": False})
        iv["euro"] += euro
        iv["acconto"] = iv["acconto"] or desc.startswith("Acconto")
        if "Acquedotto Quota Variabile" in desc and len(nums) >= 2:
            iv["mc"] += num(nums[1])
    for (a, b), iv in intervalli.items():
        spalma(reale_eur, a, b, iv["euro"])
        spalma(reale_mc, a, b, iv["mc"])
        if not iv["acconto"]:
            coperti.update(giorni(a, b))

    fornitura = _totale(tutto, "TOTALE FORNITURA Importo €", "DETTAGLIO")
    iva = num(re.search(r"IVA AGEVOLATA \(10%\) su imponibile euro [\d.,]+ Importo € "
                        r"(-?[\d.]*\d,\d\d)", tutto)[1])
    if abs(totale_righe + escluso - fornitura) > 0.02:
        raise Errore(f"somma righe {totale_righe + escluso:.2f} diversa da totale "
                     f"fornitura {fornitura:.2f}")
    k_iva = 1 + iva / fornitura
    netto = fornitura - escluso
    fuori = [k for k in set(reale_eur) | set(acc_eur) if k > mesi[-1]]
    if fuori:
        raise Errore(f"importi dopo il periodo della bolletta ({', '.join(sorted(fuori))})")

    # Mesi precedenti al periodo: correzione del valore gia' in archivio
    # (si toglie l'acconto restituito, si aggiunge il consumo reale).
    # 'completo' = tutti i giorni del mese hanno consumo reale in questa
    # bolletta: se il mese non e' in archivio vale il solo reale.
    prec = sorted(k for k in set(reale_eur) | set(acc_eur) if k < mesi[0])
    somma_prec = 0.0
    for k in prec:
        y, mm_ = map(int, k.split("-"))
        n = calendar.monthrange(y, mm_)[1]
        completo = all(dt.date(y, mm_, g) in coperti for g in range(1, n + 1))
        c = {"reale_mc": round(reale_mc.get(k, 0.0), 1),
             "reale_euro": round(reale_eur.get(k, 0.0) * k_iva, 2),
             "acconto_mc": round(acc_mc.get(k, 0.0), 1),
             "acconto_euro": round(acc_eur.get(k, 0.0) * k_iva, 2),
             "completo": completo}
        somma_prec += c["reale_euro"] + c["acconto_euro"]
        out["correzioni"][k] = c

    tot = chiudi({k: (reale_eur.get(k, 0.0) + acc_eur.get(k, 0.0)) * k_iva for k in mesi},
                 netto * k_iva - somma_prec)
    for k in mesi:
        out["mesi"][k] = {"acqua_mc": round(reale_mc.get(k, 0.0) + acc_mc.get(k, 0.0), 1),
                          "acqua_euro": tot[k]}
    if escluso:
        out["note"].append(f"conguagli anni precedenti esclusi {escluso * k_iva:.2f} €")
    if fisse_prezzi:
        out["parametri"]["input_number.acqua_quota_fissa_mese"] = \
            round(sum(fisse_prezzi.values()) / 12 * k_iva, 2)
    # Prezzo a consumo medio effettivo dei consumi di questa bolletta
    # (acquedotto a scaglioni + fognatura + oneri, IVA inclusa).
    mc_tot = sum(iv["mc"] for iv in intervalli.values())
    if mc_tot > 0:
        out["parametri"]["input_number.acqua_tariffa"] = \
            round(sum(iv["euro"] for iv in intervalli.values()) / mc_tot * k_iva, 2)
    out["note"].append(f"acqua {netto * k_iva:.2f} €")
    return out


if __name__ == "__main__":
    import json
    for p in sys.argv[1:]:
        try:
            print(json.dumps(leggi(p), ensure_ascii=False, indent=1, default=str))
        except Errore as e:
            print(p, "ERRORE:", e)
