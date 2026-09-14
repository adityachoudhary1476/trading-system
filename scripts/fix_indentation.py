import pathlib

path = pathlib.Path("src/trading_system/research/strategy_lab/interpreter.py")
content = path.read_text()

old = '''        elif name == "donchian_upper":
                    out[ind.key] = _donchian_upper(df["high"], int(p["window"]))
                elif name == "donchian_lower":
                    out[ind.key] = _donchian_lower(df["low"], int(p["window"]))
                elif name == "volume_sma":
                    out[ind.key] = _volume_sma(df["volume"], int(p["window"]))'''

new = '''        elif name == "donchian_upper":
            out[ind.key] = _donchian_upper(df["high"], int(p["window"]))
        elif name == "donchian_lower":
            out[ind.key] = _donchian_lower(df["low"], int(p["window"]))
        elif name == "volume_sma":
            out[ind.key] = _volume_sma(df["volume"], int(p["window"]))'''

assert old in content, "Pattern not found in file"
content = content.replace(old, new)
path.write_text(content)
print("Fixed indentation in interpreter.py")
