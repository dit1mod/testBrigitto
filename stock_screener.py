"""
Stock Screener + Telegram Notifier
-----------------------------------
Controlla una lista di titoli USA/Europa, individua quelli in calo giornaliero
sopra una certa soglia, calcola alcuni indicatori tecnici (RSI, SMA50, SMA200,
volume) e invia una notifica su Telegram per quelli che sembrano interessanti
da approfondire.

ATTENZIONE: questo NON e' un consiglio di investimento. E' un filtro tecnico
automatico basato su regole semplici. Ogni segnalazione va sempre verificata
con un'analisi propria prima di qualsiasi decisione.

Limiti del piano gratuito Alpha Vantage: 25 richieste/giorno, 5/minuto.
Ogni titolo richiede 2 chiamate (quotazione + RSI), quindi con 25 richieste
si possono controllare al massimo ~12 titoli al giorno. La lista WATCHLIST
sotto e' gia' dimensionata per stare nel limite.
"""

import os
import time
import requests

# ---------------------------------------------------------------------------
# CONFIGURAZIONE - modifica questi valori
# ---------------------------------------------------------------------------

ALPHA_VANTAGE_API_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY", "INSERISCI_QUI_LA_TUA_CHIAVE")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8792722517:AAEw3fGPBeNqtfEkwIYrD3Q-guw92jAUv7k")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "527421998")

# Lista titoli da monitorare (USA + Europa). Max ~12 per stare nel limite free.
WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",   # USA
    "SAP.DEX", "ASML.AS", "OR.PAR",            # Europa (SAP, ASML, L'Oreal)
    "ENI.MIL", "ENEL.MIL",                     # Italia
]

# Soglia di calo giornaliero per far scattare l'analisi (in percentuale, valore assoluto)
CALO_MINIMO_PCT = 4.5

# Soglia RSI sotto la quale consideriamo il titolo "ipervenduto"
RSI_IPERVENDUTO = 40

ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co/query"
TELEGRAM_API_URL = f"https://api.telegram.org/bot{{token}}/sendMessage"

# Budget giornaliero di chiamate API (il piano free ne concede 25/giorno).
# Teniamo un margine di sicurezza di 1 chiamata.
BUDGET_CHIAMATE = 24
_chiamate_effettuate = 0


def _consuma_budget():
    """Ritorna True se c'e' ancora budget per una chiamata, False altrimenti."""
    global _chiamate_effettuate
    if _chiamate_effettuate >= BUDGET_CHIAMATE:
        return False
    _chiamate_effettuate += 1
    return True


# ---------------------------------------------------------------------------
# FUNZIONI DI SUPPORTO
# ---------------------------------------------------------------------------

def get_quote(symbol):
    """Recupera prezzo attuale, variazione % e volume per un titolo."""
    if not _consuma_budget():
        return None
    params = {
        "function": "GLOBAL_QUOTE",
        "symbol": symbol,
        "apikey": ALPHA_VANTAGE_API_KEY,
    }
    r = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=15)
    data = r.json().get("Global Quote", {})
    if not data:
        return None
    try:
        return {
            "symbol": symbol,
            "price": float(data["05. price"]),
            "change_pct": float(data["10. change percent"].replace("%", "")),
            "volume": int(data["06. volume"]),
        }
    except (KeyError, ValueError):
        return None


def get_rsi(symbol, interval="daily", time_period=14):
    """Recupera l'ultimo valore RSI disponibile."""
    if not _consuma_budget():
        return None
    params = {
        "function": "RSI",
        "symbol": symbol,
        "interval": interval,
        "time_period": time_period,
        "series_type": "close",
        "apikey": ALPHA_VANTAGE_API_KEY,
    }
    r = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=15)
    data = r.json().get("Technical Analysis: RSI", {})
    if not data:
        return None
    ultima_data = sorted(data.keys(), reverse=True)[0]
    return float(data[ultima_data]["RSI"])


def get_sma(symbol, time_period, interval="daily"):
    """Recupera l'ultima media mobile semplice disponibile."""
    if not _consuma_budget():
        return None
    params = {
        "function": "SMA",
        "symbol": symbol,
        "interval": interval,
        "time_period": time_period,
        "series_type": "close",
        "apikey": ALPHA_VANTAGE_API_KEY,
    }
    r = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=15)
    data = r.json().get("Technical Analysis: SMA", {})
    if not data:
        return None
    ultima_data = sorted(data.keys(), reverse=True)[0]
    return float(data[ultima_data]["SMA"])


def send_telegram_message(text):
    url = TELEGRAM_API_URL.format(token=TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    r = requests.post(url, data=payload, timeout=15)
    return r.ok


# ---------------------------------------------------------------------------
# LOGICA PRINCIPALE
# ---------------------------------------------------------------------------

def analizza_titolo(symbol):
    """Ritorna un dict con l'analisi completa del titolo, o None se non idoneo."""
    quote = get_quote(symbol)
    if not quote:
        return None

    # Filtro 1: deve essere in calo di almeno CALO_MINIMO_PCT
    if quote["change_pct"] > -CALO_MINIMO_PCT:
        return None

    time.sleep(13)  # rispetta il rate limit (5 chiamate/minuto = 1 ogni 12s)
    rsi = get_rsi(symbol)

    time.sleep(13)
    sma50 = get_sma(symbol, 50)

    time.sleep(13)
    sma200 = get_sma(symbol, 200)

    trend_positivo = sma200 is not None and quote["price"] > sma200
    ipervenduto = rsi is not None and rsi < RSI_IPERVENDUTO
    sopra_sma50 = sma50 is not None and quote["price"] > sma50

    punteggio_interesse = sum([trend_positivo, ipervenduto])

    return {
        **quote,
        "rsi": rsi,
        "sma50": sma50,
        "sma200": sma200,
        "trend_positivo": trend_positivo,
        "ipervenduto": ipervenduto,
        "sopra_sma50": sopra_sma50,
        "punteggio_interesse": punteggio_interesse,
    }


def formatta_messaggio(risultati):
    if not risultati:
        return None

    righe = ["📉 <b>Titoli in calo oggi</b>\n"]
    for r in risultati:
        stelle = "⭐" * r["punteggio_interesse"] if r["punteggio_interesse"] > 0 else "–"
        righe.append(
            f"\n<b>{r['symbol']}</b>  {r['change_pct']:.2f}%\n"
            f"Prezzo: {r['price']:.2f}\n"
            f"RSI: {r['rsi']:.1f} {'(ipervenduto)' if r['ipervenduto'] else ''}\n"
            f"Trend (vs SMA200): {'positivo' if r['trend_positivo'] else 'negativo'}\n"
            f"Interesse: {stelle}\n"
        )
    righe.append(
        "\n⚠️ Analisi tecnica automatica, non è un consiglio di investimento. "
        "Verifica sempre con analisi tua prima di decidere."
    )
    return "".join(righe)


def main():
    risultati = []
    for symbol in WATCHLIST:
        print(f"Controllo {symbol}...")
        analisi = analizza_titolo(symbol)
        if analisi:
            risultati.append(analisi)
        time.sleep(13)  # pausa tra un titolo e l'altro per il rate limit

    # ordina per punteggio di interesse decrescente
    risultati.sort(key=lambda r: r["punteggio_interesse"], reverse=True)

    messaggio = formatta_messaggio(risultati)
    if messaggio:
        send_telegram_message(messaggio)
        print("Notifica inviata.")
    else:
        send_telegram_message("📊 Nessun titolo della watchlist ha superato la soglia di calo oggi.")
        print("Nessun titolo idoneo, inviato messaggio di stato.")


if __name__ == "__main__":
    main()
