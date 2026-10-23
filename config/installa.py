#!/usr/bin/env python3
# installa_7.py
#
# v7 (23/10/2026): una bolletta gia' importata non si reimporta. Prima la
#   stessa fattura copiata due volte (o gia' in SORGENTI/BOLLETTE) veniva
#   salvata di nuovo e le sue correzioni dei mesi precedenti si sommavano
#   una seconda volta all'archivio (IRETI 11502600027477, 09/2025 +4,9 m3
#   contato due volte il 23/10). Ora resta in da_installare fra gli
#   scartati con "gia' importata".
# v6 (23/10/2026): bollette IRETI con mesi prima del periodo fatturato
#   (restituzione di acconti, consumi reali che partono prima): quei mesi
#   sono correzioni del valore gia' in archivio (meno acconto, piu' reale),
#   lette dal DB al momento del salvataggio e in ordine di periodo. Se il
#   mese non e' in archivio vale il solo consumo reale, ma solo se questa
#   bolletta lo copre per intero; altrimenti non viene scritto e lo dice la
#   notifica.
# v5 (23/10/2026): bollette PDF. Un PDF in da_installare viene letto con
#   bollette.py (Plenitude luce+gas, IRETI acqua): valori mensili e
#   parametri. In conferma ogni mese va in salva_bolletta.py (stesso
#   archivio dell'inserimento a mano, i valori a 0 non vengono toccati) e
#   i parametri (quote fisse, componenti a kWh della luce, prezzo gas)
#   tornano nel JSON di conferma: li applica script.installa_e_riavvia.
#   Il PDF viene archiviato in SORGENTI/BOLLETTE (tutte le versioni).
#   Una bolletta che non si legge o non torna con i totali resta in
#   da_installare ed e' segnalata fra gli scartati. Nessun riavvio.
# v4 (21/10/2026): con piu' versioni dello stesso file in da_installare si
#   installa la piu' alta (prima vinceva la prima in ordine alfabetico,
#   cioe' la piu' vecchia).
# v3 (21/10/2026): un package nuovo (prima non esisteva) si confronta con un
#   file vuoto invece di dare "confronto non riuscito": se contiene solo
#   domini ricaricabili basta il Riavvio rapido.
# v2 (16/10/2026): il tipo di riavvio si decide dai file installati.
#   RAPIDO   = Riavvio rapido di HA (homeassistant.reload_all): automazioni,
#              scripts.yaml, package che cambiano solo domini ricaricabili.
#   COMPLETO = Riavvia Home Assistant (homeassistant.restart): plance (HA
#              le rilegge solo all'avvio), configuration.yaml, package che
#              cambiano domini non ricaricabili (shell_command,
#              utility_meter, sensori a piattaforma...).
#   Nessuno  = solo file Python, file_attesi, ESPHome.
#   prepara restituisce "riavvio" e "motivi".
#
# Installazione dei file consegnati da Claude (pulsante "Installa e riavvia",
# plancia Casa Ginepro, script.installa_e_riavvia in config_sistema).
#
# I file scaricati si copiano COSI' COME SONO, con il numero di versione, in
# /config/da_installare/. Il nome dice dove vanno:
#   automations_X_N.yaml  -> /config/automations/automations_X.yaml
#   config_X_N.yaml       -> /config/packages/config_X.yaml
#   configuration_N.yaml  -> /config/configuration.yaml
#   scripts_N.yaml        -> /config/scripts.yaml
#   file_attesi_N.yaml    -> /config/file_attesi.yaml
#   X_N.py.txt / X_N.py   -> /config/X.py
#   plancia_X_N.yaml      -> plancia X (.storage/lovelace.<id>)
#   nome-N.yaml (ESPHome) -> solo archiviato: si carica dall'ESPHome
#   *.pdf (bolletta)      -> letta da bollette.py, mesi in salva_bolletta.py
# Ogni file installato viene archiviato con il numero in /config/SORGENTI
# (sottocartella per tipo); per ogni file restano le ultime 5 versioni.
#
# Fasi (chiamate in sequenza dallo script HA, ognuna dura pochi secondi):
#   prepara        controlla i file, salva gli originali, mette al loro posto
#                  i nuovi. JSON su stdout.
#   verifica_avvia avvia in background il controllo configurazione di HA
#                  (hass --script check_config) e torna subito.
#   verifica_stato JSON {"stato": "in_corso" | "ok" | "errore", ...}
#   conferma       archivia in SORGENTI, pulisce le versioni oltre la 5a,
#                  svuota da_installare. JSON con il riepilogo.
#   annulla        rimette gli originali, i file nuovi restano in
#                  da_installare. JSON con il riepilogo.
#
# Controlli prima di installare un file:
#   - il nome ha la forma base_N.ext (o base-N.yaml per ESPHome);
#   - le prime righe contengono il commento con il nome del file;
#   - la versione non e' inferiore a quella installata ne' alla piu' alta
#     gia' archiviata in SORGENTI;
#   - YAML leggibile (plance: con la chiave views), Python compilabile.
# Un file che non passa resta in da_installare e viene segnalato.

