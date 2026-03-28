import sys
import os
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pandas as pd
from kiteconnect import KiteConnect
from dotenv import load_dotenv

# Automatically find .env from the project root
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
kite = KiteConnect(api_key=os.getenv('KITE_API_KEY'))
kite.set_access_token(os.getenv('KITE_ACCESS_TOKEN'))

df_inst = pd.DataFrame(kite.instruments('NSE'))
filtered = df_inst[df_inst['tradingsymbol'].str.contains('RELINFRA|SCHNEIDER')]
for _, row in filtered.iterrows():
    print(f"{row['tradingsymbol']} | {row['instrument_type']} | {row['segment']} | {row['name']}")
