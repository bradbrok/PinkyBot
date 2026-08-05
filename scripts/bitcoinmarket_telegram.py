#!/usr/bin/env python3
"""
BitcoinMarket.net Telegram Channel Automation
Canale: @BitcoinMarketnet | Bot: @Pinkyzio_bot

Contenuti 100% originali — niente RSS da competitor.
Slot orari (Europe/Rome):
  - 08:00 → Morning briefing: prezzo BTC + tip educativo
  - 13:30 → Pausa pranzo: analisi originale
  - 19:00 → Sera: sondaggio (mer/dom) o promo interna

Usage:
  python3 bitcoinmarket_telegram.py --slot morning
  python3 bitcoinmarket_telegram.py --slot lunch
  python3 bitcoinmarket_telegram.py --slot evening
  python3 bitcoinmarket_telegram.py --slot test --dry-run
"""

import argparse
import json
import sqlite3
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = "8634209696:AAHdtNa9BtmMZZea5POdL_iJ28vj8JFXzdo"
CHANNEL_ID = "-1003996981941"
SITE_URL = "https://bitcoinmarket.net"
STATE_DB = Path(__file__).parent / "bitcoinmarket_tg_state.db"
COINGECKO_URL = "https://api.coingecko.com/api/v3"

# ── Contenuti originali ───────────────────────────────────────────────────────
# Piano editoriale 4 settimane a rotazione ciclica
# Settimana 1: Exchange e commissioni
# Settimana 2: Fondamenti Bitcoin
# Settimana 3: Sicurezza e tasse
# Settimana 4: Strategie e investimento

MORNING_INTROS = [
    "Buongiorno ₿", "Buongiorno da BitcoinMarket 👋", "Inizia la giornata con i mercati 📊",
    "Un nuovo giorno, nuovi prezzi 🔔", "Mercati aperti, eccoci 🟠",
]

