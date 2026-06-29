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
HT_CACHE = Path(__file__).parent / ".ht_cache.json"
POWER_LOG = Path(__file__).parent / ".power_log.json"
HUE_CACHE = Path(__file__).parent / ".hue_cache.json"
HT_CLOUD_TTL_MIN = 10  # hent fra cloud højst hvert 10. minut

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
    dtype = device["type"].lower()
    if device["type"] == "dimmer":
        r = requests.get(f"http://{device['ip']}/rpc/Light.GetStatus?id=0", timeout=5)
        r.raise_for_status()
        d = r.json()
        out["on"] = bool(d["output"])
        out["brightness"] = d.get("brightness", 100)
        out["power_w"] = float(d.get("apower", 0.0))
    elif "pm mini" in dtype or dtype == "pm":
        # Shelly PM Mini Gen 3 — kun måler, intet relæ
        r = requests.get(f"http://{device['ip']}/rpc/PM1.GetStatus?id=0", timeout=5)
        r.raise_for_status()
        d = r.json()
        out["on"] = True
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
    """Parse Hour eller Quarter Eloverblik response → {day_str: {hour: kwh}}
    Kvarter-værdier summeres til timer (spotpris er pr. time)."""
    hourly: dict = {}
    for result in response.get("result", []):
        for ts in result.get("MyEnergyData_MarketDocument", {}).get("TimeSeries", []):
            for period in ts.get("Period", []):
                start_utc = datetime.fromisoformat(
                    period["timeInterval"]["start"].replace("Z", "+00:00")
                )
                # PT15M = kvarter, PT1H = time
                mins = 15 if period.get("resolution", "PT1H") == "PT15M" else 60
                for point in period.get("Point", []):
                    pos = int(point["position"])
                    kwh = float(point.get("out_Quantity.quantity", 0))
                    local = (start_utc + timedelta(minutes=(pos - 1) * mins)).astimezone(TZ)
                    day_str = local.date().isoformat()
                    hour = local.hour
                    hourly.setdefault(day_str, {})[hour] = hourly.get(day_str, {}).get(hour, 0.0) + kwh
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
    records = r.json().get("records", [])
    if records:
        LOG.info("Spot nøgle eksempel: %r", records[0].get("HourDK", "")[:16])
    return {
        rec["HourDK"][:16].replace(" ", "T"): round(rec["SpotPriceDKK"] / 1000.0, 5)
        for rec in records
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

        hourly = _parse_hourly(_fetch_timeseries(token, mp, date_from, today_str, "Quarter"))
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


# ── Shelly H&T sensorer via Cloud API (auto-opdagelse) ───────────────────

def _parse_device_temp(dev: dict):
    """Returnerer {temp_c, humidity, battery} hvis enheden er en temp-sensor."""
    # Gen 1 WiFi H&T: bruger 'tmp' + 'hum'
    if "tmp" in dev and "hum" in dev:
        return {
            "temp_c":   round(dev["tmp"]["tC"], 1),
            "humidity": round(dev["hum"]["value"], 1),
            "battery":  dev.get("bat", {}).get("value"),
        }
    # Gen 2+ / BLU H&T: bruger 'temperature:0' + 'humidity:0'
    if "temperature:0" in dev and "humidity:0" in dev:
        return {
            "temp_c":   round(dev["temperature:0"]["tC"], 1),
            "humidity": round(dev["humidity:0"]["rh"], 1),
            "battery":  dev.get("devicepower:0", {}).get("battery", {}).get("percent"),
        }
    return None


def poll_ht_sensors(sensors_cfg: list, cloud_cfg: dict) -> list:
    """Henter alle H&T data fra Shelly Cloud i ét kald. Cache i 10 minutter."""
    cache = {}
    if HT_CACHE.exists():
        try:
            cache = json.loads(HT_CACHE.read_text())
        except Exception:
            pass

    now = datetime.now(TZ)
    name_map = {s["cloud_id"]: s["name"] for s in sensors_cfg if s.get("cloud_id")}

    # Check om cache er frisk nok
    cache_age_min = None
    if cache.get("_fetched"):
        cache_age_min = (now - datetime.fromisoformat(cache["_fetched"])).total_seconds() / 60

    if cache_age_min is None or cache_age_min >= HT_CLOUD_TTL_MIN:
        try:
            r = requests.get(
                f"{cloud_cfg['server']}/device/all_status",
                params={"auth_key": cloud_cfg["auth_key"]},
                timeout=15,
            )
            r.raise_for_status()
            body = r.json()
            if not body.get("isok"):
                raise ValueError(body.get("errors"))
            devices = body["data"]["devices_status"]

            fresh: dict = {"_fetched": now.isoformat()}
            for dev_id, dev in devices.items():
                parsed = _parse_device_temp(dev)
                if parsed:
                    sensor_updated = dev.get("_updated") or now.isoformat()
                    fresh[dev_id] = {**parsed, "updated": sensor_updated}

            cache = fresh
            HT_CACHE.write_text(json.dumps(cache))
            LOG.info("H&T cloud: %d sensorer opdateret", len(fresh) - 1)
        except Exception as exc:
            LOG.warning("H&T cloud: %s — bruger cache", exc)

    results = []
    for dev_id, entry in cache.items():
        if dev_id == "_fetched":
            continue
        name = name_map.get(dev_id, dev_id)
        age_min = None
        if entry.get("updated"):
            try:
                age_min = (now - datetime.fromisoformat(entry["updated"].replace(" ", "T"))).total_seconds() / 60
            except Exception:
                pass
        gruppe = next((s.get("gruppe") for s in sensors_cfg if s.get("cloud_id") == dev_id), None)
        results.append({
            "id":       dev_id,
            "name":     name,
            "gruppe":   gruppe,
            "temp_c":   entry.get("temp_c"),
            "humidity": entry.get("humidity"),
            "battery":  entry.get("battery"),
            "updated":  entry.get("updated"),
            "stale":    age_min is not None and age_min > 60,
        })
        LOG.info("H&T %s: %.1f°C", name, entry.get("temp_c", 0))

    results.sort(key=lambda x: x["name"])
    return results


# ── Philips Hue ──────────────────────────────────────────────────────────

def fetch_hue(cfg: dict) -> list:
    """Henter rum og lys fra Hue Bridge. Returnerer liste af rum med lys."""
    ip       = cfg["ip"]
    username = cfg["username"]
    grupper  = {int(k): v for k, v in cfg.get("rum_grupper", {}).items()}
    base     = f"https://{ip}/api/{username}"

    cache = {}
    if HUE_CACHE.exists():
        try:
            cache = json.loads(HUE_CACHE.read_text())
        except Exception:
            pass

    try:
        lights = requests.get(f"{base}/lights", timeout=5, verify=False).json()
        groups = requests.get(f"{base}/groups", timeout=5, verify=False).json()

        rooms = []
        for gid_str, g in groups.items():
            if g.get("type") not in ("Room", "Zone") or g.get("type") == "Zone":
                continue
            gid = int(gid_str)
            gruppe = grupper.get(gid, "Andet")
            room_lights = []
            for lid in g.get("lights", []):
                if lid not in lights:
                    continue
                l = lights[lid]
                s = l["state"]
                room_lights.append({
                    "name": l["name"],
                    "on":   bool(s["on"]),
                    "bri":  round(s["bri"] / 2.54) if s.get("bri") else None,
                    "type": l.get("type", ""),
                })
            rooms.append({
                "id":     gid,
                "name":   g["name"],
                "gruppe": gruppe,
                "on":     g["state"].get("any_on", False),
                "lights": room_lights,
            })

        rooms.sort(key=lambda r: (
            ["1. sal","Stue","Kælder","Andet"].index(r["gruppe"])
            if r["gruppe"] in ["1. sal","Stue","Kælder","Andet"] else 99,
            r["name"]
        ))

        cache = {"updated": datetime.now(TZ).isoformat(), "rooms": rooms}
        HUE_CACHE.write_text(json.dumps(cache, ensure_ascii=False))
        LOG.info("Hue: %d rum, %d lys", len(rooms), sum(len(r["lights"]) for r in rooms))
        return rooms

    except Exception as exc:
        LOG.warning("Hue: %s — bruger cache", exc)
        return cache.get("rooms", [])


# ── Watt-log (minut-opløsning, én dag) ───────────────────────────────────

def append_power_log(devices: list, total_w: float) -> list:
    """Gemmer aktuel watt-måling og returnerer dagens log."""
    now = datetime.now(TZ)
    today = now.date().isoformat()
    t_str = now.strftime("%H:%M")

    log = {}
    if POWER_LOG.exists():
        try:
            log = json.loads(POWER_LOG.read_text())
        except Exception:
            pass

    # Nulstil ved ny dag
    if log.get("date") != today:
        log = {"date": today, "readings": []}

    log["readings"].append({
        "t": t_str,
        "w": total_w,
        "d": {d["name"]: round(d["power_w"], 1) for d in devices},
    })

    # Behold max 1440 punkter (24t × 60min)
    if len(log["readings"]) > 1440:
        log["readings"] = log["readings"][-1440:]

    POWER_LOG.write_text(json.dumps(log))
    return log["readings"]


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
    sensors    = poll_ht_sensors(cfg.get("ht_sensors", []), cfg.get("shelly_cloud", {}))
    hue_rooms  = fetch_hue(cfg["hue"]) if cfg.get("hue") else []
    power_log  = append_power_log(devices, total_w)

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
        "sensors":       sensors,
        "power_log":     power_log,
        "hue_rooms":     hue_rooms,
    })


if __name__ == "__main__":
    main()
