import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import os
import pandas as pd
from kiteconnect import KiteConnect
from dotenv import load_dotenv

load_dotenv('c:/Users/Admin/.gemini/antigravity/scratch/bnf_engine/.env')
kite = KiteConnect(api_key=os.getenv('KITE_API_KEY'))
kite.set_access_token(os.getenv('KITE_ACCESS_TOKEN'))

df_inst = pd.DataFrame(kite.instruments('NSE'))
filtered = df_inst[df_inst['tradingsymbol'].str.contains('RELINFRA|SCHNEIDER')]
for _, row in filtered.iterrows():
    print(f"{row['tradingsymbol']} | {row['instrument_type']} | {row['segment']} | {row['name']}")