# Tips indicizzati per settimana tematica e giorno (0=lun..6=dom)
# Formato: (settimana_1_4, giorno_0_6, titolo, corpo, link)
WEEKLY_TIPS = [
    # ── WEEK 1: Exchanges & fees ──────────────────────────────────────────────
    (1, 0, "💡 *Bid-ask spread — the hidden cost*",
     "When you buy BTC at €65,000 and see the price at €64,980, the difference is the spread. Across different exchanges it can vary from 0.05% to 0.5%. On €1,000, that's 50 cents to €5 extra.",
     f"{SITE_URL}/guide/migliore-exchange-2026.html"),
    (1, 1, "💡 *Maker vs Taker — fees you don't notice*",
     "Maker = place a limit order (you wait). Taker = execute immediately at market. Makers pay less because they add liquidity. On Binance: Maker 0.1%, Taker 0.1%. On Coinbase: up to 0.6%.",
     f"{SITE_URL}/guide/migliore-exchange-2026.html"),
    (1, 2, "💡 *KYC explained in 60 seconds*",
     "Know Your Customer = mandatory identity verification on all EU/international exchanges. Requires ID + selfie. Legally required since 2018. Without KYC: severe limits or account suspension.",
     f"{SITE_URL}/guide/migliore-exchange-2026.html"),
    (1, 3, "💡 *Hidden fees: gas, withdrawal, spread*",
     "Trading fee visible: 0.1%. But there's more: withdrawal fees (0.0004 BTC = ~€26), conversion spread EUR→BTC (~0.2%), network fees. Calculate total cost before choosing your exchange.",
     f"{SITE_URL}/guide/migliore-exchange-2026.html"),
    (1, 4, "💡 *How to verify an exchange's security*",
     "Checklist: MiCA/FCA license? Cold storage >95% of funds? 2FA mandatory? Proof of Reserves published? Funds insured? Binance, Kraken, Coinbase all pass these criteria.",
     f"{SITE_URL}/guide/migliore-exchange-2026.html"),
    (1, 5, "💡 *Hardware wallet — is it worth it?*",
     "For <€500 in crypto: no. For >€1,000: yes, definitely. Ledger Nano S Plus (€79) or Trezor Model One (€69). One-time cost, permanent protection from exchange hacks.",
     f"{SITE_URL}/guide/sicurezza-wallet.html"),
    (1, 6, "💡 *5 mistakes to avoid on exchanges*",
     "1) Keep everything on one exchange. 2) Don't enable 2FA. 3) Use the same password as your Gmail. 4) Ignore security emails. 5) Don't verify the address before withdrawing.",
     f"{SITE_URL}/guide/migliore-exchange-2026.html"),
    # ── WEEK 2: Bitcoin basics ───────────────────────────────────────────────
    (2, 0, "💡 *Blockchain — without the jargon*",
     "A public ledger shared by millions of computers where every transaction is recorded permanently. Nobody can erase or modify it. Like an indestructible accounting book.",
     f"{SITE_URL}/guide/cose-bitcoin.html"),
    (2, 1, "💡 *Mining — who does it and why*",
     "Miners use powerful computers to validate Bitcoin transactions and earn BTC as reward. Today it requires specialized ASICs. Profitable only with cheap electricity (<€0.05/kWh).",
     f"{SITE_URL}/guide/cose-bitcoin.html"),
    (2, 2, "💡 *Fork — what happens when Bitcoin splits*",
     "Hard fork = incompatible upgrade → creates a new coin (like Bitcoin Cash in 2017). Soft fork = compatible upgrade → everyone stays on the same chain. Bitcoin has experienced both.",
     f"{SITE_URL}/guide/cose-bitcoin.html"),
    (2, 3, "💡 *21 million — why this limit is absolute*",
     "Satoshi wrote into the code that there can never be more than 21 million BTC. Changing it would require consensus from every node on the network. Practically impossible. It's digital scarcity.",
     f"{SITE_URL}/guide/cose-bitcoin.html"),
    (2, 4, "💡 *Halving — the scarcity mechanism*",
     "Every 210,000 blocks (~4 years) the miner reward cuts in half. April 2024: from 6.25 to 3.125 BTC/block. Fewer new BTC each day = inflation approaching zero. Next halving: 2028.",
     f"{SITE_URL}/guide/bitcoin-halving.html"),
    (2, 5, "💡 *Is Bitcoin legal worldwide?*",
     "Yes in most countries. In the EU it's regulated as a crypto-asset under MiCA. Exchanges must be licensed by financial authorities. Capital gains are typically taxed at 20-40% depending on jurisdiction.",
     f"{SITE_URL}/guide/tassazione-crypto-italia.html"),
    (2, 6, "💡 *Bull/bear cycles — how to spot them*",
     "Bull market: extended price rallies, euphoric sentiment, mainstream media hype. Bear market: -80% from peaks, capitulation, 'Bitcoin is dead' headlines. Historically: ~4-year cycles.",
     f"{SITE_URL}/guide/cose-bitcoin.html"),
    # ── WEEK 3: Security & taxes ─────────────────────────────────────────────
    (3, 0, "💡 *Hot vs cold wallet — the difference that saves your BTC*",
     "Hot wallet = internet-connected (Metamask, exchange app). Convenient but exposed to hacks. Cold wallet = offline (Ledger, Trezor, paper). Slow but secure. Rule: small amounts hot, large amounts cold.",
     f"{SITE_URL}/guide/sicurezza-wallet.html"),
    (3, 1, "💡 *Seed phrase — the 12/24 words you must never lose*",
     "It's the only backup for your wallet. If you lose the device, the seed phrase rebuilds it completely on any other compatible device. Write it by hand on paper. Never type it online.",
     f"{SITE_URL}/guide/sicurezza-wallet.html"),
    (3, 2, "💡 *How to spot a crypto scam*",
     "Red flags: 'guaranteed 10% monthly returns', artificial urgency, requests for seed phrase or password, unsolicited DM offers. If it sounds too good to be true, it's a scam.",
     f"{SITE_URL}/guide/sicurezza-wallet.html"),
    (3, 3, "💡 *Telegram phishing — the most common method*",
     "Fake admin in your DM: 'You've been selected for an airdrop, enter your seed phrase'. Rule: legitimate admins NEVER ask for private keys or seed phrases. Block and report.",
     f"{SITE_URL}/guide/sicurezza-wallet.html"),
    (3, 4, "💡 *Crypto taxes — the basic calculation*",
     "Buy 0.1 BTC at €50,000/BTC = €5,000. Sell at €65,000/BTC = €6,500. Capital gain: €1,500. Tax rates vary by country but typically 20-40%. Keep an Excel sheet of every trade.",
     f"{SITE_URL}/guide/tassazione-crypto-italia.html"),
    (3, 5, "💡 *How to report crypto on your tax return*",
     "Most countries require capital gains reporting. Keep detailed records of buy/sell dates and amounts. Exchanges provide account statements on request. Don't leave it for year-end.",
     f"{SITE_URL}/guide/tassazione-crypto-italia.html"),
    (3, 6, "💡 *Due diligence on a new crypto project*",
     "Before investing: is the team publicly known (real names)? Is the code open source and audited? Is there a technical whitepaper? Clear tokenomics? Does it solve a real problem? If no to 2+ questions: pass.",
     f"{SITE_URL}"),
    # ── WEEK 4: Strategies ───────────────────────────────────────────────────
    (4, 0, "💡 *DCA — invest without stress*",
     "Invest €100/month in BTC on the same day each month, regardless of price. Over 3 years (2021-2024) DCA into Bitcoin delivered average returns of +180% despite bear markets. No timing, no anxiety.",
     f"{SITE_URL}/guide/come-comprare-bitcoin-2026.html"),
    (4, 1, "💡 *When to sell — exit strategies*",
     "Common mistake: selling out of fear or greed. Strategy: decide your target BEFORE buying (+100%? +300%?) and stick to it. Alternative: sell a fixed % each month during bull markets. Never all at once.",
     f"{SITE_URL}/guide/come-comprare-bitcoin-2026.html"),
    (4, 2, "💡 *How much Bitcoin in your portfolio?*",
     "General rule for retail investors: no more than 5-10% of net worth in high-risk assets. For long-term believers: up to 20% is defensible. Depends on your time horizon and risk tolerance.",
     f"{SITE_URL}/guide/come-comprare-bitcoin-2026.html"),
    (4, 3, "💡 *Bitcoin correlation with traditional markets*",
     "In 2022-2023 Bitcoin moved closely correlated with Nasdaq. In macro crisis events it sells off. In periods of abundant liquidity (low rates) Bitcoin outperforms. It's not yet a safe haven asset.",
     f"{SITE_URL}"),
    (4, 4, "💡 *2FA — two-factor authentication, mandatory*",
     "Always enable 2FA on every exchange: Google Authenticator or Authy (never SMS — vulnerable to SIM swaps). Generate backup codes and save them offline. With 2FA: an attacker with your password is blocked.",
     f"{SITE_URL}/guide/sicurezza-wallet.html"),
    (4, 5, "💡 *How to read crypto news without FUD*",
     "FUD = Fear, Uncertainty, Doubt. Technique: read the news, then research who wrote it and why. Exchanges publishing negative news about competitors? Governments announcing bans (usually never happens)? Filter it out.",
     f"{SITE_URL}"),
    (4, 6, "💡 *The 5 most common crypto investor mistakes*",
     "1) All-in at all-time highs. 2) Sell everything at -50%. 3) Trust 'gurus' on Twitter/X. 4) Don't backup your wallet. 5) Ignore taxes until year-end. Avoiding these is already half the battle.",
     f"{SITE_URL}/guide/come-comprare-bitcoin-2026.html"),
    # ── WEEK 5: PoW vs PoS & Consensus ──────────────────────────────────────────
    (5, 0, "💡 *Bitcoin security: PoW vs PoS*",
     "Bitcoin's network security comes from miners solving complex puzzles, not from validators staking coins. This fundamental difference means Bitcoin prioritizes decentralization over energy efficiency.",
     f"{SITE_URL}/guide/cose-bitcoin.html"),
    (5, 1, "💡 *Lightning Network: milliseconds, fractions of a cent*",
     "Lightning Network transactions settle in milliseconds at a fraction of a cent. For everyday payments, it's already more practical than any traditional payment system.",
     f"{SITE_URL}/guide/cose-bitcoin.html"),
    (5, 2, "💡 *Spot Bitcoin ETFs: ownership without custody headaches*",
     "Spot Bitcoin ETFs (like IBIT and FBTC) let you own Bitcoin through your brokerage account without managing private keys. The tradeoff: you're trusting a custodian instead of controlling your coins directly.",
     f"{SITE_URL}/guide/come-comprare-bitcoin-2026.html"),
    (5, 3, "💡 *Ethereum's energy switch: 99% less power*",
     "Ethereum switched to proof-of-stake in 2022, cutting its energy consumption by 99%. Bitcoin's proof-of-work intentionally uses more energy for stronger security guarantees.",
     f"{SITE_URL}/guide/cose-bitcoin.html"),
    (5, 4, "💡 *Self-custody: not your keys, not your coins*",
     "Self-custody means you hold your own private keys. The rule: if you don't control the keys, you don't truly own the asset. Major exchange collapses (FTX, Mt. Gox) prove why this matters.",
     f"{SITE_URL}/guide/sicurezza-wallet.html"),
    (5, 5, "💡 *On-chain: long-term holder accumulation*",
     "On-chain metrics like long-term holder accumulation tell you what smart money is doing. When whales buy in bear markets, it's historically preceded major price moves.",
     f"{SITE_URL}"),
    (5, 6, "💡 *Altcoins: venture bets, not currencies*",
     "Altcoins (everything except Bitcoin) are venture bets, not currencies. Most will fail. If you can't explain why a coin exists besides 'make money', avoid it.",
     f"{SITE_URL}/guide/come-comprare-bitcoin-2026.html"),
    # ── WEEK 6: DeFi & Lightning ─────────────────────────────────────────────────
    (6, 0, "💡 *Crypto taxes: every swap is a taxable event*",
     "Crypto taxes are complex: every trade, even altcoin-to-altcoin swaps, trigger capital gains events. Keep records or hire a specialist — tax authorities are getting serious.",
     f"{SITE_URL}/guide/tassazione-crypto-italia.html"),
    (6, 1, "💡 *NFTs: hype is over, utility survives*",
     "NFTs as speculative assets mostly failed after 2022. The only NFTs with genuine use cases are those that unlock real utility (gaming, proof of membership), not profile pictures.",
     f"{SITE_URL}"),
    (6, 2, "💡 *Bitcoin and macro: they move together*",
     "Bitcoin's price often correlates with risk assets in macro downturns. When the Fed raises rates, both stocks and crypto tend to fall. They're not independent stores of value yet.",
     f"{SITE_URL}"),
    (6, 3, "💡 *DeFi: no banks, but real risks*",
     "DeFi protocols let you lend, borrow, and trade without banks. But 'decentralized' doesn't mean safe — smart contract bugs and rug pulls are real risks many retail users underestimate.",
     f"{SITE_URL}"),
    (6, 4, "💡 *Lightning channels: friction before freedom*",
     "Lightning Network's main limit: you need to open a payment channel (locking Bitcoin) to use it. For casual users, it adds friction that traditional payment apps don't have.",
     f"{SITE_URL}/guide/cose-bitcoin.html"),
    (6, 5, "💡 *Bitcoin ETF options: institutional depth*",
     "Bitcoin ETF options (like BTC call options on IBIT) let institutional traders hedge or speculate without managing custody. This deepens market efficiency and reduces retail volatility.",
     f"{SITE_URL}/guide/come-comprare-bitcoin-2026.html"),
    (6, 6, "💡 *Staking yield: passive income with caveats*",
     "Proof-of-stake validators earn yields by securing the network. The yield varies by coin and network congestion, but it's passive income if you trust the underlying protocol won't collapse.",
     f"{SITE_URL}"),
    # ── WEEK 7: Security & Self-Custody ──────────────────────────────────────────
    (7, 0, "💡 *Hardware wallets: worth every cent*",
     "Hardware wallets (Ledger, Trezor) hold your private keys offline. They cost $50-100 upfront but eliminate 99% of hacking vectors. Essential if you own significant crypto.",
     f"{SITE_URL}/guide/sicurezza-wallet.html"),
    (7, 1, "💡 *MVRV ratio: overbought or undersold?*",
     "The MVRV ratio (Market Value to Realized Value) shows when Bitcoin is overbought or undersold relative to what long-term holders paid. Extreme levels often precede reversals.",
     f"{SITE_URL}"),
    (7, 2, "💡 *Altcoins: network effects matter most*",
     "Most altcoins exist to solve problems Bitcoin doesn't need solved or to concentrate wealth in founders' hands. Before buying, ask: does this coin have genuine network effects?",
     f"{SITE_URL}/guide/come-comprare-bitcoin-2026.html"),
    (7, 3, "💡 *Crypto taxes by country: know your rules*",
     "Crypto taxes vary by country: some treat it as property (US), others as currency (El Salvador), others ban it entirely. Know your local rules before trading.",
     f"{SITE_URL}/guide/tassazione-crypto-italia.html"),
    (7, 4, "💡 *NFT gaming: fun first, earn second*",
     "NFT gaming has potential if the game is actually fun and the NFT is optional. Most 'play-to-earn' games are Ponzi schemes disguised as games.",
     f"{SITE_URL}"),
    (7, 5, "💡 *Fed tightening hurts crypto too*",
     "During Fed tightening, Bitcoin often underperforms tech stocks because both are risk-on assets. Diversification across asset classes beats chasing crypto-only portfolios.",
     f"{SITE_URL}"),
    (7, 6, "💡 *DeFi yield farming: the hidden risks*",
     "DeFi yield farming looks attractive until a black swan event wipes out pools. Impermanent loss and protocol risk mean most farmers lose money in bear markets.",
     f"{SITE_URL}"),
    # ── WEEK 8: ETFs, Taxes & Mindset ────────────────────────────────────────────
    (8, 0, "💡 *Lightning on-chain cost: plan ahead*",
     "Opening a Lightning channel requires an on-chain transaction, which can cost $1-30 depending on network congestion. Lightning shines for frequent micropayments, not one-time use.",
     f"{SITE_URL}/guide/cose-bitcoin.html"),
    (8, 1, "💡 *ETF fees compound over time*",
     "Bitcoin ETFs eliminate custodial counterparty risk but introduce management fee risks. Even 0.2% annually costs you 2% per decade in opportunity cost — check the fee before buying.",
     f"{SITE_URL}/guide/come-comprare-bitcoin-2026.html"),
    (8, 2, "💡 *Staking rewards are taxable income*",
     "Staking rewards are taxable as income in most countries the year you earn them, even if the price crashes later. This makes tax-loss harvesting more complex in crypto.",
     f"{SITE_URL}/guide/tassazione-crypto-italia.html"),
    (8, 3, "💡 *NFT provenance doesn't equal value*",
     "NFTs on blockchains like Ethereum or Solana have real provenance, but provenance doesn't equal value. Most people are speculating on price, not collecting digital art.",
     f"{SITE_URL}"),
    (8, 4, "💡 *Bitcoin correlation: no longer a safe haven*",
     "Bitcoin's correlation with macro assets (stocks, commodities) strengthened post-2020. If you wanted uncorrelated returns, Bitcoin no longer reliably delivers that.",
     f"{SITE_URL}"),
    (8, 5, "💡 *DeFi liquidations: how retail gets burned*",
     "DeFi liquidations cascade during price crashes because borrowed positions get force-closed when collateral drops. This amplifies volatility and liquidates retail traders first.",
     f"{SITE_URL}"),
    (8, 6, "💡 *Your biggest security risk is you*",
     "The biggest security risk in crypto isn't hackers — it's you. Phishing, weak passwords, and bad OPSEC cause more losses than all protocol exploits combined. Your discipline matters most.",
     f"{SITE_URL}/guide/sicurezza-wallet.html"),
]