import glob, json, os, re, shutil, sqlite3, subprocess, sys, datetime
import yaml

CONFIG = os.environ.get("CONFIG_DIR", "/config")
IN = os.path.join(CONFIG, "da_installare")
STATO = os.path.join(IN, ".stato.json")
RIPRISTINO = os.path.join(IN, ".ripristino")
VERIFICA = os.path.join(IN, ".verifica.json")
ARCH = os.path.join(CONFIG, "SORGENTI")
TENERE = 5
# Ultimo mese importato da bolletta per fornitore: i parametri si aggiornano
# solo da una bolletta piu' recente (v5).
ULTIMO = os.path.join(CONFIG, ".bollette_ultimo.json")

# Plance: nome nel file (dopo "plancia_") -> id della plancia in HA.
# I nomi non elencati valgono se esiste .storage/lovelace.<nome> oppure
# .storage/lovelace.dashboard_<nome>.
PLANCE = {
    "ginepro": "casa_ginepro",
    "casa_ginepro": "casa_ginepro",
    "otgw": "otgw_caldaia",
    "otgw_caldaia": "otgw_caldaia",
    "wifi": "wi_fi",
    "wi_fi": "wi_fi",
    "zone": "zone_casa",
    "zone_casa": "zone_casa",
    "inverter": "dettagli_inverter",
    "dettagli_inverter": "dettagli_inverter",
}

# Sottocartelle di SORGENTI in cui si tengono solo le ultime TENERE versioni.
DA_PULIRE = ["AUTOMATIONS", "CONFIGURATIONS", "CONFIGURATIONS/PACKAGES",
             "PY", "SCRIPTS", "PLANCE", "ESPHOME"]

# Domini che il Riavvio rapido ricarica. Tutto il resto, se cambia in un
# package, richiede il riavvio COMPLETO.
RICARICABILI = {
    "automation", "script", "scene", "template", "group", "command_line",
    "input_boolean", "input_number", "input_select", "input_text",
    "input_datetime", "input_button", "counter", "timer", "schedule",
    "zone", "person",
}

RE_NOME = re.compile(r"^(?P<base>.+?)_(?P<v>\d+)(?P<ext>\.py\.txt|\.py|\.yaml)$")
RE_ESPHOME = re.compile(r"^(?P<base>.+?)-(?P<v>\d+)(?P<ext>\.yaml)$")
RE_ARCH = re.compile(r"^(?P<base>.+?)[_-](?P<v>\d+)\.(?:ya?ml|py)$")


class _Loader(yaml.SafeLoader):
    pass


# configuration.yaml e i package usano !include, !secret ecc.: qui serve solo
# sapere se il YAML e' leggibile, i tag vengono ignorati.
_Loader.add_multi_constructor("!", lambda l, s, n: None)


