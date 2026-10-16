#!/usr/bin/env python3
# installa_1.py
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

import glob, json, os, re, shutil, subprocess, sys, datetime
import yaml

CONFIG = os.environ.get("CONFIG_DIR", "/config")
IN = os.path.join(CONFIG, "da_installare")
STATO = os.path.join(IN, ".stato.json")
RIPRISTINO = os.path.join(IN, ".ripristino")
VERIFICA = os.path.join(IN, ".verifica.json")
ARCH = os.path.join(CONFIG, "SORGENTI")
TENERE = 5

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


def prepara():
    os.makedirs(IN, exist_ok=True)
    avvisi = []
    if os.path.exists(STATO):
        # Un'installazione precedente si e' interrotta: prima si rimette
        # tutto com'era.
        r = annulla(silenzioso=True)
        avvisi.append("installazione precedente interrotta: originali ripristinati "
                      f"({', '.join(r['ripristinati']) or 'nessuno'})")
    nomi = sorted(n for n in os.listdir(IN)
                  if not n.startswith(".") and os.path.isfile(os.path.join(IN, n)))
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
        if base in viste:
            scartati.append(f"{nome}: piu' versioni dello stesso file, ne va copiata una")
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
                      "dest": dest, "copia": copia, "sotto": sotto, "arch": nome_arch})

    with open(STATO, "w", encoding="utf-8") as f:
        json.dump({"fatti": fatti, "esphome": esphome}, f, ensure_ascii=False)

    def descr(x):
        corto = os.path.relpath(x["dest"], CONFIG)
        if x["tipo"] == "plancia":
            return f"plancia {x['base'][8:]} {x['v']}"
        return f"{corto} {x['prima'] or '-'}->{x['v']}"
    esci({"totale": len(nomi),
          "installati": [descr(x) for x in fatti],
          "file_config": sum(1 for x in fatti if x["tipo"] == "file"),
          "esphome": [x["nome"] for x in esphome],
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
    archiviati = []
    for x in st["fatti"] + st["esphome"]:
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
          "esphome": [x["nome"] for x in st["esphome"]]})


def annulla(silenzioso=False):
    try:
        with open(STATO, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {"fatti": []}
    ripristinati = []
    for x in reversed(st["fatti"]):
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
