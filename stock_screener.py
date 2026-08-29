"""
Stock Screener + Telegram Notifier (universo combinato + AI opzionale)
------------------------------------------------------------------------
Cerca vere occasioni USA combinando due liste gratuite (top losers +
titoli piu' scambiati in rosso, entrambe da un'unica chiamata API), poi
applica un filtro a imbuto in 3 livelli:
  1. Pre-filtro gratuito: prezzo >= 5$, volume >= 500k, soglia di calo
     (solo per i "top losers"; i titoli piu' scambiati bastano in rosso)
  2. Analisi tecnica: RSI(14) ipervenduto + prezzo sopra la SMA200
  3. Validazione fondamentale: target price e rating degli analisti
     (contati solo se esiste copertura reale, altrimenti scartati)

Ogni titolo ottiene un punteggio 0-4; solo chi arriva a >=2 viene
segnalato su Telegram, con eventuale commento scritto da un modello AI
(Gemini gratis, o Claude come riserva a pagamento).

ATTENZIONE: questo NON e' un consiglio di investimento. E' un filtro
tecnico/fondamentale automatico basato su regole, che puo' sbagliare o
essere impreciso. Ogni segnalazione va sempre verificata con un'analisi
propria prima di qualsiasi decisione.

Limiti del piano gratuito Alpha Vantage: 25 richieste/giorno, 5/minuto.
- 1 chiamata per ottenere l'universo combinato (top losers + most active)
- 3 chiamate extra (RSI, SMA200, OVERVIEW) per ogni titolo analizzato
Il budget e' dimensionato per restare nel limite anche in giornate di
forte ribasso diffuso.
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

# Filtri di qualita' gratuiti (non consumano chiamate API): scartano titoli
# poco liquidi o "spazzatura" prima ancora di analizzarli in profondita'.
PREZZO_MINIMO = 5.0
VOLUME_MINIMO = 500000

# Soglia di sconto sul target price degli analisti per considerarlo
# significativo (in percentuale: target 15% sopra il prezzo attuale)
SCONTO_TARGET_MINIMO_PCT = 15.0

# Punteggio minimo (su 4) per considerare un titolo una vera "occasione"
# e includerlo nella notifica finale.
PUNTEGGIO_MINIMO_OCCASIONE = 2

# Quanti titoli al massimo analizzare in profondita' (RSI+SMA200+analisti),
# per restare nel budget di chiamate API anche nei giorni di forte ribasso.
MAX_TITOLI_ANALISI_APPROFONDITA = 7

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

def get_candidati_mercato():
    """Combina i top losers con i titoli piu' scambiati in rosso della giornata
    (entrambe le liste arrivano dalla stessa chiamata API, costo 1 sola chiamata)."""
    if not _consuma_budget():
        return []
    params = {
        "function": "TOP_GAINERS_LOSERS",
        "apikey": ALPHA_VANTAGE_API_KEY,
    }
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

    # unisce le due liste evitando duplicati (se un simbolo e' in entrambe,
    # tiene la versione "top_loser" perche' porta piu' informazione sulla soglia)
    combinati = {t["symbol"]: t for t in most_active}
    combinati.update({t["symbol"]: t for t in top_losers})
    return list(combinati.values())


def supera_prefiltro(t):
    """Filtro di qualita' gratuito: prezzo/volume minimi, e per i top_losers
    anche la soglia di calo minima (i most_active bastano in rosso, gia' filtrati sopra)."""
    if t["price"] < PREZZO_MINIMO or t["volume"] < VOLUME_MINIMO:
        return False
    if t["fonte"] == "top_loser":
        return t["change_pct"] <= -CALO_MINIMO_PCT
    return True


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


def get_analyst_data(symbol):
    """Ritorna target price medio degli analisti e conteggio rating buy/sell."""
    if not _consuma_budget():
        return None
    params = {
        "function": "OVERVIEW",
        "symbol": symbol,
        "apikey": ALPHA_VANTAGE_API_KEY,
    }
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


ISCRITTI_FILE = "subscribers.txt"


def leggi_iscritti():
    """Ritorna l'insieme dei chat_id iscritti alla notifica giornaliera."""
    if os.path.exists(ISCRITTI_FILE):
        with open(ISCRITTI_FILE, "r") as f:
            return {riga.strip() for riga in f if riga.strip()}
    return set()


def send_telegram_message(text, chat_id=None):
    """Manda un messaggio a un chat_id specifico (default: proprietario)."""
    url = TELEGRAM_API_URL.format(token=TELEGRAM_BOT_TOKEN)
    payload = {"chat_id": chat_id or TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    r = requests.post(url, data=payload, timeout=15)
    return r.ok


def invia_a_tutti_gli_iscritti(text):
    """Manda lo stesso messaggio al proprietario + a tutti gli iscritti."""
    destinatari = {TELEGRAM_CHAT_ID} | leggi_iscritti()
    for chat_id in destinatari:
        send_telegram_message(text, chat_id)
    print(f"Messaggio inviato a {len(destinatari)} destinatari.")


# ---------------------------------------------------------------------------
# ANALISI AI (opzionale)
# ---------------------------------------------------------------------------

def _prompt_commento(risultati):
    righe_dati = []
    for r in risultati:
        rsi_txt = f"{r['rsi']:.1f}" if r["rsi"] is not None else "N/D"
        analisti_txt = (
            f"target price {r['sconto_pct']:+.1f}% vs prezzo attuale, "
            f"{r['analisti']['buy']} buy/{r['analisti']['hold']} hold/{r['analisti']['sell']} sell"
            if r["analisti"] and r["sconto_pct"] is not None else "dati analisti N/D"
        )
        righe_dati.append(
            f"- {r['symbol']}: prezzo {r['price']:.2f}, variazione oggi {r['change_pct']:.2f}%, "
            f"RSI {rsi_txt}, trend vs SMA200 {'positivo' if r['trend_positivo'] else 'negativo'}, "
            f"{analisti_txt}"
        )
    dati_testuali = "\n".join(righe_dati)

    return (
        "Sei un analista finanziario che scrive per un investitore retail italiano "
        "con esperienza intermedia. Ti fornisco un elenco di titoli USA in forte calo "
        "oggi che hanno gia' superato un filtro di qualita' (trend di fondo, RSI, "
        "target price degli analisti). Per ciascun titolo scrivi 1-2 frasi brevi "
        "in italiano su perche' potrebbe essere una occasione interessante da "
        "approfondire, basandoti sui dati forniti. Non dare consigli di acquisto "
        "diretti, descrivi solo la situazione. Massimo 300 parole totali.\n\n"
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
    """Arricchisce un titolo (gia' ottenuto da get_top_losers) con RSI, SMA200
    e dati degli analisti, calcolando un punteggio di "vera occasione" 0-4."""
    symbol = base["symbol"]

    time.sleep(13)
    rsi = get_rsi(symbol)

    time.sleep(13)
    sma200 = get_sma(symbol, 200)

    time.sleep(13)
    analisti = get_analyst_data(symbol)

    trend_positivo = sma200 is not None and base["price"] > sma200
    ipervenduto = rsi is not None and rsi < RSI_IPERVENDUTO

    sconto_target = False
    maggioranza_buy = False
    sconto_pct = None
    copertura_reale = False
    if analisti:
        copertura_reale = (analisti["buy"] + analisti["hold"] + analisti["sell"]) > 0
        if copertura_reale and analisti["target_price"] > 0:
            sconto_pct = (analisti["target_price"] - base["price"]) / base["price"] * 100
            sconto_target = sconto_pct >= SCONTO_TARGET_MINIMO_PCT
            maggioranza_buy = analisti["buy"] > (analisti["hold"] + analisti["sell"])

    punteggio = sum([trend_positivo, ipervenduto, sconto_target, maggioranza_buy])

    return {
        **base,
        "rsi": rsi,
        "sma200": sma200,
        "trend_positivo": trend_positivo,
        "ipervenduto": ipervenduto,
        "analisti": analisti,
        "sconto_pct": sconto_pct,
        "sconto_target": sconto_target,
        "maggioranza_buy": maggioranza_buy,
        "punteggio_interesse": punteggio,
    }


def formatta_messaggio(risultati, commento_ai):
    if not risultati:
        return None

    righe = ["🎯 <b>Possibili occasioni USA oggi</b>\n"]
    for r in risultati:
        stelle = "⭐" * r["punteggio_interesse"] if r["punteggio_interesse"] > 0 else "–"
        rsi_txt = f"{r['rsi']:.1f}" if r["rsi"] is not None else "N/D"
        if r["analisti"] and r["sconto_pct"] is not None:
            analisti_txt = (
                f"Target analisti: {r['analisti']['target_price']:.2f} "
                f"({r['sconto_pct']:+.1f}% vs prezzo attuale), "
                f"{r['analisti']['buy']} buy / {r['analisti']['hold']} hold / {r['analisti']['sell']} sell"
            )
        else:
            analisti_txt = "⚠️ Nessuna copertura analisti reale (dato non affidabile)"

        righe.append(
            f"\n<b>{r['symbol']}</b>  {r['change_pct']:.2f}%  [{r['punteggio_interesse']}/4]\n"
            f"Prezzo: {r['price']:.2f}\n"
            f"RSI: {rsi_txt} {'(ipervenduto)' if r['ipervenduto'] else ''}\n"
            f"Trend (vs SMA200): {'positivo' if r['trend_positivo'] else 'negativo'}\n"
            f"{analisti_txt}\n"
            f"Punteggio: {stelle}\n"
        )

    if commento_ai:
        righe.append(f"\n🤖 <b>Commento AI</b>\n{commento_ai}\n")

    righe.append(
        "\n⚠️ Analisi tecnica automatica (in parte generata da AI), non è un "
        "consiglio di investimento. Verifica sempre con analisi tua prima di decidere."
    )
    return "".join(righe)


def main():
    tutti_candidati = get_candidati_mercato()
    if not tutti_candidati:
        invia_a_tutti_gli_iscritti(
            "📊 Non è stato possibile recuperare i dati di mercato oggi "
            "(o nessun titolo disponibile)."
        )
        return

    # LIVELLO 1: pre-filtro gratuito (qualita'/liquidita' + soglia calo per i top losers)
    candidati = [t for t in tutti_candidati if supera_prefiltro(t)]

    # ordina dal calo piu' forte e taglia al budget disponibile
    candidati.sort(key=lambda t: t["change_pct"])
    candidati = candidati[:MAX_TITOLI_ANALISI_APPROFONDITA]

    # LIVELLO 2 + 3: analisi tecnica + validazione analisti
    risultati = []
    for base in candidati:
        print(f"Analizzo {base['symbol']}...")
        risultati.append(analizza_titolo(base))

    # filtro finale: solo le vere "occasioni" (punteggio >= soglia)
    occasioni = [r for r in risultati if r["punteggio_interesse"] >= PUNTEGGIO_MINIMO_OCCASIONE]
    occasioni.sort(key=lambda r: r["punteggio_interesse"], reverse=True)

    commento_ai = genera_commento_ai(occasioni)
    messaggio = formatta_messaggio(occasioni, commento_ai)

    if messaggio:
        invia_a_tutti_gli_iscritti(messaggio)
        print("Notifica inviata.")
    else:
        invia_a_tutti_gli_iscritti(
            f"📊 Nessun titolo USA ha superato oggi il punteggio minimo di "
            f"{PUNTEGGIO_MINIMO_OCCASIONE}/4 per essere segnalato come occasione "
            f"(controllati {len(risultati)} titoli in calo)."
        )
        print("Nessuna occasione idonea, inviato messaggio di stato.")


if __name__ == "__main__":
    main()
