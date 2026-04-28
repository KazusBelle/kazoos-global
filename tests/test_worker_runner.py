from app.alerts import display_alert_symbol, format_alert_batch


def test_display_symbol_strips_usdt_suffix():
    assert display_alert_symbol("ORDIUSDT") == "ORDI"
    assert display_alert_symbol("AAVEUSDT") == "AAVE"
    assert display_alert_symbol("BTCUSD_PERP") == "BTCUSD_PERP"


def test_format_alert_batch_for_local_timeframe():
    text = format_alert_batch(["ORDIUSDT", "AAVEUSDT", "SOLUSDT"], "H1")
    assert text == "ORDI, AAVE, SOL — joins OTE (local)"


def test_format_alert_batch_for_global_timeframe():
    text = format_alert_batch(["BTCUSDT"], "D1")
    assert text == "BTC — joins OTE (global)"
