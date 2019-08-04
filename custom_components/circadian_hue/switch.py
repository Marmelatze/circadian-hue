import asyncio
import logging
import threading
from datetime import timedelta

import voluptuous as vol
import async_timeout

from homeassistant.components.switch import SwitchDevice
from homeassistant.const import CONF_PLATFORM, CONF_NAME, STATE_ON
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import slugify
import homeassistant.helpers.config_validation as cv

DEPENDENCIES = ["hue", "circadian_lighting"]
_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=15)

PLATFORM_SCHEMA = vol.Schema({
    vol.Required(CONF_PLATFORM): 'circadian_hue',
    vol.Optional(CONF_NAME, default="Circadian Hue"): cv.string,

})

def get_bridges(hass):
    from homeassistant.components import hue
    from homeassistant.components.hue.bridge import HueBridge

    return [
        entry
        for entry in hass.data[hue.DOMAIN].values()
        if isinstance(entry, HueBridge) and entry.api
    ]

def is_circadian_scene(group, scene):
    return set(group.lights) == set(scene.lights) and scene.name == "Circadian"


async def update_api(api):
    import aiohue

    try:
        with async_timeout.timeout(10):
            await api.update()
    except (asyncio.TimeoutError, aiohue.AiohueException) as err:
        _LOGGER.debug("Failed to fetch sensors: %s", err)
        return False
    return True


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Initialise Hue Bridge connection."""
    name = config.get(CONF_NAME)
    data = CircadianHueSwitch(hass, name)
    await data.async_update_info()
    async_add_entities([data], True)
    async_track_time_interval(hass, data.async_update_info, SCAN_INTERVAL)


class CircadianHueSwitch(SwitchDevice, RestoreEntity):
    def __init__(self, hass, name):
        """Initialize the data object."""
        self.hass = hass
        self.lock = threading.Lock()
        self._name = name
        self._entity_id = "switch." + slugify("{} {}".format('hue_circadian', name))
        self._state = None

    @property
    def entity_id(self):
        """Return the entity ID of the switch."""
        return self._entity_id

    @property
    def name(self):
        """Return the name of the device if any."""
        return self._name

    @property
    def is_on(self):
        """Return true if circadian lighting is on."""
        return self._state

    def turn_on(self, **kwargs):
        """Turn on circadian lighting."""
        self._state = True

    def turn_off(self, **kwargs):
        """Turn off circadian lighting."""
        self._state = False
        self.schedule_update_ha_state()

    async def async_update_info(self, now=None):
        """Get the bridge info."""
        locked = self.lock.acquire(False)
        if not locked:
            return
        try:
            bridges = get_bridges(self.hass)
            if not bridges:
                if now:
                    # periodic task
                    await asyncio.sleep(5)
                return
            await asyncio.wait(
                [self.update_bridge(bridge) for bridge in bridges], loop=self.hass.loop
            )
        finally:
            self.lock.release()

    def get_lightstate(self, lights, set_brightness=True):
        xy_color = self.hass.states.get('sensor.circadian_values').attributes['xy_color']
        percent = float(self.hass.states.get('sensor.circadian_values').state)
        if percent > 0:
            brightness = 255
        else:
            brightness = 255 * ((100 + percent) / 100)
        _LOGGER.info("set brightness to {}".format(brightness))
        out = dict()
        for light in lights:
            data = {
                key: value for key, value in {
                    'on': True,
                    'xy': xy_color if 'xy' in light.state else None,
                    'bri': int(brightness) if set_brightness else None,
                    'transitiontime': 20
                }.items() if value is not None
            }
            out[light.id] = data
        return out

    async def async_added_to_hass(self):
        """Call when entity about to be added to hass."""
        # If not None, we got an initial value.
        await super().async_added_to_hass()
        if self._state is not None:
            return

        state = await self.async_get_last_state()
        self._state = state and state.state == STATE_ON

    async def update_bridge(self, bridge):
        if self._state is not True:
            _LOGGER.debug(self._name + " off - not adjusting")
            return False
        await update_api(bridge.api.scenes)
        available = await update_api(bridge.api.lights)
        if not available:
            return
        scenes = bridge.api.scenes
        for scene_id in scenes:
            scene = bridge.api.scenes[scene_id]
            if scene.name != "Circadian":
                continue
            _LOGGER.info("Found circadian scene %s for lights %s", scene.id, scene.lights)
            """check if scene is currently active"""
            current_scene_state = await bridge.api.request("get", "scenes/{}".format(scene_id))
            is_current_scene = True
            brightness_changed = False
            for light_id, state in current_scene_state['lightstates'].items():
                current_state = bridge.api.lights[light_id].state
                if current_state['on'] != state['on']:
                    is_current_scene = False
                if abs(current_state['bri'] - state['bri']) > 5:
                    brightness_changed = True
                if 'xy' in current_state:
                    _LOGGER.info("values %s %s", abs(current_state['xy'][0] - state['xy'][0]), abs(current_state['xy'][1] - state['xy'][0]))
                if 'xy' in current_state and (abs(current_state['xy'][0] - state['xy'][0]) > 0.02 or abs(current_state['xy'][1] - state['xy'][0]) > 0.02):
                    is_current_scene = False
            lights = list(map(lambda id: bridge.api.lights[id], scene.lights))
            state = self.get_lightstate(lights)

            data = {
                key: value for key, value in {
                    'lightstates': state
                }.items() if value is not None
            }
            result = await bridge.api.request("put", "scenes/{}".format(scene_id), json=data)
            _LOGGER.info("Updated scene")

            if is_current_scene:
                _LOGGER.info("Circadian scene is currently active.")
                if brightness_changed:
                    _LOGGER.info("Brightness was changed manually")
                state = self.get_lightstate(lights, not brightness_changed)
                await asyncio.gather(*[bridge.api.lights[light].set_state(**light_state) for light, light_state in state.items()])


