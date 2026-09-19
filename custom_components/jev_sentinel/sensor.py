"""Minimal diagnostic sensors for Sentinel status."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([SentinelStatusSensor(entry)], True)


class SentinelStatusSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Status"
    _attr_unique_id = "jev_sentinel_status"

    def __init__(self, entry: ConfigEntry) -> None:
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}, "name": "Jev Home Assistant Sentinel", "manufacturer": "Community"}
        self._attr_native_value = "ready"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.hass.bus.async_listen("jev_sentinel_decision", self._decision_received))
        self.async_on_remove(self.hass.bus.async_listen("jev_sentinel_verification", self._verification_received))

    async def _decision_received(self, event) -> None:
        self._attr_native_value = event.data.get("decision", {}).get("outcome", "unknown")
        self._attr_extra_state_attributes = {"event": "decision", "action": event.data.get("decision", {}).get("action")}
        self.async_write_ha_state()

    async def _verification_received(self, event) -> None:
        self._attr_native_value = event.data.get("status", "unknown")
        self._attr_extra_state_attributes = {"event": "verification", "verified": event.data.get("verified", False), "next_step": event.data.get("next_step")}
        self.async_write_ha_state()
