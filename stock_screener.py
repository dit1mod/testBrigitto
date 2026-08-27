"""
Stock Screener + Telegram Notifier (con analisi AI)
-----------------------------------------------------
Controlla i titoli USA in maggior calo della giornata (non una lista fissa,
ma i "Top Losers" di tutto il mercato secondo Alpha Vantage), calcola alcuni
indicatori tecnici (RSI, SMA50, SMA200) e chiede a Claude di scrivere un
breve commento in linguaggio naturale su ciascun titolo interessante.
Infine invia tutto su Telegram.

ATTENZIONE: questo NON e' un consiglio di investimento. E' un filtro tecnico
automatico basato su regole semplici + un commento generato da un modello AI,
che puo' sbagliare o essere impreciso. Ogni segnalazione va sempre verificata
con un'analisi propria prima di qualsiasi decisione.

Limiti del piano gratuito Alpha Vantage: 25 richieste/giorno, 5/minuto.
- 1 chiamata per ottenere i top losers di tutto il mercato
- 3 chiamate extra (RSI, SMA50, SMA200) per ogni titolo che supera la soglia
Il budget sotto e' dimensionato per restare nel limite anche in giornate
di forte ribasso diffuso.
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

# Chiave per l'analisi AI gratuita con Google Gemini (opzionale). Se lasciata
# vuota, lo script funziona comunque ma senza il commento scritto dall'AI.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"

# Chiave alternativa per l'analisi AI a pagamento con Claude (opzionale,
# usata solo se GEMINI_API_KEY non e' impostata).
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = "claude-sonnet-4-6"

# Soglia di calo giornaliero per far scattare l'analisi approfondita (% assoluta)
CALO_MINIMO_PCT = 4.5

# Soglia RSI sotto la quale consideriamo il titolo "ipervenduto"
RSI_IPERVENDUTO = 40

# Quanti titoli al massimo analizzare in profondita' (RSI+SMA), per restare
# nel budget di chiamate API anche nei giorni di forte ribasso diffuso.
MAX_TITOLI_ANALISI_APPROFONDITA = 6

ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co/query"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{{model}}:generateContent"

# Budget giornaliero di chiamate Alpha Vantage (il piano free ne concede 25/giorno).
BUDGET_CHIAMATE = 24
_chiamate_effettuate = 0


def _consuma_budget():
    global _chiamate_effettuate
    if _chiamate_effettuate >= BUDGET_CHIAMATE:
        return False
    _chiamate_effettuate += 1
    return True


# ---------------------------------------------------------------------------
# FUNZIONI DATI DI MERCATO
# ---------------------------------------------------------------------------

def get_top_losers():
    """Ritorna la lista dei titoli USA in maggior calo oggi (1 sola chiamata API)."""
    if not _consuma_budget():
        return []
    params = {
        "function": "TOP_GAINERS_LOSERS",
        "apikey": ALPHA_VANTAGE_API_KEY,
    }
    r = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=15)
    data = r.json().get("top_losers", [])
    risultati = []
    for item in data:
        try:
            risultati.append({
                "symbol": item["ticker"],
                "price": float(item["price"]),
                "change_pct": -abs(float(item["change_percentage"].replace("%", ""))),
                "volume": int(item["volume"]),
            })
        except (KeyError, ValueError):
            continue
    return risultati


def get_rsi(symbol, interval="daily", time_period=14):
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
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    r = requests.post(url, data=payload, timeout=15)
    return r.ok


# ---------------------------------------------------------------------------
# ANALISI AI (opzionale)
# ---------------------------------------------------------------------------

def _prompt_commento(risultati):
    righe_dati = []
    for r in risultati:
        rsi_txt = f"{r['rsi']:.1f}" if r["rsi"] is not None else "N/D"
        righe_dati.append(
            f"- {r['symbol']}: prezzo {r['price']:.2f}, variazione oggi {r['change_pct']:.2f}%, "
            f"RSI {rsi_txt}, "
            f"trend vs SMA200 {'positivo' if r['trend_positivo'] else 'negativo'}, "
            f"sopra SMA50: {'si' if r['sopra_sma50'] else 'no'}"
        )
    dati_testuali = "\n".join(righe_dati)

    return (
        "Sei un analista finanziario che scrive per un investitore retail italiano "
        "con esperienza intermedia. Ti fornisco un elenco di titoli USA in forte calo "
        "oggi con alcuni indicatori tecnici. Per ciascun titolo scrivi 1-2 frasi brevi "
        "in italiano su cosa potrebbe significare il quadro tecnico (es. ipervenduto "
        "in trend di fondo positivo = possibile rimbalzo da valutare; calo con trend "
        "gia' negativo = maggiore cautela). Non dare consigli di acquisto diretti, "
        "descrivi solo la situazione tecnica in modo chiaro. Massimo 300 parole totali.\n\n"
        f"Titoli da commentare:\n{dati_testuali}"
    )


def _commento_gemini(prompt):
    url = GEMINI_API_URL.format(model=GEMINI_MODEL)
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        r = requests.post(url, params={"key": GEMINI_API_KEY}, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"Gemini non disponibile: {e}")
        return None


def _commento_claude(prompt):
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 700,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        r = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        testo = "".join(
            blocco.get("text", "") for blocco in data.get("content", [])
            if blocco.get("type") == "text"
        )
        return testo.strip() or None
    except Exception as e:
        print(f"Claude non disponibile: {e}")
        return None


def genera_commento_ai(risultati):
    """Prova prima Gemini (gratis), poi Claude come riserva se configurato.
    Ritorna None se nessuna delle due chiavi e' impostata o entrambe falliscono."""
    if not risultati:
        return None

    prompt = _prompt_commento(risultati)

    if GEMINI_API_KEY:
        commento = _commento_gemini(prompt)
        if commento:
            return commento

    if ANTHROPIC_API_KEY:
        commento = _commento_claude(prompt)
        if commento:
            return commento

    return None


