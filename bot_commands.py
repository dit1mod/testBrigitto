"""
Bot Comandi Telegram (polling via GitHub Actions)
----------------------------------------------------
Questo script viene lanciato ogni 10 minuti da GitHub Actions. Ad ogni
esecuzione controlla se hai scritto un nuovo comando al bot dall'ultima
volta, e se sì lo esegue:

  /analizza  -> scansiona i top losers USA e manda l'analisi (come la
                notifica giornaliera, ma a comando)
  /mercato   -> dice se i mercati USA/Europa sono aperti o chiusi ora
  /help      -> elenco comandi disponibili

Per "ricordarsi" quali messaggi ha gia' letto, lo script salva un piccolo
file (last_update_id.txt) nel repository stesso, che il workflow si
occupa di ricommittare ad ogni esecuzione.

ATTENZIONE: questo NON e' un consiglio di investimento.
"""

import os
import time
import requests

ALPHA_VANTAGE_API_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{{model}}:generateContent"

ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co/query"
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"

CALO_MINIMO_PCT = 4.5
RSI_IPERVENDUTO = 40
MAX_TITOLI_ANALISI_APPROFONDITA = 6

STATO_FILE = "last_update_id.txt"

# Regioni di mercato da mostrare nel comando /mercato
REGIONI_INTERESSE = ["United States", "Germany", "France", "United Kingdom"]

BUDGET_CHIAMATE = 24
_chiamate_effettuate = 0


def _consuma_budget():
    global _chiamate_effettuate
    if _chiamate_effettuate >= BUDGET_CHIAMATE:
        return False
    _chiamate_effettuate += 1
    return True


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------

def leggi_ultimo_update_id():
    if os.path.exists(STATO_FILE):
        with open(STATO_FILE, "r") as f:
            contenuto = f.read().strip()
            return int(contenuto) if contenuto else None
    return None


def salva_ultimo_update_id(update_id):
    with open(STATO_FILE, "w") as f:
        f.write(str(update_id))


def get_nuovi_messaggi(offset):
    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN) + "/getUpdates"
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset + 1
    r = requests.get(url, params=params, timeout=15)
    return r.json().get("result", [])


def invia_messaggio(testo):
    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN) + "/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": testo, "parse_mode": "HTML"}
    requests.post(url, data=payload, timeout=15)


# ---------------------------------------------------------------------------
# COMANDO: /mercato
# ---------------------------------------------------------------------------

def comando_mercato():
    if not _consuma_budget():
        invia_messaggio("⚠️ Budget chiamate API esaurito per oggi, riprova domani.")
        return

    params = {"function": "MARKET_STATUS", "apikey": ALPHA_VANTAGE_API_KEY}
    r = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=15)
    mercati = r.json().get("markets", [])

    if not mercati:
        invia_messaggio("⚠️ Non riesco a recuperare lo stato dei mercati in questo momento.")
        return

    righe = ["🕒 <b>Stato dei mercati</b>\n"]
    for m in mercati:
        if m.get("region") in REGIONI_INTERESSE:
            stato = m.get("current_status", "sconosciuto")
            emoji = "🟢" if stato == "open" else "🔴"
            righe.append(
                f"{emoji} {m.get('region')}: {'APERTO' if stato == 'open' else 'CHIUSO'} "
                f"(orario locale {m.get('local_open')} - {m.get('local_close')})"
            )

    invia_messaggio("\n".join(righe))


# ---------------------------------------------------------------------------
# COMANDO: /analizza  (stessa logica dello screener giornaliero)
# ---------------------------------------------------------------------------

def get_top_losers():
    if not _consuma_budget():
        return []
    params = {"function": "TOP_GAINERS_LOSERS", "apikey": ALPHA_VANTAGE_API_KEY}
    r = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=15)
    data = r.json().get("top_losers", [])
    risultati = []
    for item in data:
        try:
            risultati.append({
                "symbol": item["ticker"],
                "price": float(item["price"]),
                "change_pct": -abs(float(item["change_percentage"].replace("%", ""))),
            })
        except (KeyError, ValueError):
            continue
    return risultati


def get_rsi(symbol):
    if not _consuma_budget():
        return None
    params = {
        "function": "RSI", "symbol": symbol, "interval": "daily",
        "time_period": 14, "series_type": "close", "apikey": ALPHA_VANTAGE_API_KEY,
    }
    r = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=15)
    data = r.json().get("Technical Analysis: RSI", {})
    if not data:
        return None
    ultima = sorted(data.keys(), reverse=True)[0]
    return float(data[ultima]["RSI"])


def get_sma(symbol, time_period):
    if not _consuma_budget():
        return None
    params = {
        "function": "SMA", "symbol": symbol, "interval": "daily",
        "time_period": time_period, "series_type": "close", "apikey": ALPHA_VANTAGE_API_KEY,
    }
    r = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=15)
    data = r.json().get("Technical Analysis: SMA", {})
    if not data:
        return None
    ultima = sorted(data.keys(), reverse=True)[0]
    return float(data[ultima]["SMA"])


