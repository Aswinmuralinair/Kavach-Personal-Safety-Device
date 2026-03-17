"""
hardware/whatsapp.py — Project Kavach

WhatsApp messaging via CallMeBot API (free, no SDK needed).

Setup (one-time):
  1. Save +34 644 51 95 23 in your phone contacts as "CallMeBot"
  2. Send "I allow callmebot to send me messages" to that number on WhatsApp
  3. You'll receive an API key — put it in config.json as "whatsapp_apikey"
  4. Put your WhatsApp number (with country code) as "whatsapp_number"

Usage:
    from hardware.whatsapp import send_whatsapp
    send_whatsapp("+919876543210", "YOUR_API_KEY", "Hello from Kavach!")
"""

import logging
import urllib.parse

logger = logging.getLogger(__name__)


def send_whatsapp(phone: str, apikey: str, message: str) -> bool:
    """
    Send a WhatsApp message via CallMeBot API.

    Args:
        phone:   Recipient phone number with country code (e.g. "+919876543210")
        apikey:  CallMeBot API key (received during setup)
        message: Text message to send (emojis and newlines supported)

    Returns:
        True on success, False on failure (logged).
    """
    import requests

    encoded_msg = urllib.parse.quote(message)
    url = (
        f"https://api.callmebot.com/whatsapp.php"
        f"?phone={phone}&apikey={apikey}&text={encoded_msg}"
    )

    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            logger.info("[WhatsApp] Message sent to %s.", phone)
            return True
        else:
            logger.warning(
                "[WhatsApp] API returned %d for %s: %s",
                r.status_code, phone, r.text[:100]
            )
            return False
    except Exception as e:
        logger.error("[WhatsApp] Send failed: %s", e)
        return False