def esci(dati):
    print(json.dumps(dati, ensure_ascii=False))
    sys.exit(0)


def classifica(nome):
    """Ritorna (tipo, base, versione, destinazione, sottocartella, nome_arch)
    oppure (None, motivo)."""
    if nome.lower().endswith(".pdf"):
        return ("bolletta", nome, 0, None, "BOLLETTE", nome)
    m = RE_NOME.match(nome)
    if m:
        base, v, ext = m["base"], int(m["v"]), m["ext"]
        if ext in (".py.txt", ".py"):
            return ("file", base, v, os.path.join(CONFIG, base + ".py"),
                    "PY", f"{base}_{v}.py")
        arch = f"{base}_{v}.yaml"
        if base.startswith("automations_"):
            return ("file", base, v, os.path.join(CONFIG, "automations", base + ".yaml"),
                    "AUTOMATIONS", arch)
        if base.startswith("config_"):
            return ("file", base, v, os.path.join(CONFIG, "packages", base + ".yaml"),
                    "CONFIGURATIONS/PACKAGES", arch)
        if base in ("configuration", "file_attesi"):
            return ("file", base, v, os.path.join(CONFIG, base + ".yaml"),
                    "CONFIGURATIONS", arch)
        if base == "scripts":
            return ("file", base, v, os.path.join(CONFIG, "scripts.yaml"),
                    "SCRIPTS", arch)
        if base.startswith("plancia_"):
            corto = base[len("plancia_"):]
            pid = PLANCE.get(corto)
            if not pid:
                for c in (corto, "dashboard_" + corto):
                    if os.path.isfile(os.path.join(CONFIG, ".storage", "lovelace." + c)):
                        pid = c
                        break
            if not pid:
                return (None, "plancia sconosciuta")
            return ("plancia", base, v,
                    os.path.join(CONFIG, ".storage", "lovelace." + pid),
                    "PLANCE", arch)
        return (None, "tipo di file non riconosciuto")
    m = RE_ESPHOME.match(nome)
    if m:
        return ("esphome", m["base"], int(m["v"]), None, "ESPHOME", nome)
    return (None, "nome senza numero di versione")


def versione_installata(base, dest):
    try:
        with open(dest, encoding="utf-8", errors="replace") as f:
            testa = "".join(f.readline() for _ in range(3))
    except OSError:
        return None
    m = re.search(r"#\s*" + re.escape(base) + r"_(\d+)\.", testa)
    return int(m.group(1)) if m else None


def versione_archiviata(base, sotto):
    vs = []
    for p in os.listdir(os.path.join(ARCH, sotto)) if os.path.isdir(os.path.join(ARCH, sotto)) else []:
        m = RE_ARCH.match(p)
        if m and m["base"] == base:
            vs.append(int(m["v"]))
    return max(vs) if vs else None


def controlla_contenuto(tipo, nome, base, v, percorso):
    with open(percorso, "rb") as f:
        dati = f.read()
    testo = dati.decode("utf-8", errors="replace")
    testa = "\n".join(testo.splitlines()[:3])
    if not re.search(re.escape(base) + r"[_-]" + str(v) + r"\.", testa):
        return None, "manca il commento con il nome del file nelle prime righe"
    if nome.endswith((".py", ".py.txt")):
        try:
            compile(testo, nome, "exec")
        except SyntaxError as e:
            return None, f"errore Python riga {e.lineno}"
        return dati, None
    try:
        y = yaml.load(testo, Loader=_Loader)
    except yaml.YAMLError as e:
        return None, f"YAML non valido: {str(e).splitlines()[0]}"
    if tipo == "plancia":
        if isinstance(y, dict) and "config" in y and "views" not in y:
            y = y["config"]
        if not isinstance(y, dict) or "views" not in y:
            return None, "plancia senza views"
        return y, None
    return dati, None


def leggi_yaml(percorso):
    try:
        with open(percorso, encoding="utf-8", errors="replace") as f:
            y = yaml.load(f, Loader=_Loader)
        return y if isinstance(y, dict) else {}
    except (OSError, yaml.YAMLError):
        return None


