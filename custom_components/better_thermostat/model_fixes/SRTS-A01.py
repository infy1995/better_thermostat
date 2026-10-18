"""Quirks for the Aqara SRTS-A01 (Smart radiator thermostat E1) Zigbee TRV.

The SRTS-A01 regulates on an external temperature once its ``sensor``
selector is on ``external``, and reads that temperature from the
``external_temperature_input`` number (0 to 55 °C) nobody else keeps
current. The room temperature is mirrored into that input, with the
selector pointed at it first.

Unlike the Sonoff TRVZB, the SRTS-A01 does not fall back to its own sensor
when the input goes quiet: it keeps regulating on the last value written,
and reports that value as its ``local_temperature``. Better Thermostat stops
writing the input while the room sensor gives no reading, and controls on
the TRV temperature instead, which on this device would be its own stale
input read back. Better Thermostat publishes that switch as the
``control_mode`` attribute of its climate entity, which leaves ``optimal``
for ``sensor_fallback`` (or ``hold``). The quirk listens for that change and
puts the selector back on ``internal``, so the device reports and regulates
on what it measures. The next write, once the room sensor reports again,
puts it back on ``external``.

Everything else stays as the default quirks have it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import logging

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, EventStateChangedData, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_conversion import TemperatureConverter

from custom_components.better_thermostat.model_fixes.default import (
    initial_tweak as default_initial_tweak,
)
from custom_components.better_thermostat.model_fixes.types import (
    ExternalTemperatureQuirk,
    InitialTweakQuirk,
    ModelFixHost,
    ModelQuirks,
)
from custom_components.better_thermostat.utils.helpers import is_sibling_entry

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(
    self: ModelFixHost, entity_id: str, calibration_offset: float
) -> float:
    """Return unchanged local calibration for SRTS-A01 by default."""
    return calibration_offset


def fix_target_temperature_calibration(
    self: ModelFixHost, entity_id: str, temperature: float
) -> float:
    """Return unchanged setpoint temperature for SRTS-A01 by default."""
    return temperature


async def override_set_hvac_mode(
    self: ModelFixHost, entity_id: str, hvac_mode: str
) -> bool:
    """No special HVAC mode handling for SRTS-A01; the generic adapter performs the write.

    Parameters
    ----------
    self : ModelFixHost
            Better Thermostat host providing device state and HA access
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


async def override_set_temperature(
    self: ModelFixHost, entity_id: str, temperature: float
) -> bool:
    """No special setpoint handling for SRTS-A01; the generic adapter performs the write.

    Parameters
    ----------
    self : ModelFixHost
            Better Thermostat host providing device state and HA access
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

# Zigbee2MQTT exposes the SRTS-A01 ``sensor`` enum with exactly these two
# values.
_EXTERNAL_SENSOR_OPTION = "external"
_INTERNAL_SENSOR_OPTION = "internal"

# Range of the external temperature input, in degrees Celsius, for an input
# that publishes no bounds of its own.
_EXTERNAL_TEMPERATURE_MIN_C = 0.0
_EXTERNAL_TEMPERATURE_MAX_C = 55.0

# Attributes of the Better Thermostat climate entity the quirk reads. The
# entity publishes the rung of its control-mode ladder, the optional sensors
# it finds unavailable, and a health entry per TRV it drives, which is how the
# quirk tells the thermostat of its TRV from any other.
_ATTR_CONTROL_MODE = "control_mode"
_ATTR_UNAVAILABLE_SENSORS = "unavailable_sensors"
_ATTR_CALIBRATOR_HEALTH = "calibrator_health"

# Rungs on which Better Thermostat controls without a room reading.
_WITHOUT_A_ROOM_READING = frozenset({"sensor_fallback", "hold"})

# The listener per TRV, keyed by the TRV's climate entity id: the thermostat
# it was registered for, and how to unsubscribe it. A reloaded thermostat is
# a new host and registers anew.
_FALLBACK_WATCHES: dict[str, tuple[ModelFixHost, Callable[[], None]]] = {}

# TRVs whose switch to their own sensor is due, because the device did not
# take it when the thermostat fell back. The next state the thermostat writes
# during the same fallback tries again.
_HAND_BACK_DUE: set[str] = set()


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
        matches nothing. A disabled entry is no sibling either.
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
        The entity id of the sibling whose translation key names it, the
        first id fragment match when no sibling carries one of the keys, or
        ``None`` when the device has no such entity.
    """
    siblings = [
        ent
        for ent in entity_registry.entities.values()
        if is_sibling_entry(ent, device_id) and ent.domain == domain
    ]
    for ent in siblings:
        if ent.translation_key in translation_keys:
            return ent.entity_id
    # The registry hands its entities out in insertion order, so a fragment
    # match tried per entry beats the canonical key of an entry behind it.
    # The fallback therefore runs as a second pass, and only over the entries
    # that name themselves nothing.
    for ent in siblings:
        if ent.translation_key is not None:
            continue
        haystacks = (
            (ent.entity_id or "").lower(),
            (ent.unique_id or "").lower(),
            (ent.original_name or "").lower().replace(" ", "_"),
        )
        if any(id_fragment in haystack for haystack in haystacks):
            return ent.entity_id
    return None


