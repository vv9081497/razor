"""tg_bot.py — minimal stub. Notifications handled inline by rzp_api.py."""
import sys


def notify_charge(card_line, gateway, message, amount_inr, site):
    pass


def increment_stat(key):
    pass


def get_stats():
    return {"waf_403": 0, "rzp_5xx": 0, "gateway_error": 0, "total_checkouts": 0}


async def send_admin_alert(msg):
    pass
