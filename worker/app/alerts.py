def display_alert_symbol(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def format_alert_batch(symbols: list[str], timeframe: str) -> str:
    pretty_symbols = ", ".join(display_alert_symbol(symbol) for symbol in symbols)
    tf_label = "global" if timeframe == "D1" else "local"
    return f"{pretty_symbols} — joins OTE ({tf_label})"
