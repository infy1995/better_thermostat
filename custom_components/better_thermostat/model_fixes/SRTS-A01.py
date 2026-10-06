"""Model quirks for Aqara SRTS-A01."""

from __future__ import annotations

import logging

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(self, entity_id, offset):
    """Return unchanged local calibration for SRTS-A01 by default."""
    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    """Return unchanged setpoint temperature for SRTS-A01 by default."""
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    """No special HVAC mode handling for SRTS-A01; the generic adapter performs the write.

    Parameters
    ----------
    self :
        self instance of better_thermostat
    entity_id : str
        entity_id of the TRV
    hvac_mode : str
        the HVAC mode to set

    Returns
    -------
    bool
        False, always: the generic adapter fallback performs the
        service call, including its retry handling
    """
    return False


async def override_set_temperature(self, entity_id, temperature):
    """No special setpoint handling for SRTS-A01; the generic adapter performs the write.

    Parameters
    ----------
    self :
        self instance of better_thermostat
    entity_id : str
        entity_id of the TRV
    temperature : float
        the target temperature to set

    Returns
    -------
    bool
        False, always: the generic adapter fallback performs the
        service call, including step rounding and system-unit
        conversion
    """
    return False


# Translation keys Zigbee2MQTT uses for the input the room temperature is
# mirrored into.
_TK_EXTERNAL_TEMP = frozenset({"external_temperature_input", "external_temperature"})

# Translation keys Zigbee2MQTT uses for the selector that decides which sensor
# the SRTS-A01 regulates on.
_TK_SENSOR_SELECT = frozenset({"sensor_select", "sensor"})

# The option that hands regulation to the value BT writes. Devices offer more
# than one option naming an external sensor, so the ones already on such an
# option are left as their owner set them.
_EXTERNAL_SENSOR_OPTION = "external"


def _find_device_entity(
    entity_registry: er.EntityRegistry,
    device_id: str | None,
    domain: str,
    translation_keys: frozenset[str],
    id_fragment: str,
) -> str | None:
    """Return a sibling entity of ``device_id`` in ``domain``, or ``None``.

    The translation key is the stable, language-independent handle and is
    tried first; the id fragment is the fallback for a registry entry that
    carries none.

    Parameters
    ----------
    entity_registry : er.EntityRegistry
        The registry to search.
    device_id : str | None
        The device the sibling has to belong to. ``None`` is no device and
        matches nothing: every entity that belongs to no device would
        otherwise be a candidate.
    domain : str
        The entity domain to search, ``number`` or ``select`` here.
    translation_keys : frozenset[str]
        The translation keys that name the wanted entity.
    id_fragment : str
        Matched against the entity id, unique id and original name of a
        registry entry that carries no translation key.

    Returns
    -------
    str | None
        The entity id of the translation key match, the first id fragment
        match when no entry carries one of the keys, or ``None`` when the
        device has no such entity.
    """
    if device_id is None:
        return None
    siblings = [
        ent
        for ent in entity_registry.entities.values()
        if ent.device_id == device_id and ent.domain == domain
    ]
    for ent in siblings:
        if getattr(ent, "translation_key", None) in translation_keys:
            return ent.entity_id
    # Only now, and only for entries that name themselves nothing: the
    # registry hands its entities out in insertion order, so a fragment match
    # tried per entry would beat the canonical key of an entry behind it.
    for ent in siblings:
        if getattr(ent, "translation_key", None) is not None:
            continue
        haystacks = (
            (ent.entity_id or "").lower(),
            (ent.unique_id or "").lower(),
            (getattr(ent, "original_name", None) or "").lower().replace(" ", "_"),
        )
        if any(id_fragment in haystack for haystack in haystacks):
            return ent.entity_id
    return None