def riavvio_per(tipo, base, dest, src):
    """Ritorna ("nessuno"|"rapido"|"completo", motivo)."""
    if tipo == "plancia":
        return "completo", f"{base}: HA rilegge le plance solo all'avvio"
    if tipo != "file" or src.endswith((".py", ".py.txt")) or base == "file_attesi":
        return "nessuno", ""
    if base == "configuration":
        return "completo", "configuration.yaml"
    if base.startswith("automations_") or base == "scripts":
        return "rapido", ""
    # Package nuovo: prima non c'era, si confronta con un file vuoto.
    vecchio = leggi_yaml(dest) if os.path.exists(dest) else {}
    nuovo = leggi_yaml(src)
    if vecchio is None or nuovo is None:
        return "completo", f"{base}: confronto non riuscito"
    cambiati = sorted(k for k in set(vecchio) | set(nuovo)
                      if vecchio.get(k) != nuovo.get(k))
    fissi = [k for k in cambiati if k not in RICARICABILI]
    if fissi:
        return "completo", f"{base}: {', '.join(fissi)}"
    return ("rapido" if cambiati else "nessuno"), ""


def descr_bolletta(d):
    """Riga di riepilogo: fornitore, fattura e valori per mese."""
    parti = []
    for k in sorted(d["mesi"]):
        v = d["mesi"][k]
        y, m = k.split("-")
        voci = []
        if "elec_kwh" in v:
            voci.append(f"luce {v['elec_kwh']:g} kWh {v['elec_euro']:.2f} €")
        if "gas_mc" in v:
            voci.append(f"gas {v['gas_mc']:g} m³ {v['gas_euro']:.2f} €")
        if "acqua_mc" in v:
            voci.append(f"acqua {v['acqua_mc']:g} m³ {v['acqua_euro']:.2f} €")
        parti.append(f"{m}/{y} " + ", ".join(voci))
    for k, c in sorted(d.get("correzioni", {}).items()):
        y, m = k.split("-")
        parti.append(f"{m}/{y} correzione acqua "
                     f"{c['reale_mc'] + c['acconto_mc']:+.1f} m³ "
                     f"{c['reale_euro'] + c['acconto_euro']:+.2f} €")
    return f"bolletta {d['fornitore']} {d['fattura']}: " + "; ".join(parti)


def valore_archivio(statistic_id, ym):
    """Valore mensile gia' salvato da salva_bolletta.py (None se manca)."""
    y, m = map(int, ym.split("-"))
    t = datetime.datetime(y, m, 1, tzinfo=datetime.timezone.utc).timestamp()
    try:
        con = sqlite3.connect(f"file:{os.path.join(CONFIG, 'home-assistant_v2.db')}?mode=ro",
                              uri=True, timeout=30)
        r = con.execute("""SELECT MAX(s.max) FROM statistics s
            JOIN statistics_meta m ON s.metadata_id = m.id
            WHERE m.statistic_id = ? AND s.start_ts = ?""", (statistic_id, t)).fetchone()
        con.close()
    except sqlite3.Error:
        return None
    return r[0] if r and r[0] is not None and r[0] > 0 else None


def correggi_mesi(d):
    """Mesi prima del periodo della bolletta (solo acqua): archivio meno
    acconto restituito piu' consumo reale. Ritorna (mesi da salvare, note)."""
    out, note = {}, []
    for k, c in sorted(d.get("correzioni", {}).items()):
        mc0 = valore_archivio("input_number.bolletta_acqua_mc", k)
        eu0 = valore_archivio("input_number.bolletta_acqua_euro", k)
        if mc0 is not None and eu0 is not None:
            mc = mc0 + c["acconto_mc"] + c["reale_mc"]
            eu = eu0 + c["acconto_euro"] + c["reale_euro"]
        elif c["completo"]:
            mc, eu = c["reale_mc"], c["reale_euro"]
        else:
            note.append(f"{k} non in archivio e coperto solo in parte: non scritto")
            continue
        if mc <= 0 or eu <= 0:
            note.append(f"{k} correzione non scritta (valore risultante {mc:.1f} m³ {eu:.2f} €)")
            continue
        out[k] = {"acqua_mc": round(mc, 1), "acqua_euro": round(eu, 2)}
    return out, note


