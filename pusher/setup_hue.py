#!/usr/bin/env python3
"""
Opsætter Philips Hue API-nøgle og henter rum + lys.
Kør fra Ubuntu-serveren (som kan nå 192.168.1.12).

1. Tryk knappen på Hue Bridge
2. Kør dette script inden for 30 sekunder
"""

import json
import sys
from pathlib import Path

import requests
import yaml

HUE_IP     = "192.168.1.12"
CONFIG_FILE = Path(__file__).parent / "config.yaml"

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def get_or_create_username():
    cfg = yaml.safe_load(CONFIG_FILE.read_text())
    existing = cfg.get("hue", {}).get("username")
    if existing:
        print(f"Bruger eksisterende API-nøgle: {existing[:8]}…")
        return existing

    print("Opretter ny API-nøgle — tryk knappen på Hue Bridge nu, kør så scriptet inden 30 sek…")
    input("Tryk Enter når du har trykket knappen: ")

    r = requests.post(
        f"https://{HUE_IP}/api",
        json={"devicetype": "energi_dashboard#ubuntu"},
        timeout=10,
        verify=False,
    )
    resp = r.json()
    if isinstance(resp, list) and "success" in resp[0]:
        username = resp[0]["success"]["username"]
        print(f"API-nøgle oprettet: {username}")
        cfg.setdefault("hue", {})["username"] = username
        cfg["hue"]["ip"] = HUE_IP
        CONFIG_FILE.write_text(yaml.dump(cfg, allow_unicode=True, sort_keys=False))
        print("Gemt i config.yaml")
        return username
    else:
        print(f"Fejl: {resp}")
        sys.exit(1)


def fetch_rooms_and_lights(username):
    base = f"http://{HUE_IP}/api/{username}"

    base = f"https://{HUE_IP}/api/{username}"
    lights_raw = requests.get(f"{base}/lights", timeout=10, verify=False).json()
    groups_raw = requests.get(f"{base}/groups", timeout=10, verify=False).json()

    print(f"\n{len(lights_raw)} lys fundet")
    print(f"{len(groups_raw)} grupper/rum fundet\n")

    print("=== RUM ===")
    for gid, g in groups_raw.items():
        if g.get("type") in ("Room", "Zone"):
            lights_in_room = [lights_raw[lid]["name"] for lid in g.get("lights", []) if lid in lights_raw]
            print(f"  [{gid}] {g['name']} ({g['type']}): {', '.join(lights_in_room)}")

    print("\n=== LYS ===")
    for lid, l in lights_raw.items():
        state = l["state"]
        on  = "TÆ" if state["on"] else "  "
        bri = f"{round(state.get('bri',0)/2.54)}%" if state.get("bri") else ""
        print(f"  [{lid}] {on} {l['name']:30} {l['type']:25} {bri}")


if __name__ == "__main__":
    username = get_or_create_username()
    fetch_rooms_and_lights(username)
