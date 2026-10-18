"""Tests for the Aqara SRTS-A01 external temperature and sensor selector quirk."""

import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, EventStateChangedData, State
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.better_thermostat.trv import Trv
from tests.factories import ThermostatStandIn, make_entity_registry, make_registry_entry

quirk = importlib.import_module(
    "custom_components.better_thermostat.model_fixes.SRTS-A01"
)

TRV = "climate.trv1"
INPUT = "number.trv1_external_temperature_input"
SELECTOR = "select.trv1_sensor"
BT = "climate.living_room"
ROOM_SENSOR = "sensor.living_room_temperature"


def _entry(entity_id, *, translation_key=None, device_id="dev1"):
    """A registry entry on the TRV's device; its domain is the entity id's."""
    return make_registry_entry(
        entity_id, translation_key=translation_key, device_id=device_id
    )


def _make_self(monkeypatch, *, selected="internal", model="SRTS-A01", entries=None):
    """A BT stand-in whose SRTS-A01 carries the input and the sensor selector.

    ``selected`` is what the selector reports; ``None`` stands for a selector
    that publishes no state.
    """
    mock_self = ThermostatStandIn()
    mock_self.device_name = "test_thermostat"
    mock_self.context = MagicMock()
    mock_self.is_removed = False
    mock_self.hass.services.async_call = AsyncMock()
    mock_self.real_trvs = {TRV: Trv(entity_id=TRV, model=model)}
    registry = make_entity_registry(
        *(
            entries
            if entries is not None
            else [
                _entry(TRV),
                _entry(INPUT, translation_key="external_temperature_input"),
                _entry(SELECTOR, translation_key="sensor"),
            ]
        )
    )
    monkeypatch.setattr(quirk.er, "async_get", lambda hass: registry, raising=True)

    states = {
        INPUT: State(INPUT, "20.0", {"min": 0, "max": 55, "unit_of_measurement": "°C"})
    }
    if selected is not None:
        states[SELECTOR] = State(
            SELECTOR, selected, {"options": ["internal", "external"]}
        )
    mock_self.hass.states.get = states.get
    mock_self.states = states
    mock_self.hass.states.async_all = lambda domain: [
        state for state in states.values() if state.domain == domain
    ]
    listeners: list[object] = []
    unsubscribed: list[object] = []
    mock_self.listeners = listeners
    mock_self.unsubscribed = unsubscribed

    def _async_listen(event_type, listener):
        assert event_type == EVENT_STATE_CHANGED
        mock_self.listeners.append(listener)
        return MagicMock(side_effect=lambda: mock_self.unsubscribed.append(listener))

    mock_self.hass.bus.async_listen = _async_listen
    return mock_self


def _payloads(mock_self):
    """Every (domain, service, data) the quirk dispatched, in order."""
    return [
        (call.args[0], call.args[1], call.args[2])
        for call in mock_self.hass.services.async_call.await_args_list
    ]


def _bt_state(control_mode, *, trvs=(TRV,), unavailable=()):
    """A state of the Better Thermostat entity driving ``trvs``."""
    return State(
        BT,
        "heat",
        {
            "control_mode": control_mode,
            "unavailable_sensors": list(unavailable),
            "calibrator_health": dict.fromkeys(trvs, "ok"),
        },
    )


async def _change(mock_self, old_state, new_state):
    """Deliver one state change to every listener and let the work it starts run."""
    event = Event(
        EVENT_STATE_CHANGED,
        EventStateChangedData(
            entity_id=new_state.entity_id, old_state=old_state, new_state=new_state
        ),
    )
    for listener in list(mock_self.listeners):
        listener(event)
    for _ in range(5):
        await asyncio.sleep(0)


def _to_internal():
    """The call that puts the TRV on its own sensor."""
    return ("select", "select_option", {"entity_id": SELECTOR, "option": "internal"})


@pytest.fixture(autouse=True)
def _no_watch_outlives_its_test():
    """Forget every fallback watch a test registered."""
    yield
    quirk._FALLBACK_WATCHES.clear()
    quirk._HAND_BACK_DUE.clear()