# ---------------------------------------------------------------------------
# LOGICA PRINCIPALE
# ---------------------------------------------------------------------------

def analizza_titolo(base):
    """Arricchisce un titolo (gia' ottenuto da get_top_losers) con RSI e SMA."""
    symbol = base["symbol"]

    time.sleep(13)
    rsi = get_rsi(symbol)

    time.sleep(13)
    sma50 = get_sma(symbol, 50)

    time.sleep(13)
    sma200 = get_sma(symbol, 200)

    trend_positivo = sma200 is not None and base["price"] > sma200
    ipervenduto = rsi is not None and rsi < RSI_IPERVENDUTO
    sopra_sma50 = sma50 is not None and base["price"] > sma50
    punteggio_interesse = sum([trend_positivo, ipervenduto])

    return {
        **base,
        "rsi": rsi,
        "sma50": sma50,
        "sma200": sma200,
        "trend_positivo": trend_positivo,
        "ipervenduto": ipervenduto,
        "sopra_sma50": sopra_sma50,
        "punteggio_interesse": punteggio_interesse,
    }


def formatta_messaggio(risultati, commento_ai):
    if not risultati:
        return None

    righe = ["📉 <b>Titoli USA in forte calo oggi</b>\n"]
    for r in risultati:
        stelle = "⭐" * r["punteggio_interesse"] if r["punteggio_interesse"] > 0 else "–"
        rsi_txt = f"{r['rsi']:.1f}" if r["rsi"] is not None else "N/D"
        righe.append(
            f"\n<b>{r['symbol']}</b>  {r['change_pct']:.2f}%\n"
            f"Prezzo: {r['price']:.2f}\n"
            f"RSI: {rsi_txt} {'(ipervenduto)' if r['ipervenduto'] else ''}\n"
            f"Trend (vs SMA200): {'positivo' if r['trend_positivo'] else 'negativo'}\n"
            f"Interesse: {stelle}\n"
        )

    if commento_ai:
        righe.append(f"\n🤖 <b>Commento AI</b>\n{commento_ai}\n")

    righe.append(
        "\n⚠️ Analisi tecnica automatica (in parte generata da AI), non è un "
        "consiglio di investimento. Verifica sempre con analisi tua prima di decidere."
    )
    return "".join(righe)


def main():
    top_losers = get_top_losers()
    if not top_losers:
        send_telegram_message(
            "📊 Non è stato possibile recuperare i dati di mercato oggi "
            "(o nessun titolo disponibile)."
        )
        return

    # filtra solo chi supera la soglia di calo minima
    candidati = [t for t in top_losers if t["change_pct"] <= -CALO_MINIMO_PCT]

    # ordina dal calo piu' forte e taglia al budget disponibile
    candidati.sort(key=lambda t: t["change_pct"])
    candidati = candidati[:MAX_TITOLI_ANALISI_APPROFONDITA]

    risultati = []
    for base in candidati:
        print(f"Analizzo {base['symbol']}...")
        risultati.append(analizza_titolo(base))

    risultati.sort(key=lambda r: r["punteggio_interesse"], reverse=True)

    commento_ai = genera_commento_ai(risultati)
    messaggio = formatta_messaggio(risultati, commento_ai)

    if messaggio:
        send_telegram_message(messaggio)
        print("Notifica inviata.")
    else:
        send_telegram_message(
            f"📊 Nessun titolo USA ha superato oggi la soglia di calo del "
            f"{CALO_MINIMO_PCT}%."
        )
        print("Nessun titolo idoneo, inviato messaggio di stato.")


if __name__ == "__main__":
    main()