# Analisi per slot pranzo — indexate per settimana e giorno
WEEKLY_ANALYSES = [
    # Settimana 1: Exchange
    (1, 0, "📈 *Le 3 exchange più economiche per italiani nel 2026*",
     "Binance: 0,1% spot. Kraken: 0,16% maker. Bybit: 0,1%. Per DCA mensile <€500 la differenza è minima. Per trading attivo >€10.000/mese può fare €200+/anno. Verifica anche la fee di prelievo EUR.",
     f"{SITE_URL}/guide/migliore-exchange-2026.html"),
    (1, 1, "📈 *Come leggere l'order book e piazzare un ordine limite*",
     "Order book = lista di tutti gli ordini buy/sell in attesa. Bid = chi vuole comprare. Ask = chi vuole vendere. Ordine limite: scegli tu il prezzo. Ordine market: esegui subito al miglior prezzo disponibile.",
     f"{SITE_URL}/guide/migliore-exchange-2026.html"),
    (1, 2, "📈 *Coinbase vs Kraken — quale scegliere per principianti?*",
     "Coinbase: più semplice, app migliore, costi più alti (fino a 1,5%). Kraken: interfaccia più tecnica, costi inferiori (0,16% maker), supporto migliore. Per primo acquisto: Coinbase. Per uso continuativo: Kraken.",
     f"{SITE_URL}/guide/migliore-exchange-2026.html"),
    (1, 3, "📈 *Volume BTC — cosa dice il mercato questa settimana*",
     "Volume alto + prezzo stabile = accumulo (bullish). Volume alto + prezzo scende = distribuzione (bearish). Volume basso = indecisione. Controlla sempre il volume prima di interpretare il movimento del prezzo.",
     f"{SITE_URL}"),
    (1, 4, "📈 *Analisi settimanale: support/resistance chiave*",
     "Guarda i livelli storici dove il prezzo ha rimbalzato più volte. Supporto = pavimento. Resistenza = soffitto. Quando una resistenza viene rotta diventa supporto (e viceversa). Aggiorna i tuoi livelli ogni domenica.",
     f"{SITE_URL}"),
    (1, 5, "📈 *Sentiment community questa settimana*",
     "Fear & Greed Index, dati on-chain, sentiment Twitter/Reddit: tre segnali da leggere insieme, non separati. Euforia estrema = attenzione. Paura estrema = opportunità storica. Non agire mai solo su uno.",
     f"{SITE_URL}"),
    (1, 6, "📈 *Cosa aspettarsi la prossima settimana*",
     "Evento macro in arrivo? (Fed, CPI, NFP) — Bitcoin reagisce. Scadenza options? — volatilità attesa. Stagionalità: maggio storico 'sell in May'? Prepara il tuo piano prima, non durante il movimento.",
     f"{SITE_URL}"),
    # Settimana 2: Fondamenti
    (2, 0, "📈 *Blockchain: perché è rivoluzionaria*",
     "Per la prima volta nella storia è possibile trasferire valore digitale senza intermediari. Nessuna banca, nessun notaio, nessun governo. Il codice matematico garantisce la fiducia. Questo è il cambio di paradigma.",
     f"{SITE_URL}/guide/cose-bitcoin.html"),
    (2, 1, "📈 *Hash rate ai massimi storici — cosa significa*",
     "Hash rate = potenza computazionale della rete Bitcoin. Più alto = rete più sicura. Oggi >600 EH/s. Ogni attacco del 51% costerebbe miliardi. Nessun governo o corporation può permetterselo.",
     f"{SITE_URL}/guide/cose-bitcoin.html"),
    (2, 2, "📈 *Bitcoin Cash vs Bitcoin: cosa è rimasto dopo il fork*",
     "Nel 2017 Roger Ver propose block più grandi → hard fork → Bitcoin Cash. Risultato: BTC mantiene 97%+ del valore. BCH marginale. La lezione: il network effect di Bitcoin è quasi impossibile da sfidare.",
     f"{SITE_URL}/guide/cose-bitcoin.html"),
    (2, 3, "📈 *Stock-to-flow model — funziona ancora?*",
     "S2F = rapporto tra stock esistente e nuova produzione. Più alto = più scarso = più prezioso. Oro: S2F 60. Bitcoin post-halving 2024: S2F 120. Il modello ha predetto i cicli passati ma non è infallibile.",
     f"{SITE_URL}/guide/bitcoin-halving.html"),
    (2, 4, "📈 *Satoshi Nakamoto — il genio anonimo*",
     "Whitepaper: 31 ottobre 2008. Primo blocco: 3 gennaio 2009. Ultimo messaggio: 2010. Da allora sparito. Si stima abbia ~1 milione di BTC mai mossi (~€64 miliardi oggi). Chi è? Nessuno lo sa con certezza.",
     f"{SITE_URL}/guide/cose-bitcoin.html"),
    (2, 5, "📈 *Regolamentazione globale: dove siamo nel 2026*",
     "UE: MiCA in vigore. USA: ETF Bitcoin approvati, SEC più aperta. Cina: ban teorico ma mining continua. El Salvador: BTC moneta legale. Tendenza globale: regolamentare non vietare.",
     f"{SITE_URL}/guide/tassazione-crypto-italia.html"),
    (2, 6, "📈 *Prossimo ciclo: previsioni 2026-2028*",
     "Post-halving 2024 storicamente = bull run 12-18 mesi dopo. Nuovi ETF attirano istituzionali. Domanda crescente, offerta dimezzata. Consensus range analisti: $120k-$250k entro 2025-2026. DYOR.",
     f"{SITE_URL}/guide/bitcoin-halving.html"),
    # Settimana 3: Sicurezza
    (3, 0, "📈 *On-chain: quanti BTC in cold storage?*",
     "Exchange outflows (BTC che escono dagli exchange verso wallet privati) = segnale bullish. Quando gli utenti spostano in self-custody, non intendono vendere presto. Monitora glassnode.com per questi dati.",
     f"{SITE_URL}/guide/sicurezza-wallet.html"),
    (3, 1, "📈 *I 5 hack exchange più grandi della storia*",
     "Mt.Gox (2014): 850k BTC. Bitfinex (2016): 120k BTC. Binance (2019): 7k BTC. FTX (2022): non hack ma frode. Lesson: no exchange è invulnerabile. Self-custody per grandi importi, sempre.",
     f"{SITE_URL}/guide/sicurezza-wallet.html"),
    (3, 2, "📈 *HODL waves — chi sta accumulando?*",
     "HODL waves mostrano quanti BTC non si muovono da X anni. Più alto il % di monete 'dormenti', più l'offerta è bloccata. Nel 2024: 70%+ dei BTC non si muovono da >1 anno. Bullish supply shock.",
     f"{SITE_URL}"),
    (3, 3, "📈 *Casi studio: come vengono rubati i crypto*",
     "1) Exchange hackerato. 2) Phishing su sito fake. 3) Malware che sostituisce indirizzo clipboard. 4) SIM swap per rubare 2FA SMS. 5) Seed phrase digitata su sito non verificato. Tutti prevenibili.",
     f"{SITE_URL}/guide/sicurezza-wallet.html"),
    (3, 4, "📈 *Come minimizzare le tasse sulle crypto legalmente*",
     "1) Tieni BTC >1 anno (nessun vantaggio fiscale IT per ora, ma possibile futura riforma). 2) Compensa plusvalenze con minusvalenze nello stesso anno. 3) Tieni registro preciso di ogni acquisto/vendita.",
     f"{SITE_URL}/guide/tassazione-crypto-italia.html"),
    (3, 5, "📈 *Regolamentazione EU vs USA nel 2026*",
     "EU MiCA: exchange devono avere CASP authorization entro luglio 2026. Bybit, OKX, alcune piattaforme hanno già avvertito di possibili restrizioni per utenti EU. Verifica che il tuo exchange sia compliant.",
     f"{SITE_URL}/magazine/mica-deadline-july-2026.html"),
    (3, 6, "📈 *Cosa guardare la settimana prossima*",
     "Scadenze tasse? Aggiornamenti exchange locali? Nuovi requisiti KYC? La settimana 3 è quella della sicurezza: controlla che tutti i tuoi account abbiano 2FA attivo e password uniche.",
     f"{SITE_URL}/guide/sicurezza-wallet.html"),
    # Settimana 4: Strategie
    (4, 0, "📈 *DCA vs lump sum — i dati storici*",
     "Lump sum (tutto subito) batte DCA nel 67% dei casi su mercati in rialzo secolare. Ma DCA riduce stress e rischio di timing sbagliato. Per principianti: DCA. Per esperti con liquidità: considera lump sum su forti correzioni.",
     f"{SITE_URL}/guide/come-comprare-bitcoin-2026.html"),
    (4, 1, "📈 *Correlazione BTC con S&P 500 — 2024-2026*",
     "Dopo il crollo 2022 la correlazione è rimasta alta. BTC si muove come tech stock in momenti di stress macro. Divergenza: quando BTC sale con borsa piatta = domanda crypto-specifica. Tienila d'occhio.",
     f"{SITE_URL}"),
    (4, 2, "📈 *Portfolio allocation: quanta crypto è sensata?*",
     "Studi accademici: già il 2-5% di BTC in un portafoglio tradizionale (azioni+bond) migliora il Sharpe ratio negli ultimi 10 anni. Con volatilità alta, rischio aumenta proporzionalmente. Calibra sul tuo profilo.",
     f"{SITE_URL}/guide/come-comprare-bitcoin-2026.html"),
    (4, 3, "📈 *Whale watching — cosa fanno i grandi HODLer*",
     "Quando wallet da 1000+ BTC accumulano in silenzio = possibile pre-rally. Quando distribuiscono a whale più piccole = possibile top. Fonti: Whale Alert su Twitter, Glassnode, CryptoQuant.",
     f"{SITE_URL}"),
    (4, 4, "📈 *Analisi finale del mese*",
     "Come si chiude aprile 2026? Ogni fine mese: rivedi il tuo portafoglio, aggiorna il foglio tasse, verifica che tutti i tuoi wallet siano ancora sicuri. Prossimo halving: 2028. Sei posizionato?",
     f"{SITE_URL}"),
    (4, 5, "📈 *Media e FUD — come filtrare le notizie*",
     "Regola: se un'headline ti fa venire voglia di agire immediatamente, aspetta 24 ore. 90% delle notizie 'urgenti' crypto non cambiano il trend di lungo periodo. Reagisci ai fondamentali, non alle headline.",
     f"{SITE_URL}"),
    (4, 6, "📈 *Recap mese + prospettive*",
     "Fine del ciclo di 4 settimane. Hai imparato: exchange, fondamentali, sicurezza, strategie. Il passo successivo: scegli UN'azione da implementare questa settimana. Piccoli passi, costanti.",
     f"{SITE_URL}/guide/come-comprare-bitcoin-2026.html"),
]

