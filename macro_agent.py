import threading
import yfinance as yf

class MacroAgent:
    """
    Evaluates Global Macroeconomic Sentiment to determine foreign liquidity flows (FII).
    If the Dollar Index (DXY) or US 10-Year Treasury Yields spike, emerging markets 
    (like the NSE) suffer capital flight. The engine will block swing breakouts 
    when global liquidity is choked.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self.dxy = 0.0
        self.us10y = 0.0
        self.brent = 0.0
        self.is_bearish = False
        self._loaded = False
        
    def preload(self):
        """Fetches daily macro indicators."""
        print("[MacroAgent] Fetching global macroeconomic indicators...")
        import concurrent.futures

        def fetch_ticker(symbol):
            try:
                data = yf.Ticker(symbol).history(period="5d")
                if not data.empty:
                    return float(data['Close'].iloc[-1])
            except Exception:
                pass
            return 0.0

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            future_dxy   = executor.submit(fetch_ticker, "DX-Y.NYB")
            future_tnx   = executor.submit(fetch_ticker, "^TNX")
            future_brent = executor.submit(fetch_ticker, "BZ=F")
            
            dxy_val   = future_dxy.result()
            tnx_val   = future_tnx.result()
            brent_val = future_brent.result()

        with self._lock:
            self.dxy   = dxy_val
            self.us10y = tnx_val
            self.brent = brent_val
            
            # Simple Global Liquidity Bear Guard:
            # If DXY > 105 (Strong Dollar) OR US10Y > 4.5% (High Yields) 
            # -> Foreign money leaves India. Block new swing positions.
            self.is_bearish = (self.dxy > 105.0) or (self.us10y > 4.5)
            self._loaded = True
            
        print(f"[MacroAgent] DXY: {self.dxy:.2f} | US10Y: {self.us10y:.2f}% | Brent: ${self.brent:.2f} | Macro Bearish: {self.is_bearish}")

    def block_swing_trades(self) -> bool:
        """Returns True if global macro indicators demand 'Sit on Hands'."""
        with self._lock:
            return self.is_bearish