def genera_commento_ai(risultati):
    """Chiede a Gemini (gratis) un breve commento in italiano. Ritorna None
    se la chiave non e' configurata o la chiamata fallisce."""
    if not GEMINI_API_KEY or not risultati:
        return None

    righe_dati = []
    for r in risultati:
        righe_dati.append(
            f"- {r['symbol']}: prezzo {r['price']:.2f}, variazione oggi {r['change_pct']:.2f}%, "
            f"RSI {r['rsi_txt']}, trend vs SMA200 {r['trend_txt']}"
        )
    dati_testuali = "\n".join(righe_dati)

    prompt = (
        "Sei un analista finanziario che scrive per un investitore retail italiano "
        "con esperienza intermedia. Ti fornisco un elenco di titoli USA in forte calo "
        "oggi con alcuni indicatori tecnici. Per ciascun titolo scrivi 1-2 frasi brevi "
        "in italiano su cosa potrebbe significare il quadro tecnico. Non dare consigli "
        "di acquisto diretti, descrivi solo la situazione tecnica. Massimo 250 parole.\n\n"
        f"Titoli da commentare:\n{dati_testuali}"
    )

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


def comando_analizza():
    invia_messaggio("🔎 Analisi in corso, richiede qualche minuto...")

    top_losers = get_top_losers()
    if not top_losers:
        invia_messaggio("⚠️ Non riesco a recuperare i dati di mercato ora (mercato chiuso o budget esaurito).")
        return

    candidati = [t for t in top_losers if t["change_pct"] <= -CALO_MINIMO_PCT]
    candidati.sort(key=lambda t: t["change_pct"])
    candidati = candidati[:MAX_TITOLI_ANALISI_APPROFONDITA]

    if not candidati:
        invia_messaggio(f"📊 Nessun titolo USA ha superato oggi la soglia di calo del {CALO_MINIMO_PCT}%.")
        return

    righe = ["📉 <b>Analisi a comando</b>\n"]
    risultati_completi = []
    for base in candidati:
        time.sleep(13)
        rsi = get_rsi(base["symbol"])
        time.sleep(13)
        sma200 = get_sma(base["symbol"], 200)

        trend_positivo = sma200 is not None and base["price"] > sma200
        ipervenduto = rsi is not None and rsi < RSI_IPERVENDUTO
        stelle = "⭐" * (int(trend_positivo) + int(ipervenduto))
        rsi_txt = f"{rsi:.1f}" if rsi is not None else "N/D"
        trend_txt = "positivo" if trend_positivo else "negativo"

        righe.append(
            f"\n<b>{base['symbol']}</b>  {base['change_pct']:.2f}%\n"
            f"Prezzo: {base['price']:.2f}\n"
            f"RSI: {rsi_txt}\n"
            f"Trend: {trend_txt}\n"
            f"Interesse: {stelle if stelle else '–'}\n"
        )
        risultati_completi.append({**base, "rsi": rsi, "rsi_txt": rsi_txt, "trend_txt": trend_txt})

    commento_ai = genera_commento_ai(risultati_completi)
    if commento_ai:
        righe.append(f"\n🤖 <b>Commento AI</b>\n{commento_ai}\n")

    righe.append("\n⚠️ Non è un consiglio di investimento.")
    invia_messaggio("\n".join(righe))


# ---------------------------------------------------------------------------
# COMANDO: /help
# ---------------------------------------------------------------------------

def comando_help():
    invia_messaggio(
        "🤖 <b>Comandi disponibili</b>\n\n"
        "/analizza - controlla ora i titoli USA in maggior calo\n"
        "/mercato - stato mercati USA/Europa (aperto o chiuso)\n"
        "/help - questo messaggio\n\n"
        "Nota: i comandi vengono controllati ogni 10 minuti circa, "
        "non sono istantanei."
    )


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    if not (ALPHA_VANTAGE_API_KEY and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("Mancano una o più variabili d'ambiente richieste.")
        return

    offset = leggi_ultimo_update_id()
    messaggi = get_nuovi_messaggi(offset)

    if not messaggi:
        print("Nessun nuovo comando.")
        return

    ultimo_id = offset
    for msg in messaggi:
        ultimo_id = msg["update_id"]
        testo = msg.get("message", {}).get("text", "").strip().lower()

        if testo == "/analizza":
            comando_analizza()
        elif testo == "/mercato":
            comando_mercato()
        elif testo in ("/help", "/start"):
            comando_help()
        # comandi sconosciuti: ignorati silenziosamente

    salva_ultimo_update_id(ultimo_id)


if __name__ == "__main__":
    main()