# Sondaggi per settimana tematica (2 per settimana: mercoledì e domenica)
WEEKLY_POLLS = {
    1: [  # Settimana 1: Exchange
        {"question": "🏦 Quale exchange usi per comprare Bitcoin?",
         "options": ["Binance", "Coinbase", "Kraken", "Bybit / altro"]},
        {"question": "✅ Ti senti sicuro con il tuo exchange attuale?",
         "options": ["Sì, molto", "Abbastanza", "Un po' preoccupato", "Sto valutando alternative"]},
    ],
    2: [  # Settimana 2: Fondamentali
        {"question": "📚 Quanto conosci il funzionamento di Bitcoin?",
         "options": ["Molto bene", "Le basi", "Solo il prezzo", "Sto ancora imparando"]},
        {"question": "📈 Quale ciclo Bitcoin ti aspetti entro 2026?",
         "options": ["Nuovo ATH >$150k", "Laterale $60-80k", "Correzione <$50k", "Non so prevedere"]},
    ],
    3: [  # Settimana 3: Sicurezza
        {"question": "🔒 Come custodisci i tuoi BTC?",
         "options": ["Solo exchange", "Hardware wallet", "Software wallet offline", "Mix di soluzioni"]},
        {"question": "📋 Hai già dichiarato le crypto al Fisco italiano?",
         "options": ["Sì, sempre", "Solo l'anno scorso", "No, non sapevo", "Non ho plusvalenze"]},
    ],
    4: [  # Settimana 4: Strategie
        {"question": "💰 Qual è la tua strategia principale su Bitcoin?",
         "options": ["DCA mensile", "Buy & hold unica volta", "Trading attivo", "Nessuna strategia"]},
        {"question": "⏳ Per quanto tempo intendi tenere i tuoi BTC?",
         "options": ["Meno di 1 anno", "1-3 anni", "3-10 anni", "A vita (never sell)"]},
    ],
}

