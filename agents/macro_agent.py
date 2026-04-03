import time
import requests
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
from collections import defaultdict
from core.api_server import log_agent_action
import datetime

# Core sentiment keywords optimized for Indian Financial news
BULLISH_KEYWORDS = [
    "profit", "surges", "jumps", "wins", "acquires", "upgrade", 
    "approved", "beats", "dividend", "bonus", "rally", "soars",
    "record high", "breakout", "bagged", "order"
]

BEARISH_KEYWORDS = [
    "loss", "plunges", "slumps", "fraud", "cbi", "ed raid", "resigns", 
    "downgrade", "misses", "crashes", "probe", "scam", "default",
    "penalty", "falls", "weak"
]

class MacroAgent:
    def __init__(self, data_agent):
        """
        MacroAgent processes pure news semantics outside of Technical analysis.
        It parses public RSS feeds (Moneycontrol, ET) to locate extreme positive
        or negative catalysts for companies in the Trading Universe.
        """
        self.data = data_agent
        self.last_parsed_urls = set()
        self.feeds = [
            "https://www.moneycontrol.com/rss/MCtopnews.xml",
            "https://www.moneycontrol.com/rss/business.xml"
        ]
        self._last_scan_time = 0

    def get_company_mapping(self):
        """Builds a fast lookup dictionary mapping Company names to their NSE symbols/tokens."""
        mapping = {}
        for token, symbol in self.data.UNIVERSE.items():
            # Example mapping: "RELIANCE" gets mapped to "RELIANCE"
            mapping[symbol] = {"token": token, "symbol": symbol}
            
            # If the symbol has a trailing "EQ" or standard suffixes, strip it for name matching
            clean_name = symbol.replace("-EQ", "").replace("_", " ")
            mapping[clean_name] = {"token": token, "symbol": symbol}
            
        return mapping

    def scan_news(self, regime: str) -> list:
        """
        Fetches RSS, parses for company mentions, and outputs highly confident 
        MACRO signals directly to the execution pipeline. 
        Rate limited to run once every 15 minutes to save bandwidth.
        """
        now = time.time()
        # Only poll news APIs every 15 minutes max
        if now - self._last_scan_time < 900:
            return []
            
        self._last_scan_time = now
        signals = []
        mapping = self.get_company_mapping()
        
        try:
            for feed_url in self.feeds:
                resp = requests.get(feed_url, timeout=10)
                if resp.status_code != 200:
                    continue
                
                root = ET.fromstring(resp.content)
                for item in root.findall('.//item'):
                    title = item.find('title').text or ""
                    link = item.find('link').text or ""
                    desc = item.find('description').text or ""
                    
                    if link in self.last_parsed_urls:
                        continue
                    
                    self.last_parsed_urls.add(link)
                    combined_text = (title + " " + desc).lower()
                    
                    # 1. Search for UNIVERSE Companies in the headline
                    for company_name, data in mapping.items():
                        if company_name.lower() in combined_text:
                            # 2. Assign sentiment scoring
                            bull_score = sum(1 for w in BULLISH_KEYWORDS if w in combined_text)
                            bear_score = sum(1 for w in BEARISH_KEYWORDS if w in combined_text)
                            
                            token = data["token"]
                            symbol = data["symbol"]
                            
                            # 3. Generate Signal if overwhelming bias
                            if bull_score > 0 and bear_score == 0:
                                current_px = self.data.tick_store.get_ltp_if_fresh(token)
                                if current_px <= 0: continue
                                atr = self.data.daily_cache.get_atr(token) or (current_px * 0.02)
                                
                                signals.append({
                                    "strategy": "S8_MACRO_LONG",
                                    "symbol": symbol,
                                    "token": token,
                                    "regime": regime,
                                    "entry_price": current_px,
                                    "is_short": False,
                                    "stop_price": round(current_px - (atr * 0.5), 2),
                                    "target_price": round(current_px + (atr * 2.0), 2),
                                    "headline": title[:50]
                                })
                                log_agent_action("MacroAgent", "NEWS_CATALYST_BULL", f"Detected Positive News on {symbol}: {title}")
                                
                            elif bear_score > 0 and bull_score == 0:
                                current_px = self.data.tick_store.get_ltp_if_fresh(token)
                                if current_px <= 0: continue
                                atr = self.data.daily_cache.get_atr(token) or (current_px * 0.02)
                                
                                signals.append({
                                    "strategy": "S8_MACRO_SHORT",
                                    "symbol": symbol,
                                    "token": token,
                                    "regime": regime,
                                    "entry_price": current_px,
                                    "is_short": True,
                                    "stop_price": round(current_px + (atr * 0.5), 2),
                                    "target_price": round(current_px - (atr * 2.0), 2),
                                    "headline": title[:50]
                                })
                                log_agent_action("MacroAgent", "NEWS_CATALYST_BEAR", f"Detected Negative News on {symbol}: {title}")

        except Exception as e:
            print(f"[MacroAgent] RSS parsing failed: {e}")
            
        return signals
