#!/usr/bin/env python3
# controllo_file_1.py
#
# Sorveglianza dei file di configurazione di Casa Ginepro.
# Nato dall'incidente del 10/10/2026: automations_fotovoltaico.yaml e' sparito
# da /config/automations e per 6 giorni nessuno se n'e' accorto (tabella
# mensile ferma, nessun avviso condensatore o Tigo).
#
# Uso:
#   python3 /config/controllo_file.py            -> controllo, JSON su stdout
#   python3 /config/controllo_file.py aggiorna   -> riscrive l'elenco atteso
#                                                   dallo stato attuale
#
# L'elenco atteso e' /config/file_attesi.json. Va aggiornato DOPO ogni
# modifica voluta (file aggiunto, tolto o automazioni rimosse), altrimenti il
# controllo la segnala.
#
# Cosa controlla:
#   - mancanti: file presenti nell'elenco e non piu' su disco;
#   - ridotti:  file automazioni con meno "- id:" di quelli attesi (file
#               sovrascritto o troncato), scripts.yaml con meno script;
#   - nuovi:    file presenti su disco e non nell'elenco (solo informativo).
#
# Output del controllo (letto dall'automazione sistema_controllo_file_config):
#   {"stato": "ok" | "problemi", "mancanti": [...], "ridotti": [...],
#    "nuovi": [...], "controllati": N, "elenco_del": "..."}
# Se l'elenco atteso non esiste o non si legge: exit 1 con messaggio su stderr.

import glob, json, os, re, sys, datetime

BASE = os.environ.get("CONFIG_DIR", "/config")
ELENCO = os.path.join(BASE, "file_attesi.json")

MODELLI = [
    "configuration.yaml", "scripts.yaml", "scenes.yaml", "secrets.yaml",
    "automations/*.yaml", "packages/*.yaml",
    "*.py", "*.json",
    "esphome/*.yaml", "zigbee2mqtt/configuration.yaml",
    "custom_components/*/manifest.json",
    ".storage/lovelace*",
]
ESCLUSI = {"file_attesi.json"}

RE_ID = re.compile(r"^- id:", re.M)
RE_SCRIPT = re.compile(r"^[a-z0-9_]+:[ \t]*\r?$", re.M)


def conta(rel, path):
    # Numero di elementi nel file, solo dove ha senso; None altrove.
    try:
        if rel.startswith("automations/"):
            with open(path, encoding="utf-8", errors="replace") as f:
                return len(RE_ID.findall(f.read()))
        if rel == "scripts.yaml":
            with open(path, encoding="utf-8", errors="replace") as f:
                return len(RE_SCRIPT.findall(f.read()))
    except OSError:
        return None
    return None


def stato_attuale():
    out = {}
    for m in MODELLI:
        for p in glob.glob(os.path.join(BASE, m)):
            rel = os.path.relpath(p, BASE)
            if rel in ESCLUSI or not os.path.isfile(p):
                continue
            out[rel] = conta(rel, p)
    return out


def aggiorna():
    att = stato_attuale()
    dati = {"creato": datetime.datetime.now().strftime("%d/%m/%Y %H:%M"),
            "file": dict(sorted(att.items()))}
    tmp = ELENCO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dati, f, indent=1, ensure_ascii=False)
    os.replace(tmp, ELENCO)
    print(f"elenco aggiornato: {len(att)} file")


def controlla():
    try:
        with open(ELENCO, encoding="utf-8") as f:
            dati = json.load(f)
        attesi = dati["file"]
    except (OSError, ValueError, KeyError) as e:
        print(f"elenco atteso non leggibile: {e}", file=sys.stderr)
        sys.exit(1)
    att = stato_attuale()
    mancanti = sorted(k for k in attesi if k not in att)
    ridotti = sorted(
        f"{k} {attesi[k]}->{att[k]}" for k in attesi
        if k in att and attesi[k] is not None and att[k] is not None
        and att[k] < attesi[k])
    nuovi = sorted(k for k in att if k not in attesi)
    print(json.dumps({
        "stato": "problemi" if (mancanti or ridotti) else "ok",
        "mancanti": mancanti, "ridotti": ridotti, "nuovi": nuovi,
        "controllati": len(attesi), "elenco_del": dati.get("creato", "?"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "aggiorna":
        aggiorna()
    else:
        controlla()