# Promo guide per settimana (venerdì sera)
WEEKLY_PROMOS = {
    1: ("📖 *Guida 2026: migliori exchange per comprare Bitcoin in Italia*",
        "Confronto commissioni, sicurezza, KYC, metodi di pagamento. Verifica quale exchange è davvero MiCA-compliant.",
        f"{SITE_URL}/guide/migliore-exchange-2026.html"),
    2: ("📖 *Cos'è Bitcoin — guida completa per principianti*",
        "Blockchain, mining, halving, supply cap: tutto quello che devi capire prima di investire. Senza parolacce tecniche.",
        f"{SITE_URL}/guide/cose-bitcoin.html"),
    3: ("📖 *Tasse crypto in Italia 2026 — guida completa*",
        "26% sulle plusvalenze, soglia €2.000, dichiarazione 730, quadro RW. Tutto quello che devi sapere.",
        f"{SITE_URL}/guide/tassazione-crypto-italia.html"),
    4: ("📖 *Come proteggere i tuoi Bitcoin — sicurezza wallet*",
        "Seed phrase, hardware wallet, 2FA, phishing: la checklist completa per non perdere mai i tuoi BTC.",
        f"{SITE_URL}/guide/sicurezza-wallet.html"),
}


# ── DB State ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(STATE_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS state (
        key TEXT PRIMARY KEY, value TEXT
    )""")
    conn.commit()
    return conn


def get_state(conn, key, default=0):
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return int(row[0]) if row else default


def set_state(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, str(value)))
    conn.commit()


# ── Telegram API ──────────────────────────────────────────────────────────────
def tg_post(method, data):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    payload = json.dumps(data).encode()
    req = urllib.request.Request(url, data=payload,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[TG ERROR] {method}: {e}", file=sys.stderr)
        return {"ok": False, "error": str(e)}


def send_message(text, disable_preview=True):
    return tg_post("sendMessage", {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": disable_preview,
    })


def send_poll(question, options):
    return tg_post("sendPoll", {
        "chat_id": CHANNEL_ID,
        "question": question,
        "options": options,
        "is_anonymous": True,
    })


def send_photo_file(image_path, caption):
    """Upload local image file with caption via multipart form."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    boundary = "PinkyBotBoundary42"
    with open(image_path, "rb") as f:
        image_data = f.read()

    body = b""
    for name, value in [("chat_id", CHANNEL_ID), ("caption", caption), ("parse_mode", "Markdown")]:
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += f"{value}\r\n".encode()
    # file field
    body += f"--{boundary}\r\n".encode()
    fname = Path(image_path).name
    body += f'Content-Disposition: form-data; name="photo"; filename="{fname}"\r\n'.encode()
    body += b"Content-Type: image/png\r\n\r\n"
    body += image_data
    body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[TG ERROR] sendPhoto file: {e}", file=sys.stderr)
        return {"ok": False, "error": str(e)}


