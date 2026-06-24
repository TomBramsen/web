#!/usr/bin/env python3
"""
Energi Dashboard – data pusher
Poll Shelly devices + Eloverblik, upload data.json to GitHub.
"""

import base64
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

LOG = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Copenhagen")
CACHE_FILE = Path(__file__).parent / ".eloverblik_cache.json"
CONFIG_FILE = Path(__file__).parent / "config.yaml"

MONTH_NAMES = [
    "Januar", "Februar", "Marts", "April", "Maj", "Juni",
    "Juli", "August", "September", "Oktober", "November", "December",
]


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


# ── Shelly ──────────────────────────────────────────────────────────────

def _poll_gen1(device: dict) -> dict:
    r = requests.get(f"http://{device['ip']}/status", timeout=5)
    r.raise_for_status()
    d = r.json()
    out = {"name": device["name"], "power_w": 0.0, "on": False}
    if device["type"] == "dimmer":
        out["on"] = bool(d["lights"][0]["ison"])
        out["brightness"] = d["lights"][0].get("brightness", 100)
        out["power_w"] = float(d["meters"][0]["power"])
    else:  # relay / plug / 1PM
        out["on"] = bool(d["relays"][0]["ison"])
        out["power_w"] = float(d["meters"][0]["power"])
    return out


def _poll_gen2(device: dict) -> dict:
    out = {"name": device["name"], "power_w": 0.0, "on": False}
    if device["type"] == "dimmer":
        r = requests.get(f"http://{device['ip']}/rpc/Light.GetStatus?id=0", timeout=5)
        r.raise_for_status()
        d = r.json()
        out["on"] = bool(d["output"])
        out["brightness"] = d.get("brightness", 100)
        out["power_w"] = float(d.get("apower", 0.0))
    else:  # switch / relay
        r = requests.get(f"http://{device['ip']}/rpc/Switch.GetStatus?id=0", timeout=5)
        r.raise_for_status()
        d = r.json()
        out["on"] = bool(d["output"])
        out["power_w"] = float(d.get("apower", 0.0))
    return out


def poll_shelly(device: dict) -> dict:
    try:
        if int(device.get("gen", 1)) >= 2:
            return _poll_gen2(device)
        return _poll_gen1(device)
    except Exception as exc:
        LOG.warning("Shelly %s (%s): %s", device["name"], device["ip"], exc)
        return {"name": device["name"], "power_w": 0.0, "on": False, "error": str(exc)}


# ── Eloverblik ──────────────────────────────────────────────────────────

def _data_token(refresh_token: str) -> str:
    r = requests.get(
        "https://api.eloverblik.dk/customerapi/api/token",
        headers={"Authorization": f"Bearer {refresh_token}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["result"]


def _sum_kwh(response: dict) -> float:
    total = 0.0
    for result in response.get("result", []):
        doc = result.get("MyEnergyData_MarketDocument", {})
        for ts in doc.get("TimeSeries", []):
            for period in ts.get("Period", []):
                for point in period.get("Point", []):
                    total += float(point.get("out_Quantity.quantity", 0))
    return round(total, 3)


def _timeseries(data_token: str, metering_point: str, date_from: str, date_to: str) -> float:
    r = requests.post(
        f"https://api.eloverblik.dk/customerapi/api/meterdata/gettimeseries/{date_from}/{date_to}/Day",
        headers={"Authorization": f"Bearer {data_token}", "Content-Type": "application/json"},
        json={"meteringPoints": {"meteringPoint": [metering_point]}},
        timeout=30,
    )
    r.raise_for_status()
    return _sum_kwh(r.json())


def get_eloverblik(cfg: dict) -> dict:
    today_str = date.today().isoformat()

    if CACHE_FILE.exists():
        cached = json.loads(CACHE_FILE.read_text())
        if cached.get("date") == today_str:
            LOG.info("Eloverblik: using cache")
            return cached

    LOG.info("Eloverblik: fetching")
    try:
        token = _data_token(cfg["token"])
        mp = cfg["metering_point"]
        today = date.today()
        yesterday = today - timedelta(days=1)
        month_start = today.replace(day=1)

        result = {
            "date": today_str,
            "yesterday_kwh": _timeseries(token, mp, yesterday.isoformat(), today.isoformat()),
            "month_kwh": _timeseries(token, mp, month_start.isoformat(), today.isoformat()),
        }
        CACHE_FILE.write_text(json.dumps(result))
        return result
    except Exception as exc:
        LOG.error("Eloverblik fejl: %s", exc)
        if CACHE_FILE.exists():
            return json.loads(CACHE_FILE.read_text())
        return {"date": today_str, "yesterday_kwh": None, "month_kwh": None}


# ── GitHub ──────────────────────────────────────────────────────────────

def push_to_github(cfg: dict, data: dict):
    url = f"https://api.github.com/repos/{cfg['repo']}/contents/{cfg['file_path']}"
    headers = {
        "Authorization": f"token {cfg['token']}",
        "Accept": "application/vnd.github.v3+json",
    }

    r = requests.get(url, headers=headers, timeout=10)
    sha = r.json().get("sha") if r.status_code == 200 else None

    content = base64.b64encode(
        json.dumps(data, ensure_ascii=False, indent=2).encode()
    ).decode()

    body = {
        "message": f"data: {datetime.now(TZ).strftime('%H:%M:%S')}",
        "content": content,
    }
    if sha:
        body["sha"] = sha

    r = requests.put(url, headers=headers, json=body, timeout=15)
    r.raise_for_status()
    LOG.info("GitHub: pushed OK")


# ── Main ────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config()

    devices = [poll_shelly(d) for d in cfg.get("shelly_devices", [])]
    total_w = round(sum(d["power_w"] for d in devices), 1)
    lights_on = sum(1 for d in devices if d.get("on"))

    energy = get_eloverblik(cfg["eloverblik"])

    now = datetime.now(TZ)
    data = {
        "updated": now.isoformat(),
        "power_now_w": total_w,
        "lights_on": lights_on,
        "devices": devices,
        "yesterday_kwh": energy["yesterday_kwh"],
        "month_kwh": energy["month_kwh"],
        "month_name": MONTH_NAMES[now.month - 1],
    }

    push_to_github(cfg["github"], data)


if __name__ == "__main__":
    main()
