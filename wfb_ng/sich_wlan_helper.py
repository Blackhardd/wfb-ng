from . import call_and_check_rc


def set_txpower(wlan, value):
    return call_and_check_rc("iw", "dev", wlan, "set", "txpower", "fixed", str(value))