def send_photo_url(image_url, caption):
    """Send photo from public URL with caption."""
    return tg_post("sendPhoto", {
        "chat_id": CHANNEL_ID,
        "photo": image_url,
        "caption": caption,
        "parse_mode": "Markdown",
    })


def pollinations_url(prompt, seed=42, width=1200, height=630):
    """Return a deterministic Pollinations.ai image URL (same seed = same image)."""
    import urllib.parse
    encoded = urllib.parse.quote(prompt)
    return (f"https://image.pollinations.ai/prompt/{encoded}"
            f"?width={width}&height={height}&nologo=true&seed={seed}&model=flux")


def generate_btc_chart(prices_current=None):
    """Generate a 7-day BTC/EUR price chart. Returns local PNG path or None on failure."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from datetime import datetime as _dt

        url = f"{COINGECKO_URL}/coins/bitcoin/market_chart?vs_currency=eur&days=7&interval=hourly"
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())

        raw = data["prices"]  # [[ms, price], ...]
        times = [_dt.fromtimestamp(p[0] / 1000) for p in raw]
        values = [p[1] for p in raw]

        fig, ax = plt.subplots(figsize=(12, 6))
        fig.patch.set_facecolor("#0D0D0D")
        ax.set_facecolor("#0D0D0D")

        ax.fill_between(times, values, alpha=0.12, color="#F7931A")
        ax.plot(times, values, color="#F7931A", linewidth=2.5, zorder=5)
        ax.scatter([times[-1]], [values[-1]], color="#F7931A", s=100, zorder=6)

        # Last price annotation
        ax.annotate(
            f"  €{values[-1]:,.0f}",
            xy=(times[-1], values[-1]),
            color="#F7931A", fontsize=13, fontweight="bold", va="center",
        )

        ax.tick_params(colors="#666666", labelsize=11)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
        ax.xaxis.set_major_locator(mdates.DayLocator())
        ax.yaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda x, _: f"€{x:,.0f}")
        )
        for spine in ax.spines.values():
            spine.set_color("#222222")
        ax.grid(True, color="#1c1c1c", linewidth=0.8, linestyle="--")

        ax.set_title("Bitcoin  ·  7 giorni", color="#F7931A", fontsize=17,
                     fontweight="bold", pad=16, loc="left")
        fig.text(0.99, 0.02, "BitcoinMarket.net", color="#333333", fontsize=10,
                 ha="right", va="bottom", style="italic")

        plt.tight_layout()
        path = "/tmp/bm_chart_morning.png"
        plt.savefig(path, dpi=150, bbox_inches="tight", facecolor="#0D0D0D")
        plt.close()
        return path
    except Exception as e:
        print(f"[Chart ERROR] {e}", file=sys.stderr)
        return None


# ── CoinGecko ─────────────────────────────────────────────────────────────────
def get_prices():
    try:
        url = (f"{COINGECKO_URL}/simple/price"
               f"?ids=bitcoin,ethereum&vs_currencies=eur,usd&include_24hr_change=true")
        with urllib.request.urlopen(url, timeout=10) as resp:
            d = json.loads(resp.read())
        return {
            "btc_eur": d["bitcoin"]["eur"],
            "btc_usd": d["bitcoin"]["usd"],
            "btc_chg": d["bitcoin"]["eur_24h_change"],
            "eth_eur": d["ethereum"]["eur"],
            "eth_chg": d["ethereum"]["eur_24h_change"],
        }
    except Exception as e:
        print(f"[CoinGecko ERROR] {e}", file=sys.stderr)
        return None


def price_card(prices):
    b_ico = "🟢" if prices["btc_chg"] >= 0 else "🔴"
    e_ico = "🟢" if prices["eth_chg"] >= 0 else "🔴"
    b_s = "+" if prices["btc_chg"] >= 0 else ""
    e_s = "+" if prices["eth_chg"] >= 0 else ""
    now = datetime.now().strftime("%H:%M")
    return (
        f"📊 *Mercato crypto — {now}*\n\n"
        f"{b_ico} *Bitcoin (BTC)*\n"
        f"   €{prices['btc_eur']:,.0f}  •  ${prices['btc_usd']:,.0f}\n"
        f"   {b_s}{prices['btc_chg']:.2f}% nelle ultime 24h\n\n"
        f"{e_ico} *Ethereum (ETH)*\n"
        f"   €{prices['eth_eur']:,.0f}\n"
        f"   {e_s}{prices['eth_chg']:.2f}% nelle ultime 24h\n\n"
        f"_Dati: CoinGecko_  •  [Confronta exchange →]({SITE_URL})"
    )


# ── Slot Handlers ─────────────────────────────────────────────────────────────
def get_week_and_day():
    """Returns (week_1_4, weekday_0_6) based on current date cycling through 4-week plan."""
    now = datetime.now()
    # Week 1 started April 30, 2026 (launch week)
    from datetime import date
    launch = date(2026, 4, 30)
    days_since = (now.date() - launch).days
    if days_since < 0:
        days_since = 0
    week_num = (days_since // 7) % 8 + 1  # cycles 1→2→…→8→1→...
    weekday = now.weekday()  # 0=lun, 6=dom
    return week_num, weekday


def get_tip_for_today(conn):
    """Get the tip for today's week/day from WEEKLY_TIPS."""
    week, day = get_week_and_day()
    candidates = [(w, d, t, b, l) for w, d, t, b, l in WEEKLY_TIPS if w == week and d == day]
    if candidates:
        return candidates[0][2], candidates[0][3], candidates[0][4]
    # Fallback: any tip for this week
    fallback = [(w, d, t, b, l) for w, d, t, b, l in WEEKLY_TIPS if w == week]
    if fallback:
        idx = get_state(conn, "tip_fallback_idx")
        item = fallback[idx % len(fallback)]
        if not True:  # don't increment here, caller will
            set_state(conn, "tip_fallback_idx", idx + 1)
        return item[2], item[3], item[4]
    return WEEKLY_TIPS[0][2], WEEKLY_TIPS[0][3], WEEKLY_TIPS[0][4]


