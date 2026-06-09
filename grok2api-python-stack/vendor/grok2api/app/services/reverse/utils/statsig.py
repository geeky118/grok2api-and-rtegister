"""
Statsig ID generator for reverse interfaces.
"""

import base64

from app.core.logger import logger


class StatsigGenerator:
    """Statsig ID generator for reverse interfaces."""

    @staticmethod
    def gen_id() -> str:
        """
        Generate Statsig ID.

        Returns:
            Base64 encoded ID.
        """
        # Grok's current web client computes this header through a browser-side
        # botoxSign(path, method) helper. When that helper cannot run, the
        # official client falls back to btoa("x0:" + error). The old e:TypeError
        # shape is rejected by app-chat anti-bot rules.
        logger.debug("Generating botox fallback Statsig ID")
        message = "x0:TypeError: Cannot read properties of undefined (reading 'childNodes')"
        return base64.b64encode(message.encode()).decode()


__all__ = ["StatsigGenerator"]
