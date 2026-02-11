from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
from config import *

client = ClobClient(
    host=CLOB_HOST,
    chain_id=CHAIN_ID,
    key=POLY_PRIVATE_KEY,
    creds=ApiCreds(POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE),
    funder=POLY_FUNDER,
    signature_type=0,
)

params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
bal = client.get_balance_allowance(params)
print(f"USDC Balance: {bal}")
