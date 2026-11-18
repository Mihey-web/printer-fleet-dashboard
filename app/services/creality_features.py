"""Creality feeder telemetry and the local commands used by Creality Print."""
import re

from app.vendor.ha_creality_ws.utils import build_modify_material_payload, normalize_color_hex


def chamber_limit(model, data):
    maximum = data.get("max_box_temp")
    if (str(model).lower().replace(" ", "") in ("k2plus", "k2pro")
            and data.get("target_box_temp") is not None and isinstance(maximum, int)
            and 0 < maximum <= 100):
        return maximum
    return None


def feeder(report):
    if not isinstance(report, dict):
        return None
    boxes = []
    for box in report.get("materialBoxs") or []:
        if not isinstance(box, dict) or box.get("type") not in (0, 1) or type(box.get("id")) is not int:
            continue
        slots = []
        for m in box.get("materials") or []:
            if not isinstance(m, dict) or type(m.get("id")) is not int:
                continue
            colour = normalize_color_hex(m.get("color"))
            slots.append({"id": m["id"], "type": m.get("type"), "name": m.get("name"),
                          "color": colour if isinstance(colour, str) else None,
                          "percent": m.get("percent"), "empty": m.get("state") == 0,
                          "selected": m.get("selected") == 1})
        boxes.append({"id": box["id"], "external": box["type"] == 1, "slots": slots,
                      "temp": box.get("temp"), "humidity": box.get("humidity")})
    return boxes or None


def slot_frame(action, params, data):
    for box in data.get("cfs") or []:
        if box["id"] != params.get("ams"):
            continue
        for slot in box["slots"]:
            if slot["id"] != params.get("slot"):
                continue
            address = {"boxId": box["id"], "materialId": slot["id"]}
            if action == "cfs_rfid" and not box["external"]:
                return {"refreshBox": address}
            if slot["empty"]:
                raise ValueError("Слот пуст")
            if action == "cfs_set":
                material = params.get("material")
                if not isinstance(material, str) or not material.strip():
                    raise ValueError("Укажите материал")
                color = params.get("color")
                if color and not re.fullmatch(r"[0-9a-fA-F]{6}", color):
                    raise ValueError("Некорректный цвет")
                return {"modifyMaterial": build_modify_material_payload(
                    box_id=box["id"], slot_id=slot["id"], material_type=material, color=color)}
            if action in ("cfs_load", "cfs_unload"):
                if slot["selected"] != (action == "cfs_unload"):
                    raise ValueError("Активный слот изменился")
                return {"feedInOrOut": {**address, "isFeed": int(action == "cfs_load")}}
    raise ValueError("Слот недоступен")


def history_units(boxes):
    """Use the existing humidity/temperature charts, keyed by physical box id."""
    return [{"index": b["id"], "system": "cfs", "humidity_pct": b["humidity"],
             "temp": b["temp"], "slots": [dict(s, color=(s["color"] or "").lstrip("#"))
                                           for s in b["slots"]]}
            for b in boxes or [] if not b["external"]]


def feeder_settings(data):
    config = data.get("box_config")
    if not data.get("cfs") or not isinstance(config, dict):
        return None
    if config.get("autoRefill") not in (0, 1) or config.get("cAutoFeed") not in (0, 1):
        return None
    return {"auto_refill": bool(config["autoRefill"])}


def settings_frame(params, data):
    settings = feeder_settings(data) or {}
    if params.get("setting") not in settings or type(params.get("enabled")) is not bool:
        raise ValueError("Настройка CFS недоступна или значение неверно")
    # Creality Print sends the whole known config: preserve every sibling option.
    config = dict(data["box_config"])
    config["autoRefill"] = int(params["enabled"])
    return {"boxConfig": config}
