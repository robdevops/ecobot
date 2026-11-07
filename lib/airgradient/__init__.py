from .metrics import pm25_aqi, rating


def __getattr__(name: str):
    """The client (AirGradient, Latest) is imported when it is first asked for, not with the package: a part that needs only the
    metrics (the storage's day figures, scripts/edit_readings.py) does without the client's libraries (httpx)."""
    if name in ("AirGradient", "Latest"):
        from . import source
        return getattr(source, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["AirGradient", "Latest", "pm25_aqi", "rating"]
