#!/usr/bin/env python3
"""
Sætter enhedsnavne direkte på Shelly-enhederne via lokal API.
Kør én gang fra Ubuntu-serveren (som er på samme netværk som enhederne).
"""

import logging
from pathlib import Path

import requests
import yaml

LOG = logging.getLogger(__name__)
CONFIG_FILE = Path(__file__).parent / "config.yaml"


def set_name_gen1(ip: str, name: str):
    r = requests.post(f"http://{ip}/settings", data={"name": name}, timeout=5)
    r.raise_for_status()


def set_name_gen2(ip: str, name: str):
    r = requests.post(
        f"http://{ip}/rpc/Sys.SetConfig",
        json={"config": {"device": {"name": name}}},
        timeout=5,
    )
    r.raise_for_status()
    # Kræver reboot for at træde i kraft
    requests.post(f"http://{ip}/rpc/Shelly.Reboot", json={}, timeout=5)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    cfg = yaml.safe_load(CONFIG_FILE.read_text())

    for d in cfg.get("shelly_devices", []):
        ip   = d["ip"]
        name = d["name"]
        gen  = int(d.get("gen", 1))
        try:
            if gen >= 2:
                set_name_gen2(ip, name)
            else:
                set_name_gen1(ip, name)
            LOG.info("OK  %-30s %s", name, ip)
        except Exception as exc:
            LOG.warning("FEJL %-30s %s — %s", name, ip, exc)


if __name__ == "__main__":
    main()
