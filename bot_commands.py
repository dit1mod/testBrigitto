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
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co/query"
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"

CALO_MINIMO_PCT = 4.5
RSI_IPERVENDUTO = 40
PREZZO_MINIMO = 5.0
VOLUME_MINIMO = 500000
SCONTO_TARGET_MINIMO_PCT = 15.0
PUNTEGGIO_MINIMO_OCCASIONE = 2
MAX_TITOLI_ANALISI_APPROFONDITA = 7

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
    try:
        r = requests.get(url, params=params, timeout=15)
        return r.json().get("result", [])
    except Exception as e:
        print(f"Errore getUpdates: {e}")
        return []


def invia_messaggio(testo):
    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN) + "/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": testo, "parse_mode": "HTML"}
    try:
        requests.post(url, data=payload, timeout=15)
    except Exception as e:
        print(f"Errore invio messaggio: {e}")


# ---------------------------------------------------------------------------
# COMANDO: /mercato
# ---------------------------------------------------------------------------

def comando_mercato():
    if not _consuma_budget():
        invia_messaggio("⚠️ Budget chiamate API esaurito per oggi, riprova domani.")
        return

    params = {"function": "MARKET_STATUS", "apikey": ALPHA_VANTAGE_API_KEY}
    try:
        r = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=15)
        mercati = r.json().get("markets", [])
    except Exception:
        mercati = []

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
# COMANDO: /analizza (Screener)
# ---------------------------------------------------------------------------

def get_candidati_mercato():
    if not _consuma_budget():
        return []
    params = {"function": "TOP_GAINERS_LOSERS", "apikey": ALPHA_VANTAGE_API_KEY}
    try:
        r = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=15)
        data = r.json()
    except Exception:
        return []

    def _parse(lista, fonte):
        risultati = []
        for item in lista:
            try:
                risultati.append({
                    "symbol": item["ticker"],
                    "price": float(item["price"]),
                    "change_pct": -abs(float(item["change_percentage"].replace("%", ""))),
                    "volume": int(item["volume"]),
                    "fonte": fonte,
                })
            except (KeyError, ValueError):
                continue
        return risultati

    top_losers = _parse(data.get("top_losers", []), "top_loser")
    most_active = [
        t for t in _parse(data.get("most_actively_traded", []), "most_active")
        if t["change_pct"] < 0
    ]

    combinati = {t["symbol"]: t for t in most_active}
    combinati.update({t["symbol"]: t for t in top_losers})
    return list(combinati.values())


def supera_prefiltro(t):
    if t["price"] < PREZZO_MINIMO or t["volume"] < VOLUME_MINIMO:
        return False
    if t["fonte"] == "top_loser":
        return t["change_pct"] <= -CALO_MINIMO_PCT
    return True


def get_rsi(symbol):
    if not _consuma_budget():
        return None
    params = {
        "function": "RSI", "symbol": symbol, "interval": "daily",
        "time_period": 14, "series_type": "close", "apikey": ALPHA_VANTAGE_API_KEY,
    }
    try:
        r = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=15)
        data = r.json().get("Technical Analysis: RSI", {})
        if not data:
            return None
        ultima = sorted(data.keys(), reverse=True)[0]
        return float(data[ultima]["RSI"])
    except Exception:
        return None


def get_sma(symbol, time_period):
    if not _consuma_budget():
        return None
    params = {
        "function": "SMA", "symbol": symbol, "interval": "daily",
        "time_period": time_period, "series_type": "close", "apikey": ALPHA_VANTAGE_API_KEY,
    }
    try:
        r = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=15)
        data = r.json().get("Technical Analysis: SMA", {})
        if not data:
            return None
        ultima = sorted(data.keys(), reverse=True)[0]
        return float(data[ultima]["SMA"])
    except Exception:
        return None


def get_analyst_data(symbol):
    if not _consuma_budget():
        return None
    params = {"function": "OVERVIEW", "symbol": symbol, "apikey": ALPHA_VANTAGE_API_KEY}
    try:
        r = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=15)
        data = r.json()
        if not data or "AnalystTargetPrice" not in data:
            return None
        target_price = float(data.get("AnalystTargetPrice", 0) or 0)
        buy = int(data.get("AnalystRatingStrongBuy", 0) or 0) + int(data.get("AnalystRatingBuy", 0) or 0)
        hold = int(data.get("AnalystRatingHold", 0) or 0)
        sell = int(data.get("AnalystRatingSell", 0) or 0) + int(data.get("AnalystRatingStrongSell", 0) or 0)
        return {"target_price": target_price, "buy": buy, "hold": hold, "sell": sell}
    except Exception:
        return None


def genera_commento_ai(risultati):
    if not GEMINI_API_KEY or not risultati:
        return None

    righe_dati = []
    for r in risultati:
        righe_dati.append(
            f"- {r['symbol']}: prezzo {r['price']:.2f}, variazione {r['change_pct']:.2f}%, "
            f"RSI {r.get('rsi', 'N/D')}"
        )
    dati_testuali = "\n".join(righe_dati)

    prompt = (
        "Sei un analista finanziario che scrive per un investitore retail italiano "
        "con esperienza intermedia. Commenta brevemente in 3-4 frasi questa lista di titoli "
        "in forte calo oggi, evidenziando se ci sono anomalie o spunti interessanti:\n\n"
        f"{dati_testuali}\n\nMantieni un tono professionale ed evita consigli diretti di acquisto."
    )

    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    
    try:
        url = GEMINI_API_URL.format(model=GEMINI_MODEL)
        r = requests.post(f"{url}?key={GEMINI_API_KEY}", json=payload, headers=headers, timeout=15)
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return "⚠️ Non è stato possibile generare il commento dell'Intelligenza Artificiale."


def comando_analizza():
    invia_messaggio("🔍 Avvio scansione del mercato USA... Potrebbe richiedere un minuto.")
    
    candidati = get_candidati_mercato()
    filtrati = [c for c in candidati if supera_prefiltro(c)]
    
    if not filtrati:
        invia_messaggio("✅ Scansione completata: nessun titolo soddisfa i criteri di calo critico al momento.")
        return

    # Limita l'analisi per non bruciare troppe chiamate API di fila
    selezionati = filtrati[:MAX_TITOLI_ANALISI_APPROFONDITA]
    buoni = []

    for s in selezionati:
        rsi = get_rsi(s["symbol"])
        if rsi is not None:
            s["rsi"] = rsi
            buoni.append(s)
        time.sleep(1) # Piccolo ritardo di sicurezza per l'API rate limit

    if not buoni:
        invia_messaggio("⚠️ Impossibile completare l'analisi tecnica a causa di limiti sulle API.")
        return