def salva_mesi(d):
    """Un'esecuzione di salva_bolletta.py per mese, prima le correzioni dei
    mesi precedenti. Ritorna (ok, errori, note)."""
    errori = []
    corr, note = correggi_mesi(d)
    mesi = dict(corr)
    mesi.update(d["mesi"])
    for k in sorted(mesi):
        v = mesi[k]
        y, m = k.split("-")
        args = [f"{m}/{y}"] + [str(v.get(c, 0)) for c in
                ("elec_kwh", "elec_euro", "gas_mc", "gas_euro",
                 "acqua_mc", "acqua_euro")] + ["0"]
        r = subprocess.run([sys.executable, os.path.join(CONFIG, "salva_bolletta.py")]
                           + args, capture_output=True, text=True, timeout=50)
        try:
            esito = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            esito = {"ok": False, "errore": (r.stderr or "nessun output")[-200:]}
        if not esito.get("ok"):
            errori.append(f"{m}/{y}: {esito.get('errore')}")
    return not errori, errori, note


def prepara():
    os.makedirs(IN, exist_ok=True)
    avvisi = []
    if os.path.exists(STATO):
        # Un'installazione precedente si e' interrotta: prima si rimette
        # tutto com'era.
        r = annulla(silenzioso=True)
        avvisi.append("installazione precedente interrotta: originali ripristinati "
                      f"({', '.join(r['ripristinati']) or 'nessuno'})")
    nomi = [n for n in os.listdir(IN)
            if not n.startswith(".") and os.path.isfile(os.path.join(IN, n))]

    # Stesso file in piu' versioni: la piu' alta va esaminata per prima,
    # le altre vengono scartate come doppioni.
    def _ordine(n):
        m = RE_NOME.match(n) or RE_ESPHOME.match(n)
        return (m.group("base"), -int(m.group("v"))) if m else (n, 0)
    nomi.sort(key=_ordine)
    if not nomi:
        esci({"totale": 0, "installati": [], "scartati": [], "avvisi": avvisi})

    shutil.rmtree(RIPRISTINO, ignore_errors=True)
    os.makedirs(RIPRISTINO)
    fatti, scartati, esphome, viste = [], [], [], set()
    for nome in nomi:
        src = os.path.join(IN, nome)
        c = classifica(nome)
        if c[0] is None:
            scartati.append(f"{nome}: {c[1]}")
            continue
        tipo, base, v, dest, sotto, nome_arch = c
        if tipo == "bolletta":
            try:
                sys.path.insert(0, CONFIG)
                import bollette
                dati = bollette.leggi(src)
            except Exception as e:
                scartati.append(f"{nome}: {e}")
                continue
            arch_b = f"{dati['fornitore']}_{dati['fattura']}.pdf"
            if (arch_b in viste
                    or os.path.exists(os.path.join(ARCH, sotto, arch_b))):
                scartati.append(f"{nome}: bolletta {dati['fornitore']} "
                                f"{dati['fattura']} gia' importata, va tolta")
                continue
            viste.add(arch_b)
            fatti.append({"nome": nome, "tipo": "bolletta", "base": base, "v": 0,
                          "prima": None, "dest": None, "copia": None,
                          "sotto": sotto,
                          "arch": arch_b,
                          "riavvio": "nessuno",
                          "motivo": "", "dati": dati})
            continue
        if base in viste:
            scartati.append(f"{nome}: superato dalla versione piu' alta dello stesso file, va tolto")
            continue
        viste.add(base)
        va = versione_archiviata(base, sotto)
        if va is not None and v < va:
            scartati.append(f"{nome}: in SORGENTI c'e' gia' la versione {va}")
            continue
        if tipo == "esphome":
            esphome.append({"nome": nome, "sotto": sotto, "arch": nome_arch, "base": base})
            continue
        vi = versione_installata(base, dest) if tipo == "file" else None
        if vi is not None and v < vi:
            scartati.append(f"{nome}: installata la versione {vi}")
            continue
        contenuto, errore = controlla_contenuto(tipo, nome, base, v, src)
        if errore:
            scartati.append(f"{nome}: {errore}")
            continue
        riavvio, motivo = riavvio_per(tipo, base, dest, src)
        copia = None
        if os.path.exists(dest):
            copia = os.path.join(RIPRISTINO, f"{len(fatti)}_{os.path.basename(dest)}")
            shutil.copy2(dest, copia)
        if tipo == "plancia":
            if not copia:
                scartati.append(f"{nome}: la plancia non esiste in HA")
                continue
            with open(dest, encoding="utf-8") as f:
                st = json.load(f)
            st["data"] = {"config": contenuto}
            tmp = dest + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
            os.replace(tmp, dest)
        else:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".tmp"
            with open(tmp, "wb") as f:
                f.write(contenuto)
            os.replace(tmp, dest)
        fatti.append({"nome": nome, "tipo": tipo, "base": base, "v": v, "prima": vi,
                      "dest": dest, "copia": copia, "sotto": sotto, "arch": nome_arch,
                      "riavvio": riavvio, "motivo": motivo})

    with open(STATO, "w", encoding="utf-8") as f:
        json.dump({"fatti": fatti, "esphome": esphome}, f, ensure_ascii=False)

    def descr(x):
        if x["tipo"] == "bolletta":
            return descr_bolletta(x["dati"])
        corto = os.path.relpath(x["dest"], CONFIG)
        if x["tipo"] == "plancia":
            return f"plancia {x['base'][8:]} {x['v']}"
        return f"{corto} {x['prima'] or '-'}->{x['v']}"
    esci({"totale": len(nomi),
          "installati": [descr(x) for x in fatti],
          "file_config": sum(1 for x in fatti if x["tipo"] == "file"),
          "esphome": [x["nome"] for x in esphome],
          "riavvio": ("completo" if any(x["riavvio"] == "completo" for x in fatti)
                      else "rapido" if any(x["riavvio"] == "rapido" for x in fatti)
                      else "nessuno"),
          "motivi": [x["motivo"] for x in fatti if x["riavvio"] == "completo" and x["motivo"]],
          "scartati": scartati, "avvisi": avvisi})


