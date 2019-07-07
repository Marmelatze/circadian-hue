import logging
_LOGGER = logging.getLogger(__name__)

DOMAIN = 'circadian_hue'

def setup(hass, base_config):
    """Set up for Vera devices."""
    _LOGGER.info("starting hue circadian")


    return True