#!/usr/bin/env python3
# controllo_file_2.py
#
# Sorveglianza dei file di configurazione di Casa Ginepro.
# Nato dall'incidente del 10/10/2026: automations_fotovoltaico.yaml e' sparito
# da /config/automations e per 6 giorni nessuno se n'e' accorto.
#
# v2 (16/10/2026): l'elenco atteso e' /config/file_attesi.yaml, scritto e
#   versionato a mano (file_attesi_N.yaml) come gli altri file di
#   configurazione. Tolta la modalita' "aggiorna": nessun comando da lanciare.
#
# Formato di file_attesi.yaml:
#   file:
#     configuration.yaml:                     -> deve solo esistere
#     automations/automations_clima.yaml: 15  -> esistere e avere almeno
#                                                15 automazioni ("- id:")
#     scripts.yaml: 27                        -> almeno 27 script
#
# Output (letto dall'automazione sistema_controllo_file_config):
#   {"stato": "ok" | "problemi", "mancanti": [...], "ridotti": [...],
#    "nuovi": [...], "controllati": N, "elenco": "file_attesi_N.yaml"}
#   nuovi = file presenti su disco nelle cartelle sorvegliate ma assenti
#   dall'elenco: non e' un guasto, l'elenco va aggiornato.
# Se l'elenco non esiste o non si legge: exit 1 con messaggio su stderr.

import glob, json, os, re, sys
import yaml

BASE = os.environ.get("CONFIG_DIR", "/config")
ELENCO = os.path.join(BASE, "file_attesi.yaml")

# Cartelle in cui cercare i file "nuovi" (non in elenco).
MODELLI = [
    "configuration.yaml", "scripts.yaml", "scenes.yaml", "secrets.yaml",
    "automations/*.yaml", "packages/*.yaml",
    "*.py", "*.json",
    "esphome/*.yaml", "zigbee2mqtt/configuration.yaml",
    "custom_components/*/manifest.json",
    ".storage/lovelace*",
]

RE_ID = re.compile(r"^- id:", re.M)
RE_SCRIPT = re.compile(r"^[a-z0-9_]+:[ \t]*\r?$", re.M)


def conta(rel, path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            testo = f.read()
    except OSError:
        return None
    if rel == "scripts.yaml":
        return len(RE_SCRIPT.findall(testo))
    return len(RE_ID.findall(testo))


def presenti():
    out = set()
    for m in MODELLI:
        for p in glob.glob(os.path.join(BASE, m)):
            if os.path.isfile(p):
                out.add(os.path.relpath(p, BASE))
    return out


def main():
    try:
        with open(ELENCO, encoding="utf-8") as f:
            prima = f.readline().strip().lstrip("#").strip()
            f.seek(0)
            attesi = (yaml.safe_load(f) or {})["file"]
        if not isinstance(attesi, dict):
            raise ValueError("la chiave file non e' un elenco")
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as e:
        print(f"file_attesi.yaml non leggibile: {e}", file=sys.stderr)
        sys.exit(1)

    mancanti, ridotti = [], []
    for rel, minimo in sorted(attesi.items()):
        path = os.path.join(BASE, rel)
        if not os.path.isfile(path):
            mancanti.append(rel)
        elif isinstance(minimo, int):
            n = conta(rel, path)
            if n is not None and n < minimo:
                ridotti.append(f"{rel} {minimo}->{n}")
    nuovi = sorted(presenti() - set(attesi))

    print(json.dumps({
        "stato": "problemi" if (mancanti or ridotti) else "ok",
        "mancanti": mancanti, "ridotti": ridotti, "nuovi": nuovi,
        "controllati": len(attesi), "elenco": prima,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