class TestMaybeSetExternalTemperature:
    """The room temperature reaches the input the device regulates on."""

    @pytest.mark.asyncio
    async def test_the_selector_is_switched_before_the_input_is_written(
        self, monkeypatch
    ):
        """A device on its internal sensor may discard a value written first."""
        mock_self = _make_self(monkeypatch, selected="internal")

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 21.42)

        assert _payloads(mock_self) == [
            ("select", "select_option", {"entity_id": SELECTOR, "option": "external"}),
            ("number", "set_value", {"entity_id": INPUT, "value": 21.4}),
        ]

    @pytest.mark.asyncio
    async def test_a_selector_already_on_external_is_left_alone(self, monkeypatch):
        """Only the input is written while the device already reads it."""
        mock_self = _make_self(monkeypatch, selected="external")

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 19.0)

        assert _payloads(mock_self) == [
            ("number", "set_value", {"entity_id": INPUT, "value": 19.0})
        ]

    @pytest.mark.asyncio
    async def test_the_value_is_held_inside_the_published_range(self, monkeypatch):
        """Zigbee2MQTT publishes the input as 0 to 55 °C."""
        mock_self = _make_self(monkeypatch, selected="external")

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 70.0)

        assert _payloads(mock_self)[-1][2] == {"entity_id": INPUT, "value": 55.0}

    @pytest.mark.asyncio
    async def test_an_input_without_bounds_is_held_to_the_device_range(
        self, monkeypatch
    ):
        """Without published bounds the SRTS-A01 range applies, not the TRVZB one."""
        mock_self = _make_self(monkeypatch, selected="external")
        mock_self.states[INPUT] = State(INPUT, "20.0", {})

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 70.0)

        assert _payloads(mock_self)[-1][2] == {"entity_id": INPUT, "value": 55.0}

    @pytest.mark.asyncio
    async def test_another_model_is_not_written(self, monkeypatch):
        """The quirk answers only for the device it was written for."""
        mock_self = _make_self(monkeypatch, model="TRVZB")

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 21.0) is False

        assert _payloads(mock_self) == []
        assert TRV not in quirk._FALLBACK_WATCHES

    @pytest.mark.asyncio
    async def test_a_device_without_the_input_is_not_written(self, monkeypatch):
        """Nothing on the device takes the room temperature."""
        mock_self = _make_self(
            monkeypatch,
            entries=[_entry(TRV), _entry(SELECTOR, translation_key="sensor")],
        )

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 21.0) is False

        assert _payloads(mock_self) == []

    @pytest.mark.asyncio
    async def test_a_refused_write_is_reported_as_declined(self, monkeypatch):
        """A device that does not take the value leaves the caller free to go on."""
        mock_self = _make_self(monkeypatch, selected="external")
        mock_self.hass.services.async_call.side_effect = HomeAssistantError("asleep")

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 21.0) is False


