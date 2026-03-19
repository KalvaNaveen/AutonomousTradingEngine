import threading
from config import SECTOR_TOKENS

class SectorAgent:
    """
    Tracks institutional rotation by evaluating top NSE sectoral indices.
    Calculates 1-day and 5-day momentum.
    """
    def __init__(self, daily_cache, tick_store):
        self.daily = daily_cache
        self.ticks = tick_store
        self._lock = threading.Lock()
        
        self.hot_sectors = []
        self.cold_sectors = []
        self.sector_momentum = {}

    def update(self):
        """
        Calculates momentum for all mapped sectors.
        Called once per tick cycle or periodically in main_loop.
        """
        momenta = []
        for name, token in SECTOR_TOKENS.items():
            cache_data = self.daily.get(token)
            if not cache_data or not cache_data.get("closes"):
                continue
                
            closes = cache_data["closes"]
            if len(closes) < 5:
                continue
                
            # Current price (fallback to yesterday close if no tick yet)
            current_px = self.ticks.get_ltp_if_fresh(token)
            if current_px == 0.0:
                current_px = closes[-1]
                
            prev_close = closes[-1]
            close_5d   = closes[-5]
            
            mom_1d = ((current_px - prev_close) / prev_close) * 100 if prev_close else 0.0
            mom_5d = ((current_px - close_5d) / close_5d) * 100 if close_5d else 0.0
            
            # Weighted score: heavily favor recent 1D flow, but respect 5D trend
            score = (mom_1d * 0.7) + (mom_5d * 0.3)
            
            momenta.append({
                "name": name,
                "token": token,
                "mom_1d": mom_1d,
                "mom_5d": mom_5d,
                "score": score
            })
            
        with self._lock:
            # Sort by score descending (highest momentum first)
            momenta.sort(key=lambda x: x["score"], reverse=True)
            self.sector_momentum = {m["name"]: m for m in momenta}
            
            if len(momenta) >= 6:
                self.hot_sectors = [m["name"] for m in momenta[:3]]
                self.cold_sectors = [m["name"] for m in momenta[-3:]]
            else:
                self.hot_sectors = []
                self.cold_sectors = []

    def is_hot(self, sector_name: str) -> bool:
        with self._lock:
            return sector_name in self.hot_sectors
            
    def is_cold(self, sector_name: str) -> bool:
        with self._lock:
            return sector_name in self.cold_sectors

    def get_market_breadth_summary(self) -> str:
        with self._lock:
            if not self.sector_momentum:
                return "No Sector Data"
            hot_str = ", ".join(self.hot_sectors)
            cold_str = ", ".join(self.cold_sectors)
            return f"🔥 Hot: {hot_str}\n❄️ Cold: {cold_str}"