def get_analysis_for_today(conn):
    """Get the analysis for today's week/day from WEEKLY_ANALYSES."""
    week, day = get_week_and_day()
    candidates = [(w, d, t, b, l) for w, d, t, b, l in WEEKLY_ANALYSES if w == week and d == day]
    if candidates:
        return candidates[0][2], candidates[0][3], candidates[0][4]
    fallback = [(w, d, t, b, l) for w, d, t, b, l in WEEKLY_ANALYSES if w == week]
    if fallback:
        idx = get_state(conn, "analysis_fallback_idx")
        return fallback[idx % len(fallback)][2], fallback[idx % len(fallback)][3], fallback[idx % len(fallback)][4]
    return WEEKLY_ANALYSES[0][2], WEEKLY_ANALYSES[0][3], WEEKLY_ANALYSES[0][4]


def slot_morning(conn, dry_run=False):
    msgs = []
    import random
    intro = random.choice(MORNING_INTROS)

    # 1. Price card + chart reale BTC 7 giorni
    prices = get_prices()
    caption = (f"{intro}\n\n" + price_card(prices)) if prices else (
        f"{intro}\n\n📊 Dati non disponibili.\n[bitcoinmarket.net]({SITE_URL})")

    chart_path = None if dry_run else generate_btc_chart()
    if chart_path:
        msgs.append(("photo_file", chart_path, caption))
    else:
        img_url = pollinations_url(
            "bitcoin price chart dark background orange glow professional minimal trading dashboard", seed=101)
        msgs.append(("photo_url", img_url, caption))

    # 2. Tip del giorno dal calendario settimanale
    title, body, link = get_tip_for_today(conn)
    tip_img = pollinations_url(
        "crypto education infographic dark background bitcoin minimal professional clean", seed=202)
    msgs.append(("photo_url", tip_img, f"{title}\n\n{body}\n\n[Approfondisci →]({link})"))

    return msgs


