"""
MacroAgent — Real-time News Intelligence Layer for BNF Engine V19
═════════════════════════════════════════════════════════════════
Runs a dedicated background thread polling multiple Indian financial
news RSS feeds every 30 seconds. When a Universe stock is mentioned
in a headline with strong sentiment, it cross-validates against
LIVE price movement + volume spike before emitting a signal.

Flow:
  1. Background thread polls RSS feeds every 30s (concurrent via ThreadPool)
  2. Parses headlines → matches against Universe symbols
  3. Scores sentiment using curated Indian financial keyword dictionaries
  4. Before emitting signal: confirms price direction + RVOL spike
  5. Pushes confirmed signals into a thread-safe queue
  6. Main engine loop drains the queue every tick cycle
"""

import time
import threading
import requests
import xml.etree.ElementTree as ET
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from core.api_server import log_agent_action, log_news_headline

# ══════════════════════════════════════════════════════════════
#  CURATED SENTIMENT DICTIONARIES — Indian Financial Markets
# ══════════════════════════════════════════════════════════════

# --- STRONG BULLISH (high confidence → immediate LONG) ---
STRONG_BULLISH = [
    # Earnings & Financial Performance
    "beats estimate", "profit surges", "profit jumps", "revenue surges",
    "record profit", "record revenue", "strong earnings", "beats expectations",
    "margin expansion", "ebitda growth", "pat jumps", "pat surges",
    "net profit rises", "net profit jumps", "top line grows",
    # Corporate Actions
    "dividend declared", "bonus shares", "buyback", "share buyback",
    "stock split", "rights issue",
    # Business Wins
    "bags order", "bagged order", "wins contract", "wins order",
    "new order", "order inflow", "order book", "deal signed",
    "partnership with", "tie-up with", "collaboration with",
    "acquisition", "acquires", "takeover bid",
    # Analyst & Rating
    "upgrade", "target raised", "initiates buy", "outperform",
    "overweight", "accumulate", "strong buy", "top pick",
    "adds to conviction list",
    # Regulatory & Government
    "approval received", "license granted", "regulatory clearance",
    "fda approval", "dcgi approval", "sebi approval",
    "government contract", "pli scheme", "subsidy approved",
    # Market Action
    "all-time high", "52-week high", "breakout", "rally",
    "surges", "soars", "spikes", "zooms",
    # Sector / Macro Positive
    "rate cut", "repo rate cut", "rbi cuts", "stimulus",
    "reform", "fdi inflow", "rupee strengthens",
    "inflation eases", "gdp beats", "iip rises",
]

# --- STRONG BEARISH (high confidence → immediate SHORT) ---
STRONG_BEARISH = [
    # Earnings & Financial Loss
    "profit falls", "profit drops", "profit declines", "revenue falls",
    "revenue misses", "misses estimate", "weak earnings", "disappointing results",
    "margin contraction", "ebitda drops", "pat falls", "pat declines",
    "net loss", "net profit falls", "widening losses", "loss widens",
    # Corporate Governance / Fraud
    "fraud", "scam", "cbi raid", "ed raid", "ed attaches", "sebi ban",
    "insider trading", "accounting fraud", "money laundering",
    "promoter pledge", "pledge increases", "promoter selling",
    "auditor resigns", "cfo resigns", "ceo resigns", "md resigns",
    "whistleblower", "corporate governance issue",
    # Analyst & Rating
    "downgrade", "target cut", "initiates sell", "underperform",
    "underweight", "reduce", "strong sell", "removes from list",
    # Regulatory & Legal
    "penalty imposed", "fine by sebi", "nclat order", "court order against",
    "ban imposed", "suspension", "delisting", "gst notice",
    "tax evasion", "income tax raid",
    # Market Action
    "52-week low", "crashes", "plunges", "slumps", "tanks",
    "falls sharply", "heavy selling", "circuit breaker hit",
    "lower circuit", "bloodbath",
    # Business Negative
    "order cancelled", "contract terminated", "plant shutdown",
    "layoffs", "job cuts", "retrenchment", "default",
    "debt restructuring", "credit downgrade", "npa rises",
    # Sector / Macro Negative
    "rate hike", "repo rate hike", "rbi hikes", "tightening",
    "rupee weakens", "rupee falls", "crude surges", "oil spikes",
    "inflation surges", "gdp misses", "iip falls",
    "sanctions", "trade war", "tariff hike",
    # Geopolitical
    "war", "military conflict", "border tensions", "airstrike",
    "terror attack", "geopolitical risk", "global recession fears",
]

