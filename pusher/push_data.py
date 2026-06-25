#!/usr/bin/env python3
"""
Energi Dashboard – data pusher
Poll Shelly + Eloverblik + Energi Data Service spot priser → GitHub Gist
"""

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

LOG = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Copenhagen")
CONFIG_FILE = Path(__file__).parent / "config.yaml"
DAILY_CACHE = Path(__file__).parent / ".daily_cache.json"
HISTORY_CACHE = Path(__file__).parent / ".history_cache.json"

MONTH_NAMES = ["Januar","Februar","Marts","April","Maj","Juni",
               "Juli","August","September","Oktober","November","December"]
DAY_NAMES   = ["Man","Tir","Ons","Tor","Fre","Lør","Søn"]


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
    else:
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
    else:
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


# ── Eloverblik helpers ───────────────────────────────────────────────────

def _data_token(refresh_token: str) -> str:
    r = requests.get(
        "https://api.eloverblik.dk/customerapi/api/token",
        headers={"Authorization": f"Bearer {refresh_token}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["result"]


def _fetch_timeseries(data_token: str, metering_point: str,
                      date_from: str, date_to: str, aggregation: str) -> dict:
    r = requests.post(
        f"https://api.eloverblik.dk/customerapi/api/meterdata/gettimeseries"
        f"/{date_from}/{date_to}/{aggregation}",
        headers={"Authorization": f"Bearer {data_token}", "Content-Type": "application/json"},
        json={"meteringPoints": {"meteringPoint": [metering_point]}},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _sum_kwh(response: dict) -> float:
    total = 0.0
    for result in response.get("result", []):
        for ts in result.get("MyEnergyData_MarketDocument", {}).get("TimeSeries", []):
            for period in ts.get("Period", []):
                for point in period.get("Point", []):
                    total += float(point.get("out_Quantity.quantity", 0))
    return round(total, 3)


def _parse_hourly(response: dict) -> dict:
    """Parse Hour-aggregated Eloverblik response → {day_str: {hour: kwh}}"""
    hourly: dict = {}
    for result in response.get("result", []):
        for ts in result.get("MyEnergyData_MarketDocument", {}).get("TimeSeries", []):
            for period in ts.get("Period", []):
                start_utc = datetime.fromisoformat(
                    period["timeInterval"]["start"].replace("Z", "+00:00")
                )
                for point in period.get("Point", []):
                    pos = int(point["position"])
                    kwh = float(point.get("out_Quantity.quantity", 0))
                    local = (start_utc + timedelta(hours=pos - 1)).astimezone(TZ)
                    hourly.setdefault(local.date().isoformat(), {})[local.hour] = kwh
    return hourly


# ── Eloverblik: daglige totaler (yesterday + måned) ─────────────────────

def get_daily_totals(cfg: dict) -> dict:
    today_str = date.today().isoformat()
    if DAILY_CACHE.exists():
        c = json.loads(DAILY_CACHE.read_text())
        if c.get("date") == today_str:
            LOG.info("Eloverblik daily: cache")
            return c
    LOG.info("Eloverblik daily: henter")
    try:
        token = _data_token(cfg["token"])
        mp = cfg["metering_point"]
        today = date.today()
        result = {
            "date": today_str,
            "yesterday_kwh": _sum_kwh(_fetch_timeseries(
                token, mp, (today - timedelta(1)).isoformat(), today_str, "Day")),
            "month_kwh": _sum_kwh(_fetch_timeseries(
                token, mp, today.replace(day=1).isoformat(), today_str, "Day")),
        }
        DAILY_CACHE.write_text(json.dumps(result))
        return result
    except Exception as exc:
        LOG.error("Eloverblik daily: %s", exc)
        if DAILY_CACHE.exists():
            return json.loads(DAILY_CACHE.read_text())
        return {"date": today_str, "yesterday_kwh": None, "month_kwh": None}


# ── Spot priser (Energi Data Service) ───────────────────────────────────

def fetch_spot_prices(date_from: str, date_to: str, price_area: str) -> dict:
    """Returnerer {HourDK[:16]: DKK/kWh}  — gratis API, ingen auth"""
    r = requests.get(
        "https://api.energidataservice.dk/dataset/Elspotprices",
        params={
            "start": f"{date_from}T00:00",
            "end": f"{date_to}T00:00",
            "filter": json.dumps({"PriceArea": price_area}),
            "sort": "HourDK",
            "columns": "HourDK,SpotPriceDKK",
            "limit": 300,
        },
        timeout=15,
    )
    r.raise_for_status()
    return {
        rec["HourDK"][:16]: round(rec["SpotPriceDKK"] / 1000.0, 5)
        for rec in r.json().get("records", [])
        if rec.get("SpotPriceDKK") is not None
    }


# ── 7-dages historik med pris ────────────────────────────────────────────

def get_history(eloverblik_cfg: dict, price_area: str, tariffs: dict) -> list:
    today_str = date.today().isoformat()
    if HISTORY_CACHE.exists():
        c = json.loads(HISTORY_CACHE.read_text())
        if c.get("date") == today_str:
            LOG.info("History: cache")
            return c["history"]

    LOG.info("History: henter 7 dage")
    try:
        today = date.today()
        date_from = (today - timedelta(days=7)).isoformat()

        token = _data_token(eloverblik_cfg["token"])
        mp = eloverblik_cfg["metering_point"]

        hourly = _parse_hourly(_fetch_timeseries(token, mp, date_from, today_str, "Hour"))
        spot   = fetch_spot_prices(date_from, today_str, price_area)

        moms  = 1 + tariffs.get("moms", 0.25)
        fixed = (tariffs.get("elafgift", 0.761)
               + tariffs.get("systemtarif", 0.054)
               + tariffs.get("transmissionstarif", 0.049)
               + tariffs.get("dso_tarif", 0.0))

        history = []
        for i in range(7, 0, -1):
            day = today - timedelta(days=i)
            day_str = day.isoformat()
            day_hours = hourly.get(day_str, {})

            hours = []
            for h in range(24):
                kwh = day_hours.get(h, 0.0)
                spot_key = f"{day_str}T{h:02d}:00"
                spot_dkk = spot.get(spot_key, 0.0)
                cost = round(kwh * (spot_dkk + fixed) * moms, 3)
                hours.append({
                    "hour": h,
                    "kwh": round(kwh, 3),
                    "spot_ore": round(spot_dkk * 100, 1),
                    "cost_dkk": cost,
                })

            history.append({
                "date": day_str,
                "day_name": DAY_NAMES[day.weekday()],
                "kwh": round(sum(h["kwh"] for h in hours), 2),
                "cost_dkk": round(sum(h["cost_dkk"] for h in hours), 2),
                "hours": hours,
            })

        HISTORY_CACHE.write_text(json.dumps({"date": today_str, "history": history}))
        return history

    except Exception as exc:
        LOG.error("History: %s", exc)
        if HISTORY_CACHE.exists():
            return json.loads(HISTORY_CACHE.read_text()).get("history", [])
        return []


# ── GitHub Gist ──────────────────────────────────────────────────────────

def push_to_gist(cfg: dict, data: dict):
    r = requests.patch(
        f"https://api.github.com/gists/{cfg['gist_id']}",
        headers={
            "Authorization": f"token {cfg['token']}",
            "Accept": "application/vnd.github.v3+json",
        },
        json={"files": {"data.json": {"content": json.dumps(data, ensure_ascii=False, indent=2)}}},
        timeout=15,
    )
    r.raise_for_status()
    LOG.info("Gist: OK")


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config()

    devices    = [poll_shelly(d) for d in cfg.get("shelly_devices", [])]
    total_w    = round(sum(d["power_w"] for d in devices), 1)
    lights_on  = sum(1 for d in devices if d.get("on"))
    daily      = get_daily_totals(cfg["eloverblik"])
    history    = get_history(cfg["eloverblik"], cfg.get("price_area", "DK1"), cfg.get("tariffs", {}))

    now = datetime.now(TZ)
    push_to_gist(cfg["github"], {
        "updated":       now.isoformat(),
        "power_now_w":   total_w,
        "lights_on":     lights_on,
        "devices":       devices,
        "yesterday_kwh": daily["yesterday_kwh"],
        "month_kwh":     daily["month_kwh"],
        "month_name":    MONTH_NAMES[now.month - 1],
        "history":       history,
    })


if __name__ == "__main__":
    main()
