"""People's names of the Creality models: the one table, on the server.

The registry keeps a Creality model as a code ("k1c", "ender5max", "k2plus"); people read
«K1C», «Ender-5 Max», «K2 Plus». The API adds the name to what it returns (model_name in
/api/printers, /api/admin/printers, /api/ams/current, /api/history/jobs and its stats; the
jobs CSV's «Модель» column), the shared file pool titles its groups with it, the printer
form lists NAMES (/api/admin/printer-models), and a name typed into the form is stored as
its code (code_for).
"""
import re
from typing import Any, Optional

NAMES = {
    "k1": "K1",
    "k1max": "K1 Max",
    "k1c": "K1C",
    "k1se": "K1 SE",
    "k2": "K2",
    "k2plus": "K2 Plus",
    "k2pro": "K2 Pro",
    "k2se": "K2 SE",
    "ender3v3": "Ender-3 V3",
    "ender3v3ke": "Ender-3 V3 KE",
    "ender3v3plus": "Ender-3 V3 Plus",
    "ender5max": "Ender-5 Max",
    "cr10se": "CR-10 SE",
    "hi": "Hi",
    "sparkxi7": "SPARKX i7",
}


def _key(text: str) -> str:
    return re.sub(r"[\s-]+", "", text.lower())


def display_name(model: Any) -> Optional[str]:
    """The name of a model code ("k1c" -> «K1C», any case; a name as typed, «k1 max», gives its own
    «K1 Max»), None for any other model."""
    if not isinstance(model, str):
        return None
    return NAMES.get(_key(model))


def code_for(name: Any) -> Any:
    """The code of a model typed as its name («K2 Plus», «ender-5 max» -> "k2plus" / "ender5max");
    anything else as given (an unknown model is stored as typed)."""
    if not isinstance(name, str):
        return name
    key = _key(name)
    return key if key in NAMES else name
