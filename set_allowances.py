"""
Set on-chain token allowances for Polymarket trading.

This script approves the Polymarket exchange contracts to spend your
USDC (for buys) and Conditional Tokens (for sells).  You only need to
run this ONCE per wallet.

Requirements:
  - web3 (pip install web3)
  - A small amount of POL (formerly MATIC) in the EOA for gas
  - .env with POLY_PRIVATE_KEY set

Usage:
  python set_allowances.py          # dry-run (shows what would be done)
  python set_allowances.py --run    # execute the transactions

Note for proxy wallets (signature_type=2):
  This script sets approvals from the EOA.  If your funds live in a
  Polymarket proxy wallet, you may also need to enable trading through
  the Polymarket UI ("Sign" / "Enable Trading" button) to set approvals
  from the proxy wallet itself.  After that, run the live_trader which
  calls update_balance_allowance() to refresh the CLOB server's cache.
"""

import sys
import os
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY")
if not PRIVATE_KEY:
    print("FATAL: POLY_PRIVATE_KEY not set in .env")
    sys.exit(1)

# ── Contract addresses (Polygon mainnet, chain_id=137) ──────────
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS  = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Exchange contracts that need approval
EXCHANGE_CONTRACTS = {
    "CTF Exchange":        "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "Neg Risk Exchange":   "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "Neg Risk Adapter":    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
}

# ABIs (minimal)
ERC20_APPROVE_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

ERC1155_APPROVAL_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "operator", "type": "address"},
            {"internalType": "bool", "name": "approved", "type": "bool"},
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

ERC1155_IS_APPROVED_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "account", "type": "address"},
            {"internalType": "address", "name": "operator", "type": "address"},
        ],
        "name": "isApprovedForAll",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    }
]

ERC20_ALLOWANCE_ABI = [
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def main():
    dry_run = "--run" not in sys.argv

    try:
        from web3 import Web3
        from web3.constants import MAX_INT
    except ImportError:
        print("web3 not installed.  Run: pip install web3")
        sys.exit(1)

    # Try to import POA middleware (name varies by web3 version)
    poa_middleware = None
    try:
        from web3.middleware import ExtraDataToPOAMiddleware
        poa_middleware = ExtraDataToPOAMiddleware
    except ImportError:
        try:
            from web3.middleware import geth_poa_middleware
            poa_middleware = geth_poa_middleware
        except ImportError:
            print("WARNING: Could not import POA middleware — may fail on Polygon")

    rpc_url = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")
    web3 = Web3(Web3.HTTPProvider(rpc_url))
    if poa_middleware:
        web3.middleware_onion.inject(poa_middleware, layer=0)

    from eth_account import Account
    account = Account.from_key(PRIVATE_KEY)
    address = account.address

    print(f"EOA address: {address}")
    balance_wei = web3.eth.get_balance(address)
    balance_pol = web3.from_wei(balance_wei, "ether")
    print(f"POL balance: {balance_pol:.4f} POL")

    if balance_pol < 0.01:
        print("WARNING: Very low POL balance — may not have enough gas for transactions")

    funder = os.getenv("POLY_FUNDER")
    if funder and funder.lower() != address.lower():
        print(f"\nProxy wallet (funder): {funder}")
        print("NOTE: This script sets approvals from the EOA.")
        print("      For proxy wallet approvals, use the Polymarket UI 'Enable Trading' flow,")
        print("      then the live_trader's update_balance_allowance() will refresh the cache.")

    usdc = web3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS),
        abi=ERC20_APPROVE_ABI + ERC20_ALLOWANCE_ABI,
    )
    ctf = web3.eth.contract(
        address=Web3.to_checksum_address(CTF_ADDRESS),
        abi=ERC1155_APPROVAL_ABI + ERC1155_IS_APPROVED_ABI,
    )

    max_approval = int(MAX_INT, 0)

    # ── Check current state ──────────────────────────────────
    print("\n--- Current Allowance State ---")
    for label, exchange_addr in EXCHANGE_CONTRACTS.items():
        exchange_cs = Web3.to_checksum_address(exchange_addr)

        # USDC allowance
        try:
            usdc_allowance = usdc.functions.allowance(address, exchange_cs).call()
            usdc_ok = usdc_allowance > 10**12  # more than 1M USDC worth
            print(f"  {label}: USDC allowance = {'OK (max)' if usdc_ok else usdc_allowance}")
        except Exception as e:
            usdc_ok = False
            print(f"  {label}: USDC allowance check failed: {e}")

        # CTF approval
        try:
            ctf_approved = ctf.functions.isApprovedForAll(address, exchange_cs).call()
            print(f"  {label}: CTF approved = {ctf_approved}")
        except Exception as e:
            ctf_approved = False
            print(f"  {label}: CTF approval check failed: {e}")

    if dry_run:
        print("\n--- DRY RUN (pass --run to execute) ---")
        print("Would set the following approvals:")
        for label, exchange_addr in EXCHANGE_CONTRACTS.items():
            print(f"  USDC.approve({exchange_addr}, MAX_UINT256)")
            print(f"  CTF.setApprovalForAll({exchange_addr}, true)")
        return

    # ── Execute approvals ────────────────────────────────────
    print("\n--- Setting Approvals ---")
    chain_id = 137

    for label, exchange_addr in EXCHANGE_CONTRACTS.items():
        exchange_cs = Web3.to_checksum_address(exchange_addr)
        nonce = web3.eth.get_transaction_count(address)

        # USDC approve
        print(f"\n  [{label}] USDC approve...", end=" ", flush=True)
        try:
            tx = usdc.functions.approve(exchange_cs, max_approval).build_transaction({
                "chainId": chain_id,
                "from": address,
                "nonce": nonce,
            })
            signed = web3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
            tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            status = "OK" if receipt["status"] == 1 else "FAILED"
            print(f"{status} (tx: {receipt['transactionHash'].hex()})")
        except Exception as e:
            print(f"FAILED: {e}")

        nonce = web3.eth.get_transaction_count(address)

        # CTF setApprovalForAll
        print(f"  [{label}] CTF setApprovalForAll...", end=" ", flush=True)
        try:
            tx = ctf.functions.setApprovalForAll(exchange_cs, True).build_transaction({
                "chainId": chain_id,
                "from": address,
                "nonce": nonce,
            })
            signed = web3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
            tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            status = "OK" if receipt["status"] == 1 else "FAILED"
            print(f"{status} (tx: {receipt['transactionHash'].hex()})")
        except Exception as e:
            print(f"FAILED: {e}")

    print("\nDone. Allowances should now be set for the EOA.")
    print("If using a proxy wallet, also enable trading via the Polymarket UI.")


if __name__ == "__main__":
    main()