async def maybe_select_external_sensor(self, entity_id: str) -> bool:
    """Point the TRV's sensor selector at the value BT writes.

    Writing the external temperature input achieves nothing while the device
    regulates on its own sensor, and it lands there on its own: a SRTS-A01 that
    is re-paired comes back on the internal sensor. So the selector is checked
    alongside every write of the input it belongs to.

    A device already on an option naming an external sensor is left alone,
    whichever of them it is: the choice between them is its owner's.

    Parameters
    ----------
    self :
        The Better Thermostat instance, supplying ``hass`` and the context
        the service call is made under.
    entity_id : str
        The TRV whose device carries the selector.

    Returns
    -------
    bool
        True when the selector is on an external option, whether this call
        put it there or found it there.
    """
    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)
    if reg_entity is None:
        return False
    target = _find_device_entity(
        entity_registry,
        reg_entity.device_id,
        "select",
        _TK_SENSOR_SELECT,
        "sensor",
    )
    if target is None:
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 temperature sensor selector not found for %s",
            self.device_name,
            entity_id,
        )
        return False
    state = self.hass.states.get(target)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        # A selector that is not reporting names no option, and the device
        # behind it is in no state to take one either.
        return False
    if str(state.state).startswith(_EXTERNAL_SENSOR_OPTION):
        return True
    options = state.attributes.get("options")
    if not isinstance(options, (list, tuple)) or _EXTERNAL_SENSOR_OPTION not in options:
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 selector %s offers no '%s' option (%s)",
            self.device_name,
            target,
            _EXTERNAL_SENSOR_OPTION,
            options,
        )
        return False
    await self.hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": target, "option": _EXTERNAL_SENSOR_OPTION},
        blocking=True,
        context=self.context,
    )
    _LOGGER.debug(
        "better_thermostat %s: set SRTS-A01 %s from '%s' to '%s' (for %s)",
        self.device_name,
        target,
        state.state,
        _EXTERNAL_SENSOR_OPTION,
        entity_id,
    )
    return True


async def maybe_set_external_temperature(self, entity_id, temperature: float) -> bool:
    """Set Aqara SRTS-A01 external temperature input via a number entity on the same device.

    Looks for number.* entity matching external_temperature_input and writes the
    given temperature (clamped to 0..55.0, rounded to one decimal). The sensor
    selector is pointed at that input alongside the write, because a device
    regulating on its own sensor never reads it.

    Parameters
    ----------
    self :
        The Better Thermostat instance, supplying ``hass``, the TRV registry
        and the context the service calls are made under.
    entity_id : str
        The TRV whose device carries the input.
    temperature : float
        The room temperature to mirror into the device, in degrees Celsius.

    Returns
    -------
    bool
        True when the input was written, False when the device is not a
        SRTS-A01, names no such input, or the value is not a number.
    """
    try:
        model = str(self.real_trvs[entity_id].model or "")
        if not (
            "aqara" in model.lower() or "srts-a01" in model.lower() or model == "SRTS-A01"
        ):
            _LOGGER.debug(
                "better_thermostat %s: SRTS-A01 maybe_set_external_temperature skipped (model=%s)",
                self.device_name,
                model,
            )
            return False
        entity_registry = er.async_get(self.hass)
        reg_entity = entity_registry.async_get(entity_id)
        if reg_entity is None:
            _LOGGER.debug(
                "better_thermostat %s: SRTS-A01 maybe_set_external_temperature: no registry entity for %s",
                self.device_name,
                entity_id,
            )
            return False
        target = _find_device_entity(
            entity_registry,
            reg_entity.device_id,
            "number",
            _TK_EXTERNAL_TEMP,
            "external_temperature_input",
        )
        if target is None:
            _LOGGER.debug(
                "better_thermostat %s: SRTS-A01 external_temperature_input number entity not found for %s",
                self.device_name,
                entity_id,
            )
            return False

        # Clamp and round
        try:
            val = float(temperature)
        except TypeError, ValueError:
            _LOGGER.debug(
                "better_thermostat %s: SRTS-A01 maybe_set_external_temperature got non-float: %s",
                self.device_name,
                temperature,
            )
            return False
        val = max(0.0, min(55.0, round(val, 1)))

        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": target, "value": val},
            blocking=True,
            context=self.context,
        )
        _LOGGER.debug(
            "better_thermostat %s: set SRTS-A01 external_temperature_input=%.1f on %s (for %s)",
            self.device_name,
            val,
            target,
            entity_id,
        )
        # The value just written only reaches the control loop of a device
        # that is regulating on it.
        await maybe_select_external_sensor(self, entity_id)
        return True
    except (TypeError, ValueError, KeyError, AttributeError) as ex:
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 maybe_set_external_temperature exception: %s",
            self.device_name,
            ex,
        )
        return False