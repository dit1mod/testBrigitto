"""
Bot Comandi Telegram (polling via GitHub Actions) + gestione iscritti
------------------------------------------------------------------------
Questo script viene lanciato ogni 10 minuti da GitHub Actions. Ad ogni
esecuzione controlla se qualcuno ha scritto un nuovo comando al bot:

  /start     -> iscrive chi scrive alla notifica giornaliera condivisa
  /stop      -> disiscrive chi scrive
  /help      -> elenco comandi disponibili (per tutti)
  /analizza  -> analisi a comando (SOLO per il proprietario del bot)
  /mercato   -> stato mercati (SOLO per il proprietario del bot)

Gli iscritti sono salvati in un file (subscribers.txt) nel repository,
che la notifica giornaliera (stock_screener.py) legge per sapere a chi
mandare il messaggio, oltre che al proprietario.

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
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")  # proprietario del bot
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{{model}}:generateContent"

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
ISCRITTI_FILE = "subscribers.txt"

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
# GESTIONE ISCRITTI
# ---------------------------------------------------------------------------

def leggi_iscritti():
    """Ritorna l'insieme dei chat_id iscritti (stringhe)."""
    if os.path.exists(ISCRITTI_FILE):
        with open(ISCRITTI_FILE, "r") as f:
            return {riga.strip() for riga in f if riga.strip()}
    return set()


def salva_iscritti(iscritti):
    with open(ISCRITTI_FILE, "w") as f:
        for chat_id in sorted(iscritti):
            f.write(f"{chat_id}\n")


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


def invia_a(chat_id, testo):
    """Manda un messaggio a un chat_id specifico."""
    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN) + "/sendMessage"
    payload = {"chat_id": chat_id, "text": testo, "parse_mode": "HTML"}
    requests.post(url, data=payload, timeout=15)


def invia_messaggio(testo):
    """Manda un messaggio al proprietario del bot (comportamento di prima)."""
    invia_a(TELEGRAM_CHAT_ID, testo)


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

def get_candidati_mercato():
    """Combina i top losers con i titoli piu' scambiati in rosso (1 chiamata API)."""
    if not _consuma_budget():
        return []
    params = {"function": "TOP_GAINERS_LOSERS", "apikey": ALPHA_VANTAGE_API_KEY}
    r = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=15)
    data = r.json()

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


def get_analyst_data(symbol):
    if not _consuma_budget():
        return None
    params = {"function": "OVERVIEW", "symbol": symbol, "apikey": ALPHA_VANTAGE_API_KEY}
    r = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=15)
    data = r.json()
    if not data or "AnalystTargetPrice" not in data:
        return None
    try:
        target_price = float(data.get("AnalystTargetPrice", 0) or 0)
        buy = int(data.get("AnalystRatingStrongBuy", 0) or 0) + int(data.get("AnalystRatingBuy", 0) or 0)
        hold = int(data.get("AnalystRatingHold", 0) or 0)
        sell = int(data.get("AnalystRatingSell", 0) or 0) + int(data.get("AnalystRatingStrongSell", 0) or 0)
        return {"target_price": target_price, "buy": buy, "hold": hold, "sell": sell}
    except (ValueError, TypeError):
        return None


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

    tutti_candidati = get_candidati_mercato()
    if not tutti_candidati:
        invia_messaggio("⚠️ Non riesco a recuperare i dati di mercato ora (mercato chiuso o budget esaurito).")
        return

    candidati = [t for t in tutti_candidati if supera_prefiltro(t)]
    candidati.sort(key=lambda t: t["change_pct"])
    candidati = candidati[:MAX_TITOLI_ANALISI_APPROFONDITA]

    if not candidati:
        invia_messaggio(
            f"📊 Nessun titolo USA supera oggi i filtri di base (calo ≥{CALO_MINIMO_PCT}%, "
            f"prezzo ≥{PREZZO_MINIMO}$, volume ≥{VOLUME_MINIMO:,})."
        )
        return

    risultati_completi = []
    for base in candidati:
        time.sleep(13)
        rsi = get_rsi(base["symbol"])
        time.sleep(13)
        sma200 = get_sma(base["symbol"], 200)
        time.sleep(13)
        analisti = get_analyst_data(base["symbol"])

        trend_positivo = sma200 is not None and base["price"] > sma200
        ipervenduto = rsi is not None and rsi < RSI_IPERVENDUTO

        sconto_target = False
        maggioranza_buy = False
        sconto_pct = None
        if analisti:
            copertura_reale = (analisti["buy"] + analisti["hold"] + analisti["sell"]) > 0
            if copertura_reale and analisti["target_price"] > 0:
                sconto_pct = (analisti["target_price"] - base["price"]) / base["price"] * 100
                sconto_target = sconto_pct >= SCONTO_TARGET_MINIMO_PCT
                maggioranza_buy = analisti["buy"] > (analisti["hold"] + analisti["sell"])

        punteggio = sum([trend_positivo, ipervenduto, sconto_target, maggioranza_buy])
        rsi_txt = f"{rsi:.1f}" if rsi is not None else "N/D"
        trend_txt = "positivo" if trend_positivo else "negativo"

        risultati_completi.append({
            **base, "rsi": rsi, "rsi_txt": rsi_txt, "trend_txt": trend_txt,
            "trend_positivo": trend_positivo, "ipervenduto": ipervenduto,
            "analisti": analisti, "sconto_pct": sconto_pct, "punteggio": punteggio,
        })

    occasioni = [r for r in risultati_completi if r["punteggio"] >= PUNTEGGIO_MINIMO_OCCASIONE]
    occasioni.sort(key=lambda r: r["punteggio"], reverse=True)

    if not occasioni:
        invia_messaggio(
            f"📊 {len(risultati_completi)} titoli controllati, ma nessuno ha raggiunto "
            f"il punteggio minimo {PUNTEGGIO_MINIMO_OCCASIONE}/4 per essere una vera occasione."
        )
        return

    righe = ["🎯 <b>Possibili occasioni (a comando)</b>\n"]
    for r in occasioni:
        stelle = "⭐" * r["punteggio"]
        if r["analisti"] and r["sconto_pct"] is not None:
            analisti_txt = (
                f"Target: {r['analisti']['target_price']:.2f} ({r['sconto_pct']:+.1f}%), "
                f"{r['analisti']['buy']}buy/{r['analisti']['hold']}hold/{r['analisti']['sell']}sell"
            )
        else:
            analisti_txt = "⚠️ Nessuna copertura analisti reale"
        righe.append(
            f"\n<b>{r['symbol']}</b>  {r['change_pct']:.2f}%  [{r['punteggio']}/4]\n"
            f"Prezzo: {r['price']:.2f}\n"
            f"RSI: {r['rsi_txt']}\n"
            f"Trend: {r['trend_txt']}\n"
            f"{analisti_txt}\n"
            f"Punteggio: {stelle}\n"
        )

    commento_ai = genera_commento_ai(occasioni)
    if commento_ai:
        righe.append(f"\n🤖 <b>Commento AI</b>\n{commento_ai}\n")

    righe.append("\n⚠️ Non è un consiglio di investimento.")
    invia_messaggio("\n".join(righe))