def _sensor_selector(self: ModelFixHost, entity_id: str) -> str | None:
    """Return the ``sensor`` selector on the TRV's device, or ``None``."""
    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)
    if reg_entity is None:
        return None
    return _find_device_entity(
        entity_registry, reg_entity.device_id, "select", _TK_SENSOR_SELECT, "sensor"
    )


async def _select_sensor(
    self: ModelFixHost, entity_id: str, selector: str, option: str
) -> bool:
    """Put ``selector`` on ``option`` unless it is already there.

    Parameters
    ----------
    self : ModelFixHost
        The Better Thermostat instance, supplying ``hass`` and the context
        the service call is made under.
    entity_id : str
        The TRV whose device carries the selector, for log lines.
    selector : str
        The ``select`` entity to set.
    option : str
        The option to put it on.

    Returns
    -------
    bool
        True when the selector is on ``option``, whether this call put it
        there or found it there.
    """
    state = self.hass.states.get(selector)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        # A selector that is not reporting names no option, and the device
        # behind it is in no state to take one either.
        return False
    if state.state == option:
        return True
    options = state.attributes.get("options")
    if not isinstance(options, (list, tuple)) or option not in options:
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 selector %s offers no '%s' option (%s)",
            self.device_name,
            selector,
            option,
            options,
        )
        return False
    await self.hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": selector, "option": option},
        blocking=True,
        context=self.context,
    )
    _LOGGER.debug(
        "better_thermostat %s: set SRTS-A01 %s from '%s' to '%s' (for %s)",
        self.device_name,
        selector,
        state.state,
        option,
        entity_id,
    )
    return True


async def maybe_select_external_sensor(self: ModelFixHost, entity_id: str) -> bool:
    """Point the TRV's sensor selector at the value BT writes.

    Writing the external temperature input achieves nothing while the device
    regulates on its own sensor, and it lands there on its own: a SRTS-A01
    that is re-paired comes back on the internal sensor, and the fallback
    watch puts it there while the room sensor is gone. So the selector is
    checked alongside every write of the input it belongs to.

    Returns
    -------
    bool
        True when the selector is on ``external``.
    """
    selector = _sensor_selector(self, entity_id)
    if selector is None:
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 sensor selector not found for %s",
            self.device_name,
            entity_id,
        )
        return False
    return await _select_sensor(self, entity_id, selector, _EXTERNAL_SENSOR_OPTION)


def _drives(state: State, entity_id: str) -> bool:
    """Whether ``state`` is the Better Thermostat climate entity driving ``entity_id``."""
    if state.domain != "climate":
        return False
    health = state.attributes.get(_ATTR_CALIBRATOR_HEALTH)
    return isinstance(health, Mapping) and entity_id in health


def _without_a_room_reading(state: State | None) -> bool:
    """Whether the thermostat in ``state`` controls without a room reading."""
    if state is None:
        return False
    return state.attributes.get(_ATTR_CONTROL_MODE) in _WITHOUT_A_ROOM_READING


def _unavailable_sensors(state: State) -> frozenset[str]:
    """Return the optional sensors the thermostat in ``state`` finds unavailable."""
    sensors = state.attributes.get(_ATTR_UNAVAILABLE_SENSORS)
    if not isinstance(sensors, (list, tuple)):
        return frozenset()
    return frozenset(str(sensor) for sensor in sensors)