def slot_lunch(conn, dry_run=False):
    msgs = []

    # Analisi del giorno dal calendario settimanale
    title, body, link = get_analysis_for_today(conn)
    img_url = pollinations_url(
        "cryptocurrency exchange trading platform dark background professional analytics minimal orange accent",
        seed=303)
    msgs.append(("photo_url", img_url, f"{title}\n\n{body}\n\n[Leggi la guida →]({link})"))

    return msgs


def slot_tip(conn, dry_run=False):
    """Solo tip educativo, testo puro, niente immagini. 2x/settimana."""
    title, body, link = get_tip_for_today(conn)
    text = f"{title}\n\n{body}\n\n[Read more →]({link})"
    return [("text", text)]


def slot_evening(conn, dry_run=False):
    msgs = []
    week, weekday = get_week_and_day()

    if weekday in (2, 6):  # mercoledì o domenica → sondaggio
        poll_idx = 0 if weekday == 2 else 1
        poll = WEEKLY_POLLS[week][poll_idx]
        # Immagine seria per poll
        img_url = pollinations_url(
            "crypto community poll dark background bitcoin ethereum minimal professional orange glow", seed=404)
        msgs.append(("photo_url", img_url,
                     f"*{poll['question']}*\n\n📊 Vota qui sotto 👇"))
        msgs.append(("poll", poll))

    elif weekday == 4:  # venerdì → promo guida tematica
        title, body, link = WEEKLY_PROMOS[week]
        img_url = pollinations_url(
            "open book crypto guide bitcoin dark background professional minimal orange accent learning",
            seed=505)
        msgs.append(("photo_url", img_url, f"{title}\n\n{body}\n\n[Leggi →]({link})"))

    else:
        # Altri giorni: analisi extra
        idx = get_state(conn, "evening_extra_idx")
        all_analyses = [(t, b, l) for _, _, t, b, l in WEEKLY_ANALYSES if _ == week]
        if all_analyses:
            item = all_analyses[idx % len(all_analyses)]
            img_url = pollinations_url(
                "bitcoin cryptocurrency market analysis dark background professional chart minimal", seed=606)
            msgs.append(("photo_url", img_url,
                         f"{item[0]}\n\n{item[1]}\n\n[Approfondisci →]({item[2]})"))
            if not dry_run:
                set_state(conn, "evening_extra_idx", idx + 1)

    return msgs


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", choices=["morning", "lunch", "evening", "tip", "test"], required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    dry_run = args.dry_run or args.slot == "test"
    conn = init_db()
    slot_map = {"morning": slot_morning, "lunch": slot_lunch,
                "evening": slot_evening, "tip": slot_tip, "test": slot_tip}

    messages = slot_map[args.slot](conn, dry_run=dry_run)

    for item in messages:
        msg_type = item[0]
        if dry_run:
            print(f"\n── DRY RUN [{msg_type}] ──")
            if msg_type == "poll":
                poll = item[1]
                print(f"POLL: {poll['question']}")
                for o in poll["options"]: print(f"  • {o}")
            elif msg_type == "photo_file":
                _, path, caption = item
                print(f"PHOTO (file): {path}")
                print(caption)
            elif msg_type == "photo_url":
                _, url, caption = item
                print(f"PHOTO (url): {url[:80]}...")
                print(caption)
            else:
                print(item[1])
        else:
            if msg_type == "poll":
                poll = item[1]
                result = send_poll(poll["question"], poll["options"])
            elif msg_type == "photo_file":
                _, path, caption = item
                result = send_photo_file(path, caption)
                if not result.get("ok"):
                    # Fallback to text if photo fails
                    result = send_message(caption)
            elif msg_type == "photo_url":
                _, url, caption = item
                result = send_photo_url(url, caption)
                if not result.get("ok"):
                    result = send_message(caption)
            else:
                result = send_message(item[1])

            status = "✅ OK" if result.get("ok") else f"❌ ERROR: {result}"
            print(f"[{args.slot}] {msg_type}: {status}")

    conn.close()


if __name__ == "__main__":
    main()
