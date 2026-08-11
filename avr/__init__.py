"""AV receiver volume backends.

Google TV devices generally route their remote's volume buttons over HDMI-CEC
straight to the amplifier, leaving nothing for a LAN service to drive. Talking
to the receiver directly works regardless of the TV, which is what this is for.

Only Denon/Marantz has been verified against real hardware. The rest were built
from protocol documentation and the source of established open-source
libraries; `BackendSpec.tested` carries that distinction through to the UI so
nobody is misled about it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Type

from .base import AvrClient
from .denon import DenonClient
from .onkyo import OnkyoClient
from .sony import SonyClient
from .yamaha import YamahaClient

__all__ = ["AvrClient", "BackendSpec", "BACKENDS", "build", "brand_choices"]


@dataclass(frozen=True)
class BackendSpec:
    cls: Type[AvrClient]
    label: str
    #: Verified against real hardware by the maintainer.
    tested: bool
    #: Model-range caveat, shown in the UI next to the label.
    note: str = ""


# Brands that share a protocol share a class — Marantz is Denon's firmware
# lineage, and Integra/Pioneer(2016+) ride Onkyo's.
BACKENDS: dict[str, BackendSpec] = {
    "denon": BackendSpec(DenonClient, "Denon", tested=True),
    "marantz": BackendSpec(DenonClient, "Marantz", tested=True),
    "yamaha": BackendSpec(
        YamahaClient, "Yamaha", tested=False,
        note="2010 or newer — RX-V, RX-A/Aventage, TSR, HTR",
    ),
    "onkyo": BackendSpec(
        OnkyoClient, "Onkyo / Integra", tested=False, note="2011 or newer",
    ),
    "pioneer": BackendSpec(
        OnkyoClient, "Pioneer", tested=False, note="2016 or newer only",
    ),
    "sony": BackendSpec(
        SonyClient, "Sony", tested=False,
        note="STR-DN1080 and HT-series soundbars",
    ),
}


def build(brand: str, host: str, port: Optional[int] = None) -> Optional[AvrClient]:
    """Construct the client for a brand, or None if there's no backend.

    Returns None for "none" (the user said they have no receiver) and for any
    unknown brand, so callers can treat both as "no volume control".
    """
    spec = BACKENDS.get((brand or "").strip().lower())
    if spec is None or not host:
        return None
    return spec.cls(host, port)


def brand_choices() -> list[dict]:
    """Brand list for the setup UI, tested ones first."""
    return [
        {"value": key, "label": spec.label, "tested": spec.tested, "note": spec.note}
        for key, spec in sorted(
            BACKENDS.items(), key=lambda kv: (not kv[1].tested, kv[1].label)
        )
    ]
