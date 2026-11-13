"""The alert types: their names and which source sends each. Shared by the service (its alert rules, `about`) and the bot (its alert
menus, lib/alerts/menu.py), with nothing of Telegram in it, so the service doesn't load the Telegram library."""

# kind -> (the name on its button, the source that sends it: a source's name, or Irrigation for the controller), in the order listed
TYPES = {"rain": ("rain", "Ecowitt"), "gusts": ("gusts", "Ecowitt"), "uv": ("UV", "Ecowitt"), "temps": ("temperature crossing", "Ecowitt"),
         "particulates": ("particulates", "AirGradient"), "pollen_and_asthma": ("pollen & asthma", "Pollen"),
         "forecast": ("forecast changes", "Forecast"),
         "irrigation_battery": ("irrigation battery", "Irrigation"), "irrigation_pause": ("irrigation pause", "Irrigation")}
LABELS = {kind: label for kind, (label, _) in TYPES.items()}
RAIN_FROM_PCT = 50   # a day's rain is likely from this chance of at least 1 mm (the forecast alert's, and the irrigation check's)


def kinds_of(source: str) -> tuple[str, ...]:
    """The alert types a source (by name) sends: a monitor's KINDS."""
    return tuple(kind for kind, (_, sender) in TYPES.items() if sender == source)


def available_kinds(sources, irrigation: bool = False) -> list[str]:
    """The alert types the bot sends: those of its sources (by name), and the irrigation ones with a controller configured."""
    names = {*sources, *(["Irrigation"] if irrigation else [])}
    return [kind for kind, (_, source) in TYPES.items() if source in names]