# ---------------------------------------------------------------------------
# COMANDI: /start, /stop, /help  (aperti a chiunque)
# ---------------------------------------------------------------------------

def comando_start(chat_id):
    iscritti = leggi_iscritti()
    if chat_id in iscritti or chat_id == TELEGRAM_CHAT_ID:
        invia_a(chat_id, "✅ Sei già iscritto alla notifica giornaliera!")
        return
    iscritti.add(chat_id)
    salva_iscritti(iscritti)
    invia_a(
        chat_id,
        "🎉 Iscrizione completata! Riceverai la notifica giornaliera con "
        "le possibili occasioni sul mercato USA (analisi automatica, "
        "non è un consiglio di investimento).\n\n"
        "Scrivi /stop in qualsiasi momento per disiscriverti."
    )


def comando_stop(chat_id):
    iscritti = leggi_iscritti()
    if chat_id not in iscritti:
        invia_a(chat_id, "Non risulti iscritto.")
        return
    iscritti.discard(chat_id)
    salva_iscritti(iscritti)
    invia_a(chat_id, "❌ Disiscrizione completata. Non riceverai più la notifica giornaliera.")


def comando_help(chat_id):
    invia_a(
        chat_id,
        "🤖 <b>Comandi disponibili</b>\n\n"
        "/start - iscriviti alla notifica giornaliera con le occasioni USA\n"
        "/stop - disiscriviti\n"
        "/help - questo messaggio\n\n"
        "La notifica arriva una volta al giorno, in automatico, con i titoli "
        "che superano i nostri filtri (calo, trend, RSI, target analisti). "
        "Non è un consiglio di investimento."
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
        dati_messaggio = msg.get("message", {})
        testo = dati_messaggio.get("text", "").strip().lower()
        mittente = str(dati_messaggio.get("chat", {}).get("id", ""))

        if not mittente:
            continue

        if testo == "/start":
            comando_start(mittente)
        elif testo == "/stop":
            comando_stop(mittente)
        elif testo == "/help":
            comando_help(mittente)
        elif testo == "/analizza":
            if mittente == TELEGRAM_CHAT_ID:
                comando_analizza()
            else:
                invia_a(mittente, "Questo comando è riservato. Riceverai comunque la notifica giornaliera se iscritto con /start.")
        elif testo == "/mercato":
            if mittente == TELEGRAM_CHAT_ID:
                comando_mercato()
            else:
                invia_a(mittente, "Questo comando è riservato. Riceverai comunque la notifica giornaliera se iscritto con /start.")
        # comandi sconosciuti: ignorati silenziosamente

    salva_ultimo_update_id(ultimo_id)


if __name__ == "__main__":
    main()
