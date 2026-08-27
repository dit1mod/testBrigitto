# Come mettere online il bot (anche da telefono)

## 1. Crea un account GitHub (se non ce l'hai già)
Vai su github.com e registrati. È gratis.

## 2. Crea un nuovo repository
- Premi "+" in alto → "New repository"
- Nome: es. `stock-screener-bot`
- Puoi lasciarlo privato (consigliato, contiene la logica del tuo bot)
- Crea il repository

## 3. Carica i file
Dal browser (anche mobile) o dall'app GitHub:
- Carica `stock_screener.py` dentro una cartella `telegram-stock-bot/`
- Carica `.github/workflows/daily_screener.yml` (stessa struttura di cartelle)

Puoi usare il pulsante "Add file → Upload files" sul sito, funziona anche da telefono.

## 4. Configura i "Secrets" (le tue chiavi private)
Questo è il passo più importante: NON mettere mai token e chiavi direttamente nel codice
pubblico. Le mettiamo nei "Secrets" di GitHub, che sono criptati.

- Vai su: Settings del repository → Secrets and variables → Actions
- Premi "New repository secret" e crea questi 3:
  - `ALPHA_VANTAGE_API_KEY` → la tua chiave Alpha Vantage
  - `TELEGRAM_BOT_TOKEN` → 8792722517:AAEw3fGPBeNqtfEkwIYrD3Q-guw92jAUv7k
  - `TELEGRAM_CHAT_ID` → 527421998

⚠️ Dato che il token del bot è già stato condiviso in chiaro in questa conversazione,
ti consiglio di rigenerarlo prima di andare live: su Telegram scrivi a @BotFather
`/mybots` → seleziona Brigitto_bot → "API Token" → "Revoke current token".
Poi aggiorna il secret su GitHub con il nuovo token.

## 5. Testa manualmente
- Vai su "Actions" nel repository
- Seleziona "Daily Stock Screener" nella lista a sinistra
- Premi "Run workflow" (pulsante manuale, grazie a `workflow_dispatch`)
- Aspetta un paio di minuti (lo script ha delle pause per rispettare i limiti API)
- Controlla che arrivi il messaggio su Telegram

## 6. Da qui in poi è automatico
Il workflow gira da solo ogni giorno feriale all'orario impostato nel file
`daily_screener.yml` (attualmente 15:45 UTC). Puoi modificare l'orario editando
la riga `cron:` direttamente dal sito GitHub (anche da telefono, è un file di testo).

## Note importanti
- Il piano gratuito di Alpha Vantage concede 25 chiamate/giorno: la watchlist
  nello script è già dimensionata per starci dentro, ma se aggiungi troppi
  titoli lo script si fermerà prima di finire (senza errori, solo in modo
  incompleto) grazie al contatore di sicurezza incluso.
- Ricorda: le segnalazioni sono un filtro tecnico automatico, non un consiglio
  di investimento.