class TestFallbackWatch:
    """The device is handed its own sensor once BT controls without a room reading."""

    async def test_the_watch_is_registered_once_per_thermostat(self, monkeypatch):
        """The initial tweak and every write share one listener."""
        mock_self = _make_self(monkeypatch, selected="external")
        monkeypatch.setattr(quirk, "default_initial_tweak", AsyncMock())

        await quirk.initial_tweak(mock_self, TRV)
        await quirk.maybe_set_external_temperature(mock_self, TRV, 21.0)

        assert len(mock_self.listeners) == 1

    async def test_a_reloaded_thermostat_replaces_the_old_watch(self, monkeypatch):
        """The listener of a thermostat's old self is let go."""
        old_self = _make_self(monkeypatch, selected="external")
        quirk._watch_for_fallback(old_self, TRV)
        new_self = _make_self(monkeypatch, selected="external")

        quirk._watch_for_fallback(new_self, TRV)

        assert old_self.unsubscribed == old_self.listeners
        assert quirk._FALLBACK_WATCHES[TRV][0] is new_self

    @pytest.mark.parametrize("rung", ["sensor_fallback", "hold"])
    async def test_losing_the_room_reading_hands_the_device_its_own_sensor(
        self, monkeypatch, rung
    ):
        """On its internal sensor the TRV reports what it measures again."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_for_fallback(mock_self, TRV)

        await _change(mock_self, _bt_state("optimal"), _bt_state(rung))

        assert _payloads(mock_self) == [_to_internal()]

    async def test_the_first_state_of_a_thermostat_without_a_reading_counts(
        self, monkeypatch
    ):
        """A thermostat that starts without a room reading has no old state."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_for_fallback(mock_self, TRV)

        await _change(mock_self, None, _bt_state("sensor_fallback"))

        assert _payloads(mock_self) == [_to_internal()]

    async def test_another_thermostat_is_ignored(self, monkeypatch):
        """Only the thermostat that drives this TRV decides for it."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_for_fallback(mock_self, TRV)

        await _change(
            mock_self,
            _bt_state("optimal", trvs=("climate.other",)),
            _bt_state("sensor_fallback", trvs=("climate.other",)),
        )

        assert _payloads(mock_self) == []

    async def test_the_recovery_window_leaves_the_device_on_the_input(
        self, monkeypatch
    ):
        """The ladder climbs back only after the sensor has been stable a while.

        Meanwhile the room sensor reports, BT writes the input again, and a
        state written during that window must not take the device off it.
        """
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_for_fallback(mock_self, TRV)

        await _change(
            mock_self,
            _bt_state("sensor_fallback", unavailable=(ROOM_SENSOR,)),
            _bt_state("sensor_fallback"),
        )

        assert _payloads(mock_self) == []

    async def test_a_sensor_lost_again_in_the_recovery_window_counts(self, monkeypatch):
        """The ladder is still below optimal, so only the sensor list changes."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_for_fallback(mock_self, TRV)

        await _change(
            mock_self,
            _bt_state("sensor_fallback"),
            _bt_state("sensor_fallback", unavailable=(ROOM_SENSOR,)),
        )

        assert _payloads(mock_self) == [_to_internal()]

    async def test_a_device_already_on_its_own_sensor_is_left_alone(self, monkeypatch):
        """Nothing is written when the device already regulates on itself."""
        mock_self = _make_self(monkeypatch, selected="internal")
        quirk._watch_for_fallback(mock_self, TRV)

        await _change(mock_self, _bt_state("optimal"), _bt_state("sensor_fallback"))

        assert _payloads(mock_self) == []

    async def test_a_refused_switch_is_tried_again_with_the_next_state(
        self, monkeypatch
    ):
        """A device out of reach still needs its own sensor once it is back."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_for_fallback(mock_self, TRV)
        mock_self.hass.services.async_call.side_effect = HomeAssistantError("asleep")
        await _change(mock_self, _bt_state("optimal"), _bt_state("sensor_fallback"))
        assert TRV in quirk._HAND_BACK_DUE
        mock_self.hass.services.async_call.side_effect = None

        await _change(
            mock_self, _bt_state("sensor_fallback"), _bt_state("sensor_fallback")
        )

        assert _payloads(mock_self) == [_to_internal(), _to_internal()]
        assert TRV not in quirk._HAND_BACK_DUE

    async def test_a_selector_that_is_not_reporting_is_tried_again(self, monkeypatch):
        """An unavailable selector is a device out of reach, not a missing one."""
        mock_self = _make_self(monkeypatch, selected="unavailable")
        quirk._watch_for_fallback(mock_self, TRV)

        await _change(mock_self, _bt_state("optimal"), _bt_state("sensor_fallback"))

        assert _payloads(mock_self) == []
        assert TRV in quirk._HAND_BACK_DUE

    async def test_the_room_reading_back_drops_a_due_switch(self, monkeypatch):
        """Back on optimal, the device belongs on the input again."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_for_fallback(mock_self, TRV)
        quirk._HAND_BACK_DUE.add(TRV)

        await _change(mock_self, _bt_state("sensor_fallback"), _bt_state("optimal"))

        assert _payloads(mock_self) == []
        assert TRV not in quirk._HAND_BACK_DUE

    async def test_a_device_without_a_selector_is_not_written(self, monkeypatch):
        """Nothing on the device chooses the sensor, so there is nothing to retry."""
        mock_self = _make_self(
            monkeypatch,
            entries=[
                _entry(TRV),
                _entry(INPUT, translation_key="external_temperature_input"),
            ],
        )
        quirk._watch_for_fallback(mock_self, TRV)

        await _change(mock_self, _bt_state("optimal"), _bt_state("sensor_fallback"))

        assert _payloads(mock_self) == []
        assert TRV not in quirk._HAND_BACK_DUE

    async def test_a_removed_thermostat_stops_watching(self, monkeypatch):
        """The listener outliving its thermostat writes to no device."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_for_fallback(mock_self, TRV)
        mock_self.is_removed = True

        await _change(mock_self, _bt_state("optimal"), _bt_state("sensor_fallback"))

        assert _payloads(mock_self) == []
        assert mock_self.unsubscribed == mock_self.listeners
        assert TRV not in quirk._FALLBACK_WATCHES

    async def test_the_initial_tweak_catches_a_fallback_already_under_way(
        self, monkeypatch
    ):
        """The change into the fallback may be written before the watch exists."""
        mock_self = _make_self(monkeypatch, selected="external")
        mock_self.states[BT] = _bt_state("sensor_fallback")
        default_tweak = AsyncMock()
        monkeypatch.setattr(quirk, "default_initial_tweak", default_tweak)

        await quirk.initial_tweak(mock_self, TRV)
        await asyncio.sleep(0)

        default_tweak.assert_awaited_once_with(mock_self, TRV)
        assert _payloads(mock_self) == [_to_internal()]

    async def test_the_initial_tweak_leaves_a_thermostat_with_a_reading(
        self, monkeypatch
    ):
        """A room sensor that reports keeps the device on the input."""
        mock_self = _make_self(monkeypatch, selected="external")
        mock_self.states[BT] = _bt_state("optimal")
        monkeypatch.setattr(quirk, "default_initial_tweak", AsyncMock())

        await quirk.initial_tweak(mock_self, TRV)
        await asyncio.sleep(0)

        assert _payloads(mock_self) == []
        assert TRV in quirk._FALLBACK_WATCHES

    async def test_the_next_write_puts_the_device_back_on_the_input(self, monkeypatch):
        """Once the room sensor reports again, the device reads the input again."""
        mock_self = _make_self(monkeypatch, selected="internal")
        quirk._HAND_BACK_DUE.add(TRV)

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 21.0)

        assert _payloads(mock_self) == [
            ("select", "select_option", {"entity_id": SELECTOR, "option": "external"}),
            ("number", "set_value", {"entity_id": INPUT, "value": 21.0}),
        ]
        assert TRV not in quirk._HAND_BACK_DUE
