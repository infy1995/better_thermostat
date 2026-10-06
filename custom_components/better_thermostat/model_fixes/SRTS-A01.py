"""Model quirks for Aqara SRTS-A01."""

from __future__ import annotations

import logging

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from ..utils.const import ROOM_SENSOR_FALLBACK_DELAY_S
from ..utils.helpers import is_sibling_entry
from ..utils.watcher import is_entity_available

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

# The option that hands regulation to the value BT writes. Zigbee2MQTT exposes
# the SRTS-A01 ``sensor`` enum with exactly two values, ``internal`` and
# ``external``. Kept as a tuple, like in the TRVZB quirk, so a future rename
# only needs one more entry here.
_EXTERNAL_SENSOR_OPTIONS = ("external",)

# Selections that already regulate on the external input. A device on any of
# them is left as its owner set it.
_ON_A_REMOTE_SENSOR = ("external",)

# The option that puts regulation back on the TRV's own sensor.
_INTERNAL_SENSOR_OPTION = "internal"

# Key under which the state listener of the room sensor is kept in
# ``trv_state.extra``, so it is registered once per TRV.
_SENSOR_WATCH_KEY = "_srts_a01_room_sensor_watch"

# Key of the pending fallback timer, started when the room sensor drops out.
_FALLBACK_TIMER_KEY = "_srts_a01_room_sensor_fallback_timer"


def _room_sensor_down_past_delay(self) -> bool:
    """Report whether the room sensor has been gone for the fallback delay.

    Mirrors ``climate.py``: BT lets the TRV's own reading stand in for the
    room sensor only once the sensor has been unavailable for
    ``ROOM_SENSOR_FALLBACK_DELAY_S``. Until then BT keeps the last room
    reading, and so does this quirk, so a short dropout does not flip the
    selector there and back.

    Parameters
    ----------
    self :
        The Better Thermostat instance, supplying ``hass`` and
        ``sensor_entity_id``.

    Returns
    -------
    bool
        True when the room sensor is configured, is not available and has
        been in that state for at least the fallback delay.
    """
    sensor_id = getattr(self, "sensor_entity_id", None)
    if not sensor_id:
        return False
    if is_entity_available(self.hass, sensor_id):
        return False
    state = self.hass.states.get(sensor_id)
    if state is None:
        return True
    down_for = (dt_util.utcnow() - state.last_changed).total_seconds()
    return down_for >= ROOM_SENSOR_FALLBACK_DELAY_S


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
        otherwise be a candidate. A disabled entry is no sibling either.
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
    siblings = [
        ent
        for ent in entity_registry.entities.values()
        if is_sibling_entry(ent, device_id) and ent.domain == domain
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
    if str(state.state).startswith(_ON_A_REMOTE_SENSOR):
        return True
    options = state.attributes.get("options")
    option = next(
        (
            name
            for name in _EXTERNAL_SENSOR_OPTIONS
            if isinstance(options, (list, tuple)) and name in options
        ),
        None,
    )
    if option is None:
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 selector %s offers none of %s (%s)",
            self.device_name,
            target,
            _EXTERNAL_SENSOR_OPTIONS,
            options,
        )
        return False
    await self.hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": target, "option": option},
        blocking=True,
        context=self.context,
    )
    _LOGGER.info(
        "better_thermostat %s: set SRTS-A01 %s from '%s' to '%s' (for %s)",
        self.device_name,
        target,
        state.state,
        option,
        entity_id,
    )
    return True


