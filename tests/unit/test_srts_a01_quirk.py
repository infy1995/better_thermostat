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
ROOM_SENSOR = "sensor.living_room_temperature"


def _entry(entity_id, *, translation_key=None, device_id="dev1"):
    """A registry entry on the TRV's device; its domain is the entity id's."""
    return make_registry_entry(
        entity_id, translation_key=translation_key, device_id=device_id
    )


def _room(value, unit="°C"):
    """A state of the room sensor."""
    return State(ROOM_SENSOR, value, {"unit_of_measurement": unit})


def _make_self(
    monkeypatch,
    *,
    selected="internal",
    model="SRTS-A01",
    entries=None,
    room="21.0",
    trvs=(TRV,),
):
    """A BT stand-in whose SRTS-A01 carries the input and the sensor selector.

    ``selected`` is what the selector reports; ``None`` stands for a selector
    that publishes no state. ``room`` is what the room sensor reports, and
    ``trvs`` the TRVs the thermostat's config entry lists.
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

    bt_entry = MagicMock()
    bt_entry.data = {"name": "Living room"}
    bt_entry.options = {
        "temperature_sensor": ROOM_SENSOR,
        "thermostat": [{"trv": trv} for trv in trvs],
    }
    entries_by_domain = {"better_thermostat": [bt_entry]}
    mock_self.hass.config_entries.async_entries = lambda domain: entries_by_domain.get(
        domain, []
    )

    states = {
        INPUT: State(INPUT, "20.0", {"min": 0, "max": 55, "unit_of_measurement": "°C"}),
        ROOM_SENSOR: _room(room),
    }
    if selected is not None:
        states[SELECTOR] = State(
            SELECTOR, selected, {"options": ["internal", "external"]}
        )
    mock_self.hass.states.get = states.get
    mock_self.states = states

    async def _call(domain, service, data, **kwargs):
        # Home Assistant moves the selector once the device has taken the
        # option; the watch then sees that change like any other.
        if (domain, service) == ("select", "select_option"):
            states[SELECTOR] = State(
                SELECTOR, data["option"], {"options": ["internal", "external"]}
            )

    mock_self.hass.services.async_call = AsyncMock(side_effect=_call)

    listeners: list[object] = []
    watched: list[object] = []
    unsubscribed: list[object] = []
    mock_self.listeners = listeners
    mock_self.watched = watched
    mock_self.unsubscribed = unsubscribed

    def _track(hass, entity_ids, listener):
        watched.append(list(entity_ids))
        listeners.append(listener)
        return MagicMock(side_effect=lambda: unsubscribed.append(listener))

    monkeypatch.setattr(quirk, "async_track_state_change_event", _track)
    return mock_self


def _payloads(mock_self):
    """Every (domain, service, data) the quirk dispatched, in order."""
    return [
        (call.args[0], call.args[1], call.args[2])
        for call in mock_self.hass.services.async_call.await_args_list
    ]


def _event(new_state):
    """A state change of a watched entity."""
    return Event(
        EVENT_STATE_CHANGED,
        EventStateChangedData(
            entity_id=new_state.entity_id, old_state=None, new_state=new_state
        ),
    )


async def _settle():
    """Let the work a change started run to its end."""
    for _ in range(10):
        await asyncio.sleep(0)


async def _change(mock_self, new_state):
    """Put ``new_state`` in place and deliver its change to every listener."""
    mock_self.states[new_state.entity_id] = new_state
    for listener in list(mock_self.listeners):
        listener(_event(new_state))
    await _settle()


def _selector(option):
    """A state of the sensor selector."""
    return State(SELECTOR, option, {"options": ["internal", "external"]})


def _to_internal():
    """The call that puts the TRV on its own sensor."""
    return ("select", "select_option", {"entity_id": SELECTOR, "option": "internal"})


def _to_external():
    """The call that puts the TRV on the input BT writes."""
    return ("select", "select_option", {"entity_id": SELECTOR, "option": "external"})


def _write(value):
    """The call that writes ``value`` into the input."""
    return ("number", "set_value", {"entity_id": INPUT, "value": value})


@pytest.fixture(autouse=True)
def _no_watch_outlives_its_test():
    """Forget every selector watch a test registered."""
    yield
    quirk._SELECTOR_WATCHES.clear()
    quirk._KEEPING.clear()
    quirk._KEEP_AGAIN.clear()
    quirk._SELECTOR_LOCKS.clear()
    quirk._PENDING_SWITCHES.clear()


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
        assert TRV not in quirk._SELECTOR_WATCHES

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

    async def test_a_write_while_the_room_sensor_is_gone_keeps_the_own_sensor(
        self, monkeypatch
    ):
        """The selector follows the room sensor before any value is written."""
        mock_self = _make_self(monkeypatch, selected="external", room="unavailable")

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 21.0)

        assert _payloads(mock_self) == [_to_internal(), _write(21.0)]


class TestSelectorKeeping:
    """The selector follows the room sensor and is put back when changed."""

    async def test_the_room_sensor_and_the_selector_are_watched(self, monkeypatch):
        """The sensor is the one the thermostat listing the TRV is set up with."""
        mock_self = _make_self(monkeypatch, selected="external")

        quirk._watch_selector(mock_self, TRV)

        assert mock_self.watched == [[ROOM_SENSOR, SELECTOR]]

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
        quirk._watch_selector(old_self, TRV)
        new_self = _make_self(monkeypatch, selected="external")

        quirk._watch_selector(new_self, TRV)

        assert old_self.unsubscribed == old_self.listeners
        assert quirk._SELECTOR_WATCHES[TRV][0] is new_self

    async def test_a_trv_no_thermostat_lists_keeps_reading_the_input(self, monkeypatch):
        """Without a room sensor to follow, the selector stays on ``external``."""
        mock_self = _make_self(monkeypatch, selected="external", trvs=("climate.x",))
        quirk._watch_selector(mock_self, TRV)

        await _change(mock_self, _selector("internal"))

        assert mock_self.watched == [[SELECTOR]]
        assert _payloads(mock_self) == [_to_external()]

    @pytest.mark.parametrize(
        "gone",
        ["unavailable", "unknown", "not a number", "150.0", "-60.0"],
        ids=["unavailable", "unknown", "no-number", "too-hot", "too-cold"],
    )
    async def test_losing_the_reading_hands_the_device_its_own_sensor(
        self, monkeypatch, gone
    ):
        """Every state BT takes for no reading puts the TRV on its own sensor."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_selector(mock_self, TRV)

        await _change(mock_self, _room(gone))

        assert _payloads(mock_self) == [_to_internal()]

    async def test_the_reading_back_puts_the_device_on_the_input(self, monkeypatch):
        """Back on ``external``, the device is handed the reading right away."""
        mock_self = _make_self(monkeypatch, selected="internal", room="unavailable")
        quirk._watch_selector(mock_self, TRV)

        await _change(mock_self, _room("21.3"))

        assert _payloads(mock_self) == [_to_external(), _write(21.3)]

    async def test_a_reading_in_fahrenheit_is_a_reading(self, monkeypatch):
        """The plausibility check runs in Celsius, as BT's does."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_selector(mock_self, TRV)

        await _change(mock_self, _room("71.0", "°F"))

        assert _payloads(mock_self) == []

    async def test_a_selector_in_line_is_left_alone(self, monkeypatch):
        """Neither a new reading nor a lasting outage writes the selector again."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_selector(mock_self, TRV)

        await _change(mock_self, _room("21.4"))
        mock_self.states[SELECTOR] = _selector("internal")
        mock_self.states[ROOM_SENSOR] = _room("unavailable")
        await _change(mock_self, _room("unknown"))

        assert _payloads(mock_self) == []

    async def test_external_set_by_hand_during_an_outage_is_put_back(self, monkeypatch):
        """The device would read the stale input the room sensor no longer feeds."""
        mock_self = _make_self(monkeypatch, selected="internal", room="unavailable")
        quirk._watch_selector(mock_self, TRV)

        await _change(mock_self, _selector("external"))

        assert _payloads(mock_self) == [_to_internal()]
        assert mock_self.states[SELECTOR].state == "internal"

    async def test_internal_set_by_hand_with_a_reading_is_put_back(self, monkeypatch):
        """The device goes back to the room temperature, written with it."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_selector(mock_self, TRV)

        await _change(mock_self, _selector("internal"))

        assert _payloads(mock_self) == [_to_external(), _write(21.0)]

    async def test_changes_in_a_row_switch_the_device_once(self, monkeypatch):
        """Changes arriving while a switch runs are checked once it is done."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_selector(mock_self, TRV)
        mock_self.states[ROOM_SENSOR] = _room("unavailable")

        for listener in list(mock_self.listeners):
            listener(_event(_room("unavailable")))
            listener(_event(_room("unknown")))
        await _settle()

        assert _payloads(mock_self) == [_to_internal()]
        assert TRV not in quirk._KEEPING

    async def test_a_write_and_a_returning_sensor_switch_the_device_once(
        self, monkeypatch
    ):
        """Both arrive together, and the device confirms a switch seconds later.

        The selector keeps reporting ``internal`` until then, so each would
        otherwise send the switch on its own.
        """
        mock_self = _make_self(monkeypatch, selected="internal", room="unavailable")
        quirk._watch_selector(mock_self, TRV)
        mock_self.hass.services.async_call = AsyncMock()
        mock_self.states[ROOM_SENSOR] = _room("22.8")

        for listener in list(mock_self.listeners):
            listener(_event(_room("22.8")))
        await asyncio.gather(
            quirk.maybe_set_external_temperature(mock_self, TRV, 22.8), _settle()
        )
        await _settle()

        switches = [call for call in _payloads(mock_self) if call[0] == "select"]
        assert switches == [_to_external()]

    async def test_a_switch_the_device_never_confirms_is_sent_again(self, monkeypatch):
        """Past the wait for the confirmation, the switch counts as lost."""
        monkeypatch.setattr(quirk, "_SWITCH_CONFIRM_S", 0.0)
        mock_self = _make_self(monkeypatch, selected="external", room="unavailable")
        mock_self.hass.services.async_call = AsyncMock()

        await quirk._keep_selector(mock_self, TRV)
        await quirk._keep_selector(mock_self, TRV)

        assert _payloads(mock_self) == [_to_internal(), _to_internal()]

    async def test_a_confirmed_switch_is_no_longer_awaited(self, monkeypatch):
        """Once the selector reports the option, nothing waits for it."""
        mock_self = _make_self(monkeypatch, selected="external", room="unavailable")

        await quirk._keep_selector(mock_self, TRV)
        assert TRV in quirk._PENDING_SWITCHES
        await quirk._keep_selector(mock_self, TRV)

        assert TRV not in quirk._PENDING_SWITCHES
        assert _payloads(mock_self) == [_to_internal()]

    async def test_a_refused_switch_is_tried_again_with_the_next_change(
        self, monkeypatch
    ):
        """A device out of reach still needs its own sensor once it is back."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_selector(mock_self, TRV)
        answer = mock_self.hass.services.async_call.side_effect
        mock_self.hass.services.async_call.side_effect = HomeAssistantError("asleep")
        await _change(mock_self, _room("unavailable"))
        mock_self.hass.services.async_call.side_effect = answer

        await _change(mock_self, _room("unknown"))

        assert _payloads(mock_self) == [_to_internal(), _to_internal()]
        assert mock_self.states[SELECTOR].state == "internal"

    async def test_a_selector_coming_back_is_brought_in_line(self, monkeypatch):
        """An unavailable selector is out of reach; its return is a change too."""
        mock_self = _make_self(monkeypatch, selected="unavailable")
        quirk._watch_selector(mock_self, TRV)
        await _change(mock_self, _room("unavailable"))
        assert _payloads(mock_self) == []

        await _change(mock_self, _selector("external"))

        assert _payloads(mock_self) == [_to_internal()]

    async def test_a_device_without_a_selector_is_not_written(self, monkeypatch):
        """Nothing on the device chooses the sensor."""
        mock_self = _make_self(
            monkeypatch,
            entries=[
                _entry(TRV),
                _entry(INPUT, translation_key="external_temperature_input"),
            ],
        )
        quirk._watch_selector(mock_self, TRV)

        await _change(mock_self, _room("unavailable"))

        assert mock_self.watched == [[ROOM_SENSOR]]
        assert _payloads(mock_self) == []

    async def test_a_removed_thermostat_stops_watching(self, monkeypatch):
        """The listener outliving its thermostat writes to no device."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_selector(mock_self, TRV)
        mock_self.is_removed = True

        await _change(mock_self, _room("unavailable"))

        assert _payloads(mock_self) == []
        assert mock_self.unsubscribed == mock_self.listeners
        assert TRV not in quirk._SELECTOR_WATCHES

    async def test_the_initial_tweak_catches_a_sensor_already_gone(self, monkeypatch):
        """A sensor without a reading from startup on changes no state to watch."""
        mock_self = _make_self(monkeypatch, selected="external", room="unavailable")
        default_tweak = AsyncMock()
        monkeypatch.setattr(quirk, "default_initial_tweak", default_tweak)

        await quirk.initial_tweak(mock_self, TRV)

        default_tweak.assert_awaited_once_with(mock_self, TRV)
        assert _payloads(mock_self) == [_to_internal()]
        assert TRV in quirk._SELECTOR_WATCHES

    async def test_the_initial_tweak_puts_a_device_with_a_reading_on_the_input(
        self, monkeypatch
    ):
        """A device left on its own sensor goes back to the room temperature."""
        mock_self = _make_self(monkeypatch, selected="internal")
        monkeypatch.setattr(quirk, "default_initial_tweak", AsyncMock())

        await quirk.initial_tweak(mock_self, TRV)

        assert _payloads(mock_self) == [_to_external(), _write(21.0)]