def _falls_back(old: State | None, new: State) -> bool:
    """Whether the change from ``old`` to ``new`` takes the room reading away.

    The ladder leaving ``optimal`` is the main case. A thermostat already
    below it may still be waiting out its recovery window after the sensor
    returned, while the writes have put the device back on the input, so a
    sensor going unavailable again during that window counts as well.
    """
    if not _without_a_room_reading(new):
        return False
    if old is None or not _without_a_room_reading(old):
        return True
    return bool(_unavailable_sensors(new) - _unavailable_sensors(old))


async def _hand_back(self: ModelFixHost, entity_id: str) -> None:
    """Put the TRV back on its own sensor, marking the switch due if it fails."""
    if self.is_removed:
        return
    selector = _sensor_selector(self, entity_id)
    if selector is None:
        _HAND_BACK_DUE.discard(entity_id)
        return
    try:
        switched = await _select_sensor(
            self, entity_id, selector, _INTERNAL_SENSOR_OPTION
        )
    except (HomeAssistantError, OSError, TypeError, ValueError, KeyError) as ex:
        _LOGGER.warning(
            "better_thermostat %s: SRTS-A01 switch of %s to its own sensor failed: %s",
            self.device_name,
            entity_id,
            ex,
        )
        switched = False
    if not switched:
        # The device is asleep or out of reach; the switch is due all the same.
        _HAND_BACK_DUE.add(entity_id)
        return
    _HAND_BACK_DUE.discard(entity_id)
    _LOGGER.info(
        "better_thermostat %s: controls without a room reading, SRTS-A01 %s "
        "regulates on its own sensor until the room sensor reports again",
        self.device_name,
        entity_id,
    )


def _start_hand_back(self: ModelFixHost, entity_id: str) -> None:
    """Run the switch to the TRV's own sensor under the thermostat's task manager."""
    _HAND_BACK_DUE.discard(entity_id)
    self.task_manager.create_task(
        _hand_back(self, entity_id), name=f"bt_srts_a01_internal_sensor_{entity_id}"
    )


def _stop_watching(self: ModelFixHost, entity_id: str) -> None:
    """Unsubscribe the fallback watch ``self`` registered for the TRV."""
    existing = _FALLBACK_WATCHES.get(entity_id)
    if existing is not None and existing[0] is self:
        del _FALLBACK_WATCHES[entity_id]
        _HAND_BACK_DUE.discard(entity_id)
        existing[1]()


def _watch_for_fallback(self: ModelFixHost, entity_id: str) -> None:
    """Listen for the thermostat of the TRV to lose its room reading.

    Registered once per TRV and thermostat. The listener watches every state
    change and keeps the ones of the Better Thermostat entity that names the
    TRV among its own; the quirk is handed no entity id of the thermostat.
    """
    existing = _FALLBACK_WATCHES.get(entity_id)
    if existing is not None:
        if existing[0] is self:
            return
        # A thermostat that was reloaded leaves the listener of its old self.
        existing[1]()

    @callback
    def _on_state_changed(event: Event[EventStateChangedData]) -> None:
        if self.is_removed:
            _stop_watching(self, entity_id)
            return
        new_state = event.data["new_state"]
        if new_state is None or not _drives(new_state, entity_id):
            return
        if not _without_a_room_reading(new_state):
            _HAND_BACK_DUE.discard(entity_id)
            return
        if entity_id in _HAND_BACK_DUE or _falls_back(
            event.data["old_state"], new_state
        ):
            _start_hand_back(self, entity_id)

    unsubscribe = self.hass.bus.async_listen(EVENT_STATE_CHANGED, _on_state_changed)
    _FALLBACK_WATCHES[entity_id] = (self, unsubscribe)


async def initial_tweak(self: ModelFixHost, entity_id: str) -> None:
    """Run the default tweaks and start the fallback watch.

    A room sensor that gives no reading from startup on leaves the input
    without a single write, so the watch starts here rather than with the
    first one. A thermostat that already controls without a room reading
    has its TRV handed back right away: the change into that state may have
    been written before the watch existed.
    """
    await default_initial_tweak(self, entity_id)
    _watch_for_fallback(self, entity_id)
    if any(
        _drives(state, entity_id) and _without_a_room_reading(state)
        for state in self.hass.states.async_all("climate")
    ):
        _start_hand_back(self, entity_id)