# Minimum thresholds for price + volume confirmation
MIN_PRICE_MOVE_PCT = 0.005    # Stock must have moved >=0.5% in news direction
MAX_PRICE_MOVE_PCT = 0.030    # If stock already moved >3%, news is priced in — SKIP
MIN_RVOL_CONFIRM   = 1.5      # Relative Volume must be >=1.5x average
MAX_SL_PCT         = 0.015    # Hard max SL cap: 1.5% of entry for news trades
POLL_INTERVAL_SEC  = 5        # Poll every 5 seconds (HTTP caching prevents waste)


class MacroAgent:
    """
    Real-time news intelligence layer.
    Runs a persistent background daemon thread that independently polls
    RSS feeds every 30 seconds, scores sentiment, confirms with price/volume,
    and pushes validated signals into a thread-safe queue for the main loop.
    """

    def __init__(self, data_agent):
        self.data = data_agent
        self._seen_urls = set()       # Deduplication: URLs we've already processed
        self._seen_urls_max = 5000    # Cap to prevent unbounded growth on 24/7 runs
        self._signal_queue = deque()  # Thread-safe queue for confirmed signals
        self._sentiment_cache = {}    # symbol -> {"bias": "BULLISH"|"BEARISH", "timestamp": timestamp}
        self._running = False
        self._thread = None
        self._feed_etags = {}         # URL -> ETag for HTTP conditional requests
        self._feed_modified = {}      # URL -> Last-Modified for HTTP conditional requests

        # Persistent HTTP session with connection pooling (reuse TCP connections)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "BNF-Engine/1.0 (RSS Reader)"})
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10, pool_maxsize=10, max_retries=1
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        # 5 ultra-verified, LIVE purely Indian RSS feeds (with active 2026 pubDates)
        self.feeds = [
            # The Hindu BusinessLine (Super fresh, live updates)
            "https://www.thehindubusinessline.com/markets/feeder/default.rss",
            # Economic Times (Live)
            "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
            "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
            # CNBC TV18 (Tier-1 Indian Equities)
            "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml",
            # Livemint (HT Media — live)
            "https://www.livemint.com/rss/markets",
        ]

    # ── Lifecycle ──────────────────────────────────────────────

    def start(self):
        """Start the background news polling daemon."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="MacroAgent")
        self._thread.start()
        print("[MacroAgent] Background news scanner started (5s poll, 9 feeds)")
        log_agent_action("MacroAgent", "STARTED", "Background RSS polling active (5s cycle, 2-feed confirmation)")
        log_news_headline("MacroAgent Intelligence System Initialized", "SYSTEM", "", "neutral")

    def stop(self):
        """Gracefully stop the background thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        print("[MacroAgent] ⛔ Background news scanner stopped")

    # ── Main Loop (runs in background thread) ──────────────────

    def _poll_loop(self):
        """Persistent loop: fetch → parse → score → confirm → queue."""
        while self._running:
            try:
                # Prune dedup set to prevent memory leak on 24/7 runs
                if len(self._seen_urls) > self._seen_urls_max:
                    self._seen_urls.clear()
                self._fetch_and_process()
            except Exception as e:
                print(f"[MacroAgent] Poll cycle error: {e}")
            time.sleep(POLL_INTERVAL_SEC)

    def _fetch_and_process(self):
        """Fetch all feeds concurrently, parse items, score, confirm, queue."""
        raw_items = []

        # Concurrent fetch of all RSS feeds
        with ThreadPoolExecutor(max_workers=len(self.feeds)) as pool:
            futures = {pool.submit(self._fetch_feed, url): url for url in self.feeds}
            for future in as_completed(futures):
                try:
                    items = future.result()
                    if items:
                        raw_items.extend(items)
                except Exception:
                    pass

        if not raw_items:
            return

        # Build company name → (token, symbol) mapping from live universe
        # NOTE: mapping may be empty on weekends/holidays/pre-login — that's fine,
        # we still push all sentiment headlines to the dashboard feed below.
        mapping = self._build_mapping()

        # We will collect 'votes' per symbol to enforce >1 feed validation
        symbol_votes = {} # symbol -> {"bull": set(feed_urls), "bear": set(feed_urls), "titles": []}

        for title, desc, link, source_url, image in raw_items:
            if link in self._seen_urls:
                continue
            self._seen_urls.add(link)

            combined = (title + " " + desc).lower()
            src_name = source_url.split("/")[2].replace("www.", "")

            # Score sentiment on the raw headline (always, regardless of universe)
            bull_score = sum(1 for phrase in STRONG_BULLISH if phrase in combined)
            bear_score = sum(1 for phrase in STRONG_BEARISH if phrase in combined)

            # ── Always push sentiment-bearing headlines to dashboard feed ──
            # This ensures the News Feed tab shows content even on weekends/holidays
            # when no universe mapping exists.
            matched_symbol = ""

            if mapping:
                # Try to match against universe symbols
                for name_key, info in mapping.items():
                    if name_key.lower() in combined:
                        matched_symbol = info["symbol"]
                        token = info["token"]

                        if matched_symbol not in symbol_votes:
                            symbol_votes[matched_symbol] = {"bull": set(), "bear": set(), "titles": [], "token": token}

                        if bull_score > 0 and bear_score == 0:
                            symbol_votes[matched_symbol]["bull"].add(source_url)
                            symbol_votes[matched_symbol]["titles"].append(title)
                        elif bear_score > 0 and bull_score == 0:
                            symbol_votes[matched_symbol]["bear"].add(source_url)
                            symbol_votes[matched_symbol]["titles"].append(title)
                        break  # First symbol match wins

            # Push to dashboard feed: any headline with clear sentiment
            if bull_score > 0 and bear_score == 0:
                if matched_symbol:
                    self._sentiment_cache[matched_symbol] = {"bias": "BULLISH", "timestamp": time.time()}
                log_news_headline(title, src_name, matched_symbol, "bullish", link, image)
            elif bear_score > 0 and bull_score == 0:
                if matched_symbol:
                    self._sentiment_cache[matched_symbol] = {"bias": "BEARISH", "timestamp": time.time()}
                log_news_headline(title, src_name, matched_symbol, "bearish", link, image)
            elif bull_score == 0 and bear_score == 0:
                # No sentiment keywords — still show as neutral market news
                log_news_headline(title, src_name, "", "neutral", link, image)

        # ── Signal generation (only when universe + market data is live) ──
        for symbol, vote_data in symbol_votes.items():
            bull_feeds = len(vote_data["bull"])
            bear_feeds = len(vote_data["bear"])
            token = vote_data["token"]
            
            # Action immediately on the FIRST headline to hit the wire
            if bull_feeds >= 1 and bear_feeds == 0:
                headline = vote_data["titles"][0]
                sig = self._confirm_and_build(token, symbol, headline, is_short=False)
                if sig:
                    self._signal_queue.append(sig)
                    log_agent_action("MacroAgent", "BULL_CONFIRMED",
                                     f"{symbol}: {headline[:60]} | RVOL={sig.get('rvol',0):.1f}")
            elif bear_feeds >= 1 and bull_feeds == 0:
                headline = vote_data["titles"][0]
                sig = self._confirm_and_build(token, symbol, headline, is_short=True)
                if sig:
                    self._signal_queue.append(sig)
                    log_agent_action("MacroAgent", "BEAR_CONFIRMED",
                                     f"{symbol}: {headline[:60]} | RVOL={sig.get('rvol',0):.1f}")

    # ── Price + Volume Confirmation ────────────────────────────

    def _confirm_and_build(self, token: int, symbol: str, headline: str,
                           is_short: bool) -> dict:
        """
        CRITICAL GATE: Only emit a signal if the LIVE MARKET confirms the news.
        Checks:
          1. Price is moving in the expected direction (≥0.5%)
          2. Volume is spiking (RVOL ≥ 1.5x)
        If either fails, the news is noise — discard it.
        """
        if not self.data or not self.data.tick_store:
            return None

        current = self.data.tick_store.get_ltp_if_fresh(token)
        day_open = self.data.tick_store.get_day_open(token)
        if current <= 0 or day_open <= 0:
            return None

        # Price direction check
        price_change_pct = (current - day_open) / day_open

        if not is_short and price_change_pct < MIN_PRICE_MOVE_PCT:
            # Bullish news but price isn't rising → discard
            return None
        if is_short and price_change_pct > -MIN_PRICE_MOVE_PCT:
            # Bearish news but price isn't falling → discard
            return None

        # ── FIX #3: News Already Priced-In Check ──────────────────
        # If the stock has already moved >3% in the news direction,
        # the catalyst is fully absorbed. Chasing at this point means
        # buying the top / shorting the bottom after the institutional flow.
        abs_move = abs(price_change_pct)
        if abs_move > MAX_PRICE_MOVE_PCT:
            print(f"[MacroAgent] {symbol} already moved {abs_move*100:.1f}% — news priced in, SKIP")
            from core.api_server import log_agent_action
            log_agent_action("MacroAgent", "PRICED_IN_SKIP",
                             f"{symbol} moved {abs_move*100:.1f}% > {MAX_PRICE_MOVE_PCT*100:.0f}% cap")
            return None

        # Volume confirmation
        rvol = self.data.compute_rvol(token)
        if rvol < MIN_RVOL_CONFIRM:
            # No volume spike → news hasn't moved the market yet → discard
            return None

        # All checks passed — build the signal
        atr = self.data.daily_cache.get_atr(token) if self.data.daily_cache else 0
        if atr <= 0:
            atr = current * 0.02

        # ── FIX #4: Tighter Stops for News Trades ────────────────
        # News events create volatile two-way whipsaws. Use ATR-based stops
        # but cap at MAX_SL_PCT (1.5%) of entry price to prevent oversized losses.
        atr_stop = atr * 0.5
        max_stop = current * MAX_SL_PCT
        actual_stop_distance = min(atr_stop, max_stop)

        if is_short:
            stop   = round(current + actual_stop_distance, 2)
            target = round(current - atr * 2.0, 2)
        else:
            stop   = round(current - actual_stop_distance, 2)
            target = round(current + atr * 2.0, 2)

        return {
            "strategy":     "S8_MACRO_SHORT" if is_short else "S8_MACRO_LONG",
            "symbol":       symbol,
            "token":        token,
            "regime":       "NEWS_OVERRIDE",
            "entry_price":  current,
            "is_short":     is_short,
            "stop_price":   stop,
            "target_price": target,
            "partial_target": round((current + target) / 2, 2) if not is_short
                              else round((current + target) / 2, 2),
            "rvol":         round(rvol, 2),
            "atr":          round(atr, 2),
            "headline":     headline[:80],
        }

    # ── Veto API for ScannerAgent ────────────────────────────────
    
    def check_veto(self, symbol: str, is_long: bool, regime: str = "NORMAL") -> bool:
        """
        [V19 The 60% WR Protocol — Enhanced]
        Two-tier veto system:

        Tier 1 (ALL regimes): If the internet actively CONTRADICTS the trade direction
                              (e.g., going long on a stock with recent bearish headlines),
                              VETO the trade.

        Tier 2 (VOLATILE/CHOP/BEAR_PANIC): If sentiment is NEUTRAL (no recent news),
                but the regime is uncertain, VETO technical trades that lack confirming
                macro support. In unstable markets, "no news is bad news" — the absence
                of positive sentiment increases the probability of adverse moves.
        """
        cache = getattr(self, '_sentiment_cache', {}).get(symbol)

        # ── FIX #1: Aggressive Veto in Volatile Regimes ──────────
        # In uncertain regimes, require ACTIVE confirming sentiment.
        # If no sentiment exists at all, veto the trade.
        aggressive_regimes = {"VOLATILE", "CHOP", "BEAR_PANIC", "EXTREME_PANIC"}
        if regime in aggressive_regimes:
            if not cache:
                # No news at all for this symbol in an unstable market — risky
                print(f"[MacroVeto] Blocked {symbol} in {regime}: No confirming sentiment available.")
                return True
            elapsed_hours = (time.time() - cache["timestamp"]) / 3600.0
            if elapsed_hours > 2.0:
                # Sentiment is stale (>2h) in a volatile market — unreliable
                print(f"[MacroVeto] Blocked {symbol} in {regime}: Sentiment stale ({elapsed_hours:.1f}h ago).")
                return True

        if not cache:
            return False

        elapsed_hours = (time.time() - cache["timestamp"]) / 3600.0
        if elapsed_hours > 4.0:
            return False  # News is stale, let technicals ride

        bias = cache["bias"]
        if is_long and bias == "BEARISH":
            print(f"[MacroVeto] Blocked Long on {symbol}: Negative sentiment active.")
            return True
        if not is_long and bias == "BULLISH":
            print(f"[MacroVeto] Blocked Short on {symbol}: Positive sentiment active.")
            return True

        return False

    # ── Public API for main.py ─────────────────────────────────

    def drain_signals(self) -> list:
        """
        Called by main.py every tick cycle.
        Atomically drains all confirmed signals from the queue.
        Returns a list of signal dicts ready for execution.
        """
        signals = []
        while self._signal_queue:
            try:
                signals.append(self._signal_queue.popleft())
            except IndexError:
                break
        return signals

    # ── Internal Helpers ───────────────────────────────────────

    def _fetch_feed(self, url: str) -> list:
        """
        Fetch a single RSS feed with HTTP conditional caching.
        Uses ETag + If-Modified-Since to skip unchanged content.
        """
        try:
            # Mask as legitimate browser to clear 403 blocks (Bloomberg/BT)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            }
            if url in self._feed_etags:
                headers["If-None-Match"] = self._feed_etags[url]
            if url in self._feed_modified:
                headers["If-Modified-Since"] = self._feed_modified[url]

            resp = self._session.get(url, timeout=6, headers=headers)

            # 304 Not Modified — feed unchanged, skip parsing entirely
            if resp.status_code == 304:
                return []
            if resp.status_code != 200:
                return []

            # Cache response headers for next conditional request
            if "ETag" in resp.headers:
                self._feed_etags[url] = resp.headers["ETag"]
            if "Last-Modified" in resp.headers:
                self._feed_modified[url] = resp.headers["Last-Modified"]

            root = ET.fromstring(resp.content)
            items = []
            
            import re
            img_regex = re.compile(r'<img[^>]+src=["\'](.*?)["\']', re.IGNORECASE)
            
            for item in root.findall('.//item'):
                title_el = item.find('title')
                link_el  = item.find('link')
                desc_el  = item.find('description')
                
                title = title_el.text if title_el is not None and title_el.text else ""
                link  = link_el.text  if link_el is not None and link_el.text else ""
                desc  = desc_el.text  if desc_el is not None and desc_el.text else ""
                
                # Image extraction
                image = ""
                # 1. media:content
                media = item.find('.//{http://search.yahoo.com/mrss/}content')
                if media is not None and media.get('url'):
                    image = media.get('url')
                # 2. enclosure
                if not image:
                    enc = item.find('enclosure')
                    if enc is not None and enc.get('url') and 'image' in enc.get('type', ''):
                        image = enc.get('url')
                # 3. regex on description
                if not image and desc:
                    m = img_regex.search(desc)
                    if m:
                        image = m.group(1)
                
                if title:
                    items.append((title, desc, link, url, image))
            
            # The XML typically lists newest first.
            # We reverse it so the newest items are appended LAST,
            # meaning they end up at index 0 of the appendleft deque.
            items.reverse()
            return items
        except Exception:
            return []

    def _build_mapping(self) -> dict:
        """Build name → {token, symbol} lookup from live Universe."""
        mapping = {}
        if not self.data:
            return mapping
        for token, symbol in self.data.UNIVERSE.items():
            # Direct symbol match (e.g., "RELIANCE" in headline)
            mapping[symbol] = {"token": token, "symbol": symbol}
            # Clean variant without suffixes
            clean = symbol.replace("-EQ", "").replace("_", " ")
            if clean != symbol:
                mapping[clean] = {"token": token, "symbol": symbol}
        return mapping