def verifica_avvia():
    try:
        os.remove(VERIFICA)
    except OSError:
        pass
    # Il controllo gira in background: su Raspberry puo' superare il limite
    # di 60 s degli shell_command. Il risultato finisce in .verifica.json.
    cmd = os.environ.get("CHECK_CMD",
                         f"python3 -m homeassistant --script check_config --config {CONFIG}")
    figlio = (
        "import json,subprocess,sys\n"
        f"r=subprocess.run({cmd!r},shell=True,capture_output=True,text=True)\n"
        "out=(r.stdout+r.stderr).strip()\n"
        "ok=r.returncode==0\n"
        f"open({VERIFICA!r},'w').write(json.dumps({{'stato':'ok' if ok else 'errore',"
        "'dettaglio':'' if ok else out[-1500:]}))\n"
    )
    subprocess.Popen([sys.executable, "-c", figlio], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    esci({"stato": "avviato"})


def verifica_stato():
    try:
        with open(VERIFICA, encoding="utf-8") as f:
            esci(json.load(f))
    except (OSError, ValueError):
        esci({"stato": "in_corso"})


def pulisci(sotto):
    cartella = os.path.join(ARCH, sotto)
    if not os.path.isdir(cartella):
        return 0
    gruppi = {}
    for p in os.listdir(cartella):
        m = RE_ARCH.match(p)
        if m and os.path.isfile(os.path.join(cartella, p)):
            gruppi.setdefault(m["base"], []).append((int(m["v"]), p))
    tolti = 0
    for base, lista in gruppi.items():
        lista.sort(reverse=True)
        for _, p in lista[TENERE:]:
            os.remove(os.path.join(cartella, p))
            tolti += 1
    return tolti


def conferma():
    try:
        with open(STATO, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        esci({"archiviati": [], "tolti": 0, "errore": "stato installazione assente"})
    archiviati, bollette, parametri, errori_b = [], [], {}, []
    # Bollette: prima si salvano i mesi; un PDF con errori resta in
    # da_installare. Parametri: solo da una bolletta piu' recente
    # dell'ultima importata per quel fornitore (anche fra esecuzioni).
    try:
        with open(ULTIMO, encoding="utf-8") as f:
            ultimo = json.load(f)
    except (OSError, ValueError):
        ultimo = {}
    for x in sorted((x for x in st["fatti"] if x["tipo"] == "bolletta"),
                    key=lambda x: max(x["dati"]["mesi"])):
        ok, err, note = salva_mesi(x["dati"])
        if ok:
            d = x["dati"]
            fine = max(d["mesi"])
            testo = descr_bolletta(d)
            if note:
                testo += " (" + "; ".join(note) + ")"
            if fine >= ultimo.get(d["fornitore"], ""):
                parametri.update(d["parametri"])
                ultimo[d["fornitore"]] = fine
            else:
                testo += " (parametri non aggiornati: bolletta meno recente)"
            bollette.append(testo)
        else:
            errori_b.append(f"{x['nome']}: {'; '.join(err)}")
            x["non_archiviare"] = True
    if bollette:
        with open(ULTIMO, "w", encoding="utf-8") as f:
            json.dump(ultimo, f)
    for x in st["fatti"] + st["esphome"]:
        if x.get("non_archiviare"):
            continue
        cartella = os.path.join(ARCH, x["sotto"])
        os.makedirs(cartella, exist_ok=True)
        src = os.path.join(IN, x["nome"])
        if os.path.exists(src):
            shutil.move(src, os.path.join(cartella, x["arch"]))
            archiviati.append(x["arch"])
    tolti = sum(pulisci(s) for s in DA_PULIRE)
    shutil.rmtree(RIPRISTINO, ignore_errors=True)
    for p in (STATO, VERIFICA):
        try:
            os.remove(p)
        except OSError:
            pass
    esci({"archiviati": archiviati, "tolti": tolti,
          "esphome": [x["nome"] for x in st["esphome"]],
          "bollette": bollette, "parametri": parametri,
          "errori_bollette": errori_b})


def annulla(silenzioso=False):
    try:
        with open(STATO, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {"fatti": []}
    ripristinati = []
    for x in reversed(st["fatti"]):
        if x["tipo"] == "bolletta":
            continue   # niente da rimettere: le bollette si salvano in conferma
        if x["copia"] and os.path.exists(x["copia"]):
            shutil.copy2(x["copia"], x["dest"])
        elif not x["copia"] and os.path.exists(x["dest"]):
            os.remove(x["dest"])   # file nuovo: prima non c'era
        ripristinati.append(os.path.relpath(x["dest"], CONFIG))
    shutil.rmtree(RIPRISTINO, ignore_errors=True)
    for p in (STATO, VERIFICA):
        try:
            os.remove(p)
        except OSError:
            pass
    r = {"ripristinati": ripristinati}
    if silenzioso:
        return r
    esci(r)


if __name__ == "__main__":
    fase = sys.argv[1] if len(sys.argv) > 1 else ""
    {"prepara": prepara, "verifica_avvia": verifica_avvia,
     "verifica_stato": verifica_stato, "conferma": conferma,
     "annulla": annulla}.get(fase, lambda: esci({"errore": f"fase sconosciuta: {fase}"}))()