def _external_input_value(state: State | None, celsius: float) -> float:
    """Return the value the external temperature input takes for ``celsius``.

    Zigbee2MQTT publishes the input as a ``temperature`` number in Celsius,
    which Home Assistant converts into the system unit, and checks a written
    value against the ``min`` and ``max`` it publishes in that unit. The
    reading is rounded once to the device's tenth of a degree Celsius, then
    converted into the unit the input publishes and held inside its bounds.

    Parameters
    ----------
    state : State or None
        State of the external temperature input, or None when it has none.
    celsius : float
        The room temperature in degrees Celsius.

    Returns
    -------
    float
        The value to write, in the input's published unit.
    """
    attributes: Mapping[str, object] = state.attributes if state is not None else {}
    declared_unit = attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    unit = (
        declared_unit
        if isinstance(declared_unit, str)
        and declared_unit in TemperatureConverter.VALID_UNITS
        else UnitOfTemperature.CELSIUS
    )

    def published(degrees: float) -> float:
        return TemperatureConverter.convert(degrees, UnitOfTemperature.CELSIUS, unit)

    def bound(name: str, fallback_celsius: float) -> float:
        try:
            return float(str(attributes[name]))
        except KeyError, TypeError, ValueError:
            return published(fallback_celsius)

    low = bound("min", _EXTERNAL_TEMPERATURE_MIN_C)
    high = bound("max", _EXTERNAL_TEMPERATURE_MAX_C)
    return max(low, min(high, published(round(celsius, 1))))


async def maybe_set_external_temperature(
    self: ModelFixHost, entity_id: str, temperature: float
) -> bool:
    """Mirror the room temperature into the SRTS-A01 external temperature input.

    Looks for the number entity matching ``external_temperature_input`` on
    the TRV's device and writes the temperature, rounded to a tenth of a
    degree Celsius, converted into the unit the input publishes and held
    inside the range it publishes. The sensor selector is pointed at that
    input first, because a device regulating on its own sensor never reads
    it and may discard a value written to it.

    Parameters
    ----------
    self : ModelFixHost
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
        SRTS-A01, names no such input, the value is not a number, or the
        device refused the write.
    """
    try:
        model = (self.real_trvs[entity_id].model or "").lower()
        if "srts-a01" not in model and "aqara" not in model:
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

        try:
            celsius = float(temperature)
        except TypeError, ValueError:
            _LOGGER.debug(
                "better_thermostat %s: SRTS-A01 maybe_set_external_temperature got non-float: %s",
                self.device_name,
                temperature,
            )
            return False

        # The room sensor reports again, so no switch to the device's own
        # sensor is due any more.
        _watch_for_fallback(self, entity_id)
        _HAND_BACK_DUE.discard(entity_id)

        value = _external_input_value(self.hass.states.get(target), celsius)

        # The selector goes first: a device on its internal sensor can discard
        # the input, so a value written before the switch may never be used.
        await maybe_select_external_sensor(self, entity_id)

        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": target, "value": value},
            blocking=True,
            context=self.context,
        )
        _LOGGER.debug(
            "better_thermostat %s: set SRTS-A01 external_temperature_input=%.2f on %s (for %s)",
            self.device_name,
            value,
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


class _Surface:
    """Quirk surface of the module, bound below to each Protocol it implements."""

    fix_local_calibration = staticmethod(fix_local_calibration)
    fix_target_temperature_calibration = staticmethod(
        fix_target_temperature_calibration
    )
    override_set_hvac_mode = staticmethod(override_set_hvac_mode)
    override_set_temperature = staticmethod(override_set_temperature)
    initial_tweak = staticmethod(initial_tweak)
    maybe_set_external_temperature = staticmethod(maybe_set_external_temperature)


_MODEL_QUIRKS: ModelQuirks = _Surface()
_EXTERNAL_TEMPERATURE_QUIRK: ExternalTemperatureQuirk = _Surface()
_INITIAL_TWEAK_QUIRK: InitialTweakQuirk = _Surface()