async def maybe_select_internal_sensor(self, entity_id: str) -> bool:
    """Hand regulation back to the TRV's own sensor.

    While the TRV regulates on the external input, ``local_temperature`` is
    that input, so a room sensor that stops reporting leaves the TRV showing
    the last value written to it. On the internal sensor the TRV reports what
    it really measures, which is what BT falls back to. Once the room sensor
    reports again, the next write of the input points the selector back at it
    (see ``maybe_select_external_sensor``).

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
        True when the selector is on the internal option, whether this call
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
        return False
    state = self.hass.states.get(target)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return False
    if state.state == _INTERNAL_SENSOR_OPTION:
        return True
    options = state.attributes.get("options")
    if not isinstance(options, (list, tuple)) or _INTERNAL_SENSOR_OPTION not in options:
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 selector %s offers no '%s' option (%s)",
            self.device_name,
            target,
            _INTERNAL_SENSOR_OPTION,
            options,
        )
        return False
    await self.hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": target, "option": _INTERNAL_SENSOR_OPTION},
        blocking=True,
        context=self.context,
    )
    _LOGGER.info(
        "better_thermostat %s: room sensor gone, set SRTS-A01 %s from '%s' to '%s' (for %s)",
        self.device_name,
        target,
        state.state,
        _INTERNAL_SENSOR_OPTION,
        entity_id,
    )
    return True


async def _fall_back_to_internal(self, entity_id: str) -> None:
    """Switch the TRV to its own sensor, reporting a refused write instead of raising."""
    try:
        await maybe_select_internal_sensor(self, entity_id)
    except (HomeAssistantError, OSError, TypeError, ValueError, KeyError) as ex:
        _LOGGER.warning(
            "better_thermostat %s: SRTS-A01 fallback to internal sensor for %s failed: %s",
            self.device_name,
            entity_id,
            ex,
        )


def _ensure_room_sensor_watch(self, entity_id: str) -> None:
    """Watch the room sensor and fall back to the TRV's sensor when it stays gone.

    Registered once per TRV. When the sensor stops reporting, a timer of
    ``ROOM_SENSOR_FALLBACK_DELAY_S`` starts; if the sensor is still gone when
    it fires, the TRV is put back on its own sensor. A sensor that returns
    first cancels the timer. Recovery after a fallback needs no handling here,
    because the next write of the external input selects the external sensor
    again.

    Parameters
    ----------
    self :
        The Better Thermostat instance, supplying ``hass``, ``sensor_entity_id``
        and the TRV registry.
    entity_id : str
        The TRV to hand back to its own sensor.
    """
    sensor_id = getattr(self, "sensor_entity_id", None)
    trv_state = self.real_trvs.get(entity_id)
    if not sensor_id or trv_state is None:
        return
    if trv_state.extra.get(_SENSOR_WATCH_KEY) is not None:
        return

    def _cancel_timer() -> None:
        cancel = trv_state.extra.pop(_FALLBACK_TIMER_KEY, None)
        if cancel is not None:
            cancel()
            _LOGGER.debug(
                "better_thermostat %s: room sensor %s reports again, SRTS-A01 fallback to %s cancelled",
                self.device_name,
                sensor_id,
                entity_id,
            )

    @callback
    def _fire(_now) -> None:
        trv_state.extra.pop(_FALLBACK_TIMER_KEY, None)
        if is_entity_available(self.hass, sensor_id):
            return
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 fallback timer due for %s, switching to the TRV sensor now",
            self.device_name,
            entity_id,
        )
        self.hass.async_create_background_task(
            _fall_back_to_internal(self, entity_id),
            name=f"bt_srts_a01_internal_sensor_{entity_id}",
        )

    @callback
    def _on_room_sensor_change(_event) -> None:
        if is_entity_available(self.hass, sensor_id):
            _cancel_timer()
            return
        if trv_state.extra.get(_FALLBACK_TIMER_KEY) is not None:
            return
        trv_state.extra[_FALLBACK_TIMER_KEY] = async_call_later(
            self.hass, ROOM_SENSOR_FALLBACK_DELAY_S, _fire
        )
        _LOGGER.debug(
            "better_thermostat %s: room sensor %s unavailable, SRTS-A01 %s falls back to its own sensor in %ss unless the room sensor returns",
            self.device_name,
            sensor_id,
            entity_id,
            ROOM_SENSOR_FALLBACK_DELAY_S,
        )

    unsub_state = async_track_state_change_event(
        self.hass, [sensor_id], _on_room_sensor_change
    )

    def _unsub_all() -> None:
        unsub_state()
        _cancel_timer()

    trv_state.extra[_SENSOR_WATCH_KEY] = _unsub_all
    on_remove = getattr(self, "async_on_remove", None)
    if callable(on_remove):
        on_remove(_unsub_all)

    # The sensor may already be gone when the watch is registered.
    _on_room_sensor_change(None)


async def maybe_set_external_temperature(self, entity_id, temperature: float) -> bool:
    """Set Aqara SRTS-A01 external temperature input via a number entity on the same device.

    Looks for number.* entity matching external_temperature_input and writes the
    given temperature (clamped to 0..55.0, rounded to one decimal). The sensor
    selector is pointed at that input first, because a device regulating on
    its own sensor never reads it and may discard a value written to it.

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
        SRTS-A01, names no such input, the room sensor has been unavailable
        for the fallback delay (the TRV is then put back on its own sensor), the value is not a number,
        or the device refused the write.
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

        _ensure_room_sensor_watch(self, entity_id)

        # Once the room sensor has been gone for the fallback delay, whatever
        # BT would write here is the TRV's own stale reading handed back to
        # it. Leave the input alone and let the TRV measure for itself until
        # the sensor returns. Within the delay BT still holds the last room
        # reading, which is written as usual.
        if _room_sensor_down_past_delay(self):
            _LOGGER.debug(
                "better_thermostat %s: SRTS-A01 external temperature write for %s skipped, room sensor %s is gone",
                self.device_name,
                entity_id,
                getattr(self, "sensor_entity_id", None),
            )
            await maybe_select_internal_sensor(self, entity_id)
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

        # The selector goes first: a device on its internal sensor can discard
        # the input, so a value written before the switch may never be used.
        await maybe_select_external_sensor(self, entity_id)

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
        return True
    except (
        HomeAssistantError,
        OSError,
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
    ) as ex:
        # The device did not take the value: it is asleep, out of reach, its
        # integration is reloading, or it declares a narrower range than the
        # clamp above. Reporting the refused write as a declined one leaves
        # the caller free to serve the remaining TRVs and to control on the
        # new reading; the next write retries.
        _LOGGER.warning(
            "better_thermostat %s: SRTS-A01 external temperature write for %s failed: %s",
            self.device_name,
            entity_id,
            ex,
        )
        return False