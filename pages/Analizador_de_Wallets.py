from __future__ import annotations

import os
import sys
import json
import io
import time
import unicodedata
from datetime import datetime, date, timedelta
from collections import defaultdict

import streamlit as st
import pandas as pd
import requests
import plotly.graph_objects as go
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import fetch_all_account_txs, fetch_all_token_txs

load_dotenv()

ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"
POLYGON_CHAIN_ID = 137
API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
GSHEET_CSV_URL = os.getenv("GSHEET_CSV_URL", "")
GSHEET_WALLETS_URL = os.getenv("GSHEET_WALLETS_URL", "")

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
STABLECOIN_SYMBOLS = {"USDT", "USDT0", "USDC", "DAI", "USDC.E", "USDCE"}
# Contratos de stablecoins en Polygon → (símbolo canónico, decimales)
STABLECOIN_CONTRACTS = {
    "0xc2132d05d31c914a87c6611c10748aeb04b58e8f": ("USDT", 6),   # (PoS) Tether — a veces llamado USDT0 por la API
    "0xe84baaebd135cde0d03b974d3224a742570834af": ("USDT", 6),   # Tether USD (bridge alternativo)
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": ("USDC", 6),
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": ("USDC", 6),   # USDC nativo Polygon
    "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063": ("DAI", 18),
}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Ecosistema RNT — contratos en Polygon
RNT_CONTRACT     = "0x27ab6e82f3458edbc0703db2756391b899ce6324"
SLP_CONTRACT     = "0x4097073e82edac2d758ecfd594139a891340d59d"   # SushiSwap LP (RNT/USDT)
FRMRNT_CONTRACT  = "0xcfee52ce10aa7e6a4ba219e56ce60207654815e9"   # posición de farming
STAKING_RECEIVER = "0x0bdf6e4674b408cac2fb00d840c549b4e410cf47"   # recibe RNT al hacer stake; también contrato xRNT (NFT ERC-721)
STAKING_CLAIM    = "0xc2c5e41c46fa871a0f9fa54b47a115033521d92e"   # distribuye RNT+SLP+USDT en claim
FARMING_CLAIM    = "0xb20d1ec6ead7aa22a5001401cde522940eeb0f0e"   # distribuye RNT en claim farming
POOL_ROUTER      = "0x03e2ca2d684049ea8bfa87e078377980f543b6e3"   # router para ops de pool
RNT_ECOSYSTEM_CONTRACTS = {RNT_CONTRACT, SLP_CONTRACT, FRMRNT_CONTRACT, STAKING_RECEIVER}

def normalize_stable_symbol(symbol: str, contract: str = "") -> str:
    """Devuelve el símbolo canónico de un stablecoin independientemente de cómo lo etiquete la API."""
    if contract.lower() in STABLECOIN_CONTRACTS:
        return STABLECOIN_CONTRACTS[contract.lower()][0]
    sym = symbol.upper().replace(".", "").replace("0", "")  # USDT0 → USDT, USDC.E → USDCE → USDC
    if "USDT" in sym:
        return "USDT"
    if "USDC" in sym:
        return "USDC"
    if "DAI" in sym:
        return "DAI"
    return symbol

st.title("🏠 Reental — Analizador de Cartera de Inversores")


def strip_accents(text: str) -> str:
    """Elimina tildes y diacríticos para evitar problemas de encoding en texto importado."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    )


# ── Carga de tokens ──────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False, ttl=3600)
def load_reental_addresses() -> set:
    addresses = set()

    # Fuente local (fallback)
    path = os.path.join(os.path.dirname(__file__), "reental_addresses.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        addresses.update(a.lower() for a in data.get("addresses", []))

    # Fuente Google Sheet (sin cabecera: col A = address, col B = nombre interno)
    if GSHEET_WALLETS_URL:
        try:
            r = requests.get(GSHEET_WALLETS_URL, timeout=15, allow_redirects=True)
            r.raise_for_status()
            df = pd.read_csv(io.BytesIO(r.content), header=None, encoding="utf-8")
            for val in df.iloc[:, 0].dropna().astype(str):
                addr = val.strip().lower()
                if addr.startswith("0x") and len(addr) == 42:
                    addresses.add(addr)
        except Exception:
            pass  # Si falla la hoja, usamos el fichero local

    return addresses


@st.cache_data(show_spinner=False, ttl=3600)
def load_closing_dates() -> dict:
    """
    Devuelve {token_address_lower: {"fecha": "DD/MM/YYYY", "estado": "CERRADO"|...}}
    con datos del Máster Inmuebles Pro. Si no hay fecha real, la clave "fecha" es "".
    """
    if not GSHEET_CSV_URL:
        return {}
    try:
        r = requests.get(GSHEET_CSV_URL, timeout=15, allow_redirects=True)
        r.raise_for_status()
        df = pd.read_csv(io.BytesIO(r.content), header=None, encoding="utf-8")
        header_row = next(
            (i for i, row in df.iterrows() if any("Token Address" in str(v) for v in row.values)), None
        )
        if header_row is None:
            return {}
        df.columns = df.iloc[header_row]
        df = df.iloc[header_row + 1:].reset_index(drop=True)
        result = {}
        for _, row in df.iterrows():
            addr = str(row.get("Token Address", "")).strip().lower()
            if not (addr.startswith("0x") and len(addr) == 42):
                continue
            fecha = str(row.get("Real fecha de fin", "")).strip()
            if fecha == "nan":
                fecha = ""
            estado = str(row.get("ESTADO", "")).strip()
            if estado == "nan":
                estado = ""
            estado = strip_accents(estado)
            result[addr] = {"fecha": fecha, "estado": estado}
        return result
    except Exception:
        return {}


@st.cache_data(show_spinner=False, ttl=3600)
def load_tokens() -> dict:
    tokens = {}

    local_path = os.path.join(os.path.dirname(__file__), "tokens.json")
    if os.path.exists(local_path):
        with open(local_path, encoding="utf-8") as f:
            data = json.load(f)
        for t in data:
            addr = t.get("address", "").strip().lower()
            if addr and addr.startswith("0x") and len(addr) == 42:
                label = t.get("symbol") or t.get("name") or addr[:10]
                tokens[addr] = {"name": t.get("name", ""), "symbol": t.get("symbol", ""),
                                "label": label, "address": addr}

    if GSHEET_CSV_URL:
        try:
            r = requests.get(GSHEET_CSV_URL, timeout=15, allow_redirects=True)
            r.raise_for_status()
            df = pd.read_csv(io.BytesIO(r.content), header=None, encoding="utf-8")
            header_row = next(
                (i for i, row in df.iterrows() if any("Token Address" in str(v) for v in row.values)), None
            )
            if header_row is not None:
                df.columns = df.iloc[header_row]
                df = df.iloc[header_row + 1:].reset_index(drop=True)
                if "Token Address" in df.columns:
                    for _, row in df.iterrows():
                        addr = str(row.get("Token Address", "")).strip().lower()
                        if not addr.startswith("0x") or len(addr) != 42:
                            continue
                        name = str(row.get("Nombre del proyecto", "")).strip()
                        project_id = str(row.get("ID", "")).strip()
                        label = project_id if project_id and project_id != "nan" else name
                        divisa_raw = str(row.iloc[11]).strip()
                        divisa = "USD" if "$" in divisa_raw else "EUR"
                        try:
                            precio_emision = float(str(row.iloc[10]).strip().replace(",", "."))
                        except Exception:
                            precio_emision = None
                        tokens[addr] = {
                            "name": name, "symbol": project_id, "label": label, "address": addr,
                            "divisa": divisa, "precio_emision": precio_emision,
                        }
        except Exception:
            pass

    return tokens


# ── API ──────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False, ttl=300)
def fetch_nft_transfers(wallet: str, contract: str) -> list:
    """Obtiene transferencias de NFTs (ERC-721) para una wallet y contrato específico."""
    if not API_KEY:
        return []
    params = {"chainid": POLYGON_CHAIN_ID, "module": "account", "action": "tokennfttx",
              "contractaddress": contract, "address": wallet,
              "startblock": 0, "endblock": 99999999, "sort": "asc", "apikey": API_KEY}
    try:
        r = requests.get(ETHERSCAN_V2_BASE, params=params, timeout=30)
        data = r.json()
        if data.get("status") == "0":
            return []
        return data.get("result") or []
    except Exception:
        return []


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_xrnt_staked_rnt(token_id: str) -> float:
    """
    Dado el tokenID de un xRNT NFT, localiza la TX de mint original y extrae
    cuánto RNT se depositó en el contrato de staking en esa misma transacción.
    Devuelve 0.0 si no se puede determinar.
    """
    if not API_KEY or not token_id:
        return 0.0
    try:
        zero_topic = "0x" + "0" * 64
        tid_hex    = "0x" + format(int(token_id), "064x")

        # 1. Buscar el log de Transfer(from=0x0, to=any, tokenId=token_id) en xRNT
        r = requests.get(ETHERSCAN_V2_BASE, params={
            "chainid": POLYGON_CHAIN_ID, "module": "logs", "action": "getLogs",
            "address": STAKING_RECEIVER,
            "topic0": TRANSFER_TOPIC, "topic0_1_opr": "and", "topic1": zero_topic,
            "topic2_3_opr": "and", "topic3": tid_hex,
            "apikey": API_KEY,
        }, timeout=20)
        logs = r.json().get("result") or []
        if not logs:
            return 0.0

        mint_tx_hash = logs[0]["transactionHash"].lower()

        # 2. Leer el receipt completo de la TX de mint para evitar el límite de
        #    1.000 resultados que afecta a getLogs en bloques muy activos.
        r2 = requests.get(ETHERSCAN_V2_BASE, params={
            "chainid": POLYGON_CHAIN_ID, "module": "proxy",
            "action": "eth_getTransactionReceipt",
            "txhash": mint_tx_hash, "apikey": API_KEY,
        }, timeout=20)
        receipt_logs = r2.json().get("result", {}).get("logs", [])

        rnt_lower      = RNT_CONTRACT.lower()
        receiver_lower = STAKING_RECEIVER.lower()
        for log in receipt_logs:
            if log.get("address", "").lower() != rnt_lower:
                continue
            topics = log.get("topics", [])
            if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC:
                continue
            to_addr = "0x" + topics[2][-40:]
            if to_addr.lower() == receiver_lower:
                return int(log.get("data", "0x0"), 16) / 1e18
    except Exception:
        pass
    return 0.0


def fetch_token_transfers(wallet: str) -> list:
    if not API_KEY:
        raise ValueError("No hay API Key configurada. Añade ETHERSCAN_API_KEY en el archivo .env")
    # Etherscan limita tokentx a 1000 resultados por llamada: hay que paginar
    # o el análisis se queda solo con los transfers más antiguos de la wallet.
    return fetch_all_token_txs(wallet, API_KEY)


# ── Procesado ────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False, ttl=86400)
def get_vault_payment(tx_hash: str, wallet: str):
    """
    Lee el receipt completo de tx_hash y busca el patrón de reinversión desde vault:
    cualquier transferencia de stablecoin donde ni el origen ni el destino
    son la wallet del usuario (el pago ocurre entre el vault y un contrato/vendedor).
    Devuelve (vault_address, amount, symbol) o None si no aplica.
    """
    r = requests.get(ETHERSCAN_V2_BASE, params={
        "chainid": POLYGON_CHAIN_ID, "module": "proxy",
        "action": "eth_getTransactionReceipt",
        "txhash": tx_hash, "apikey": API_KEY,
    }, timeout=20)
    try:
        logs = r.json().get("result", {}).get("logs", [])
    except Exception:
        return None

    wallet = wallet.lower()
    for log in logs:
        topics = log.get("topics", [])
        if len(topics) < 3:
            continue
        if topics[0].lower() != TRANSFER_TOPIC:
            continue
        contract = log["address"].lower()
        if contract not in STABLECOIN_CONTRACTS:
            continue
        from_addr = "0x" + topics[1][-40:]
        to_addr   = "0x" + topics[2][-40:]
        # El pago de vault no involucra directamente la wallet del usuario
        # ni es una operación de mint (desde 0x0000...)
        if from_addr.lower() == wallet or to_addr.lower() == wallet:
            continue
        if from_addr.lower() == ZERO_ADDRESS:
            continue
        sym, dec = STABLECOIN_CONTRACTS[contract]
        amount = int(log.get("data", "0x0"), 16) / (10 ** dec)
        return (from_addr, amount, sym)
    return None


def match_aave_token(tx_symbol: str, tx_name: str, known_tokens: dict):
    code = None
    if tx_symbol.startswith("aMatReental-"):
        code = tx_symbol[len("aMatReental-"):]
    elif tx_name.startswith("Aave Matic Reental-"):
        code = tx_name[len("Aave Matic Reental-"):]
    if not code:
        return None
    for info in known_tokens.values():
        sym = info.get("symbol", "")
        if sym == f"Reental-{code}" or sym == code:
            return info
    return None


def label_address(addr: str, wallet: str, atoken_contracts: dict, reental_addresses: set = None,
                   own_wallets: dict = None, wallet_alias: str = None) -> str:
    addr = addr.lower()
    if addr == wallet:
        return wallet_alias or "Tu wallet"
    if addr == ZERO_ADDRESS:
        return "Protocolo"
    if addr in atoken_contracts:
        return f"Aave — {atoken_contracts[addr]}"
    if own_wallets and addr in own_wallets:
        return f"Tu wallet «{own_wallets[addr]}»"
    if reental_addresses and addr in reental_addresses:
        return "Wallet Reental"
    return "Wallet de un tercero"


def process_transfers(transfers: list, wallet: str, known_tokens: dict, reental_addresses: set = None,
                       own_wallets: dict = None, wallet_alias: str = None) -> dict:
    wallet = wallet.lower()
    reental_addresses = reental_addresses or set()
    own_wallets = own_wallets or {}
    mi_label = wallet_alias or "Tu wallet"

    # Índice aToken address → project label
    atoken_contracts = {}
    for tx in transfers:
        sym = tx.get("tokenSymbol", "")
        name = tx.get("tokenName", "")
        if sym.startswith("aMatReental-") or name.startswith("Aave Matic Reental-"):
            match = match_aave_token(sym, name, known_tokens)
            if match:
                atoken_contracts[tx["contractAddress"].lower()] = match.get("label", sym)

    # Agrupar TODOS los transfers por TX hash para detectar contexto
    tx_groups = defaultdict(list)
    for tx in transfers:
        tx_groups[tx["hash"]].append(tx)

    token_data = defaultdict(lambda: {"movements": [], "balance": 0.0, "info": {}})

    for tx_hash, group in tx_groups.items():
        # ¿Hay stablecoins en este TX?
        stable_in, stable_out = 0.0, 0.0
        stable_symbol = ""
        for tx in group:
            sym_upper = tx.get("tokenSymbol", "").upper().replace(".", "")
            if sym_upper in STABLECOIN_SYMBOLS or tx["contractAddress"].lower() in STABLECOIN_CONTRACTS:
                dec = int(tx["tokenDecimal"]) if tx["tokenDecimal"] else 6
                val = int(tx["value"]) / (10 ** dec)
                canon = normalize_stable_symbol(tx.get("tokenSymbol", ""), tx["contractAddress"])
                if tx["to"].lower() == wallet:
                    stable_in += val
                    stable_symbol = canon
                else:
                    stable_out += val
                    stable_symbol = canon

        # ¿Hay aTokens entrando o saliendo?
        atoken_in = any(
            (tx.get("tokenSymbol", "").startswith("aMatReental-") or
             tx.get("tokenName", "").startswith("Aave Matic Reental-"))
            and tx["to"].lower() == wallet
            for tx in group
        )
        atoken_out = any(
            (tx.get("tokenSymbol", "").startswith("aMatReental-") or
             tx.get("tokenName", "").startswith("Aave Matic Reental-"))
            and tx["from"].lower() == wallet
            for tx in group
        )

        for tx in group:
            contract = tx["contractAddress"].lower()
            tx_symbol = tx.get("tokenSymbol", "")
            tx_name = tx.get("tokenName", "")

            is_aave = False
            original_info = {}

            if contract in known_tokens:
                original_info = known_tokens[contract]
            else:
                aave_match = match_aave_token(tx_symbol, tx_name, known_tokens)
                if aave_match is None:
                    continue
                is_aave = True
                original_info = aave_match

            dec = int(tx["tokenDecimal"]) if tx["tokenDecimal"] else 18
            value = int(tx["value"]) / (10 ** dec)
            direction = "entrada" if tx["to"].lower() == wallet else "salida"
            signed = value if direction == "entrada" else -value

            # Clasificar la operación
            from_addr = tx["from"].lower()
            to_addr = tx["to"].lower()
            es_transferencia_interna = False

            if is_aave:
                if direction == "entrada":
                    tipo = "Colateralización en Aave"
                    origen = label_address(from_addr, wallet, atoken_contracts, reental_addresses, own_wallets, wallet_alias)
                    destino = mi_label
                else:
                    tipo = "Descolateralización en Aave"
                    origen = mi_label
                    destino = label_address(to_addr, wallet, atoken_contracts, reental_addresses, own_wallets, wallet_alias)
            elif direction == "entrada":
                if atoken_out:
                    # El usuario quemó aTokens en el mismo TX → está recuperando su colateral
                    tipo = "Descolateralización en Aave"
                    origen = label_address(from_addr, wallet, atoken_contracts, reental_addresses, own_wallets, wallet_alias)
                    destino = mi_label
                elif stable_out > 0:
                    # Compra directa: el usuario pagó USDT desde su propia wallet
                    if from_addr in reental_addresses:
                        tipo = f"Compra de Tokens a Reental ({stable_out:,.2f} {stable_symbol})"
                    else:
                        tipo = f"Compra de Tokens a terceros ({stable_out:,.2f} {stable_symbol})"
                    origen = label_address(from_addr, wallet, atoken_contracts, reental_addresses, own_wallets, wallet_alias)
                    destino = mi_label
                else:
                    # Comprobar patrón reinversión vault: USDT sale del vault → vendedor en misma TX
                    vault_info = get_vault_payment(tx["hash"], wallet)
                    if vault_info:
                        vault_addr, vault_amount, vault_sym = vault_info
                        vault_sym = normalize_stable_symbol(vault_sym)
                        tipo = f"Reinversión desde vault ({vault_amount:,.2f} {vault_sym})"
                        origen = f"Vault ({vault_addr[:8]}…{vault_addr[-4:]})"
                        destino = mi_label
                        stable_out = vault_amount
                        stable_symbol = vault_sym
                    elif from_addr in own_wallets and from_addr != wallet:
                        # Sin contraparte en stablecoin y el origen es otra wallet propia: transferencia interna
                        tipo = f"Transferencia interna ← {own_wallets[from_addr]}"
                        origen = label_address(from_addr, wallet, atoken_contracts, reental_addresses, own_wallets, wallet_alias)
                        destino = mi_label
                        es_transferencia_interna = True
                    else:
                        tipo = "Entrada de Tokens *"
                        origen = label_address(from_addr, wallet, atoken_contracts, reental_addresses, own_wallets, wallet_alias)
                        destino = mi_label
            else:  # salida
                if atoken_in:
                    tipo = "Colateralización en Aave"
                    origen = mi_label
                    destino = label_address(to_addr, wallet, atoken_contracts, reental_addresses, own_wallets, wallet_alias)
                elif stable_in > 0:
                    tipo = f"Venta de tokens ({stable_in:,.2f} {stable_symbol})"
                    origen = mi_label
                    destino = "Protocolo liquidación proyecto" if to_addr == ZERO_ADDRESS else label_address(to_addr, wallet, atoken_contracts, reental_addresses, own_wallets, wallet_alias)
                else:
                    # El USDT puede ir al vault del usuario en lugar de a la wallet directamente
                    vault_info = get_vault_payment(tx["hash"], wallet)
                    if vault_info:
                        vault_addr, vault_amount, vault_sym = vault_info
                        vault_sym = normalize_stable_symbol(vault_sym)
                        tipo = f"Venta de tokens ({vault_amount:,.2f} {vault_sym} al vault)"
                        origen = mi_label
                        destino = "Protocolo liquidación proyecto" if to_addr == ZERO_ADDRESS else label_address(to_addr, wallet, atoken_contracts, reental_addresses, own_wallets, wallet_alias)
                        stable_in = vault_amount
                        stable_symbol = vault_sym
                    elif to_addr in own_wallets and to_addr != wallet:
                        # Sin contraparte en stablecoin y el destino es otra wallet propia: transferencia interna
                        tipo = f"Transferencia interna → {own_wallets[to_addr]}"
                        origen = mi_label
                        destino = label_address(to_addr, wallet, atoken_contracts, reental_addresses, own_wallets, wallet_alias)
                        es_transferencia_interna = True
                    else:
                        destino_label = label_address(to_addr, wallet, atoken_contracts, reental_addresses, own_wallets, wallet_alias)
                        if destino_label == "Wallet Reental":
                            tipo = "Salida de Tokens *"
                        else:
                            tipo = "Salida de tokens"
                        origen = mi_label
                        destino = destino_label

            name = original_info.get("name") or tx_name
            symbol = original_info.get("symbol") or tx_symbol
            label = original_info.get("label") or symbol or name
            aave_note = " (Aave)" if is_aave else ""

            token_data[contract]["balance"] += signed
            token_data[contract]["info"] = {
                "name": name, "symbol": symbol,
                "label": label + aave_note,
                "address": contract,
                "is_aave": is_aave,
                "underlying_address": original_info.get("address", "") if is_aave else "",
                "divisa": original_info.get("divisa", "USD"),
                "precio_emision": original_info.get("precio_emision"),
            }
            token_data[contract]["movements"].append({
                "fecha": datetime.utcfromtimestamp(int(tx["timeStamp"])),
                "fecha_str": datetime.utcfromtimestamp(int(tx["timeStamp"])).strftime("%Y-%m-%d %H:%M"),
                "tipo": tipo,
                "origen": origen,
                "destino": destino,
                "cantidad": value,
                "cantidad_neta": signed,
                "stable_in": stable_in,
                "stable_out": stable_out,
                "stable_symbol": stable_symbol,
                "tx_hash": tx["hash"],
                "tx_link": f"https://polygonscan.com/tx/{tx['hash']}",
                "es_transferencia_interna": es_transferencia_interna,
                "wallet_addr": wallet,
                "wallet_alias": mi_label,
            })

    # Ordenar movimientos por fecha dentro de cada token
    for data in token_data.values():
        data["movements"].sort(key=lambda m: m["fecha"])

    return dict(token_data)


def balance_at_date(movements: list, cutoff: date) -> float:
    return sum(m["cantidad_neta"] for m in movements if m["fecha"].date() <= cutoff)


def balances_por_wallet(movements: list, cutoff: date, use_date_filter: bool) -> dict:
    """Desglosa el saldo de un token por wallet de origen: {wallet_addr: {"alias", "balance"}}."""
    result = {}
    for m in movements:
        if use_date_filter and m["fecha"].date() > cutoff:
            continue
        addr = m.get("wallet_addr")
        entry = result.setdefault(addr, {"alias": m.get("wallet_alias", "—"), "balance": 0.0})
        entry["balance"] += m["cantidad_neta"]
    return result


def build_stablecoin_movements(token_data: dict) -> list:
    """
    Extrae todos los flujos de stablecoins relacionados con Reental
    a partir de los movimientos ya procesados de tokens inmobiliarios.
    """
    rows = []
    for contract, data in token_data.items():
        label = data["info"]["label"]
        for m in data["movements"]:
            amount = 0.0
            direction = ""
            if m["stable_in"] > 0 and m["cantidad_neta"] < 0:
                # Venta: recibimos stablecoin
                amount = m["stable_in"]
                direction = "entrada"
            elif m["stable_out"] > 0 and m["cantidad_neta"] > 0:
                # Compra o reinversión: pagamos stablecoin
                amount = m["stable_out"]
                direction = "salida"
            else:
                continue
            rows.append({
                "fecha": m["fecha"],
                "fecha_str": m["fecha_str"],
                "token": label,
                "operacion": m["tipo"],
                "direction": direction,
                "amount": amount,
                "stable_symbol": m["stable_symbol"] or "USDT",
                "tx_link": m["tx_link"],
                "wallet_addr": m.get("wallet_addr"),
                "wallet_alias": m.get("wallet_alias"),
            })
    rows.sort(key=lambda r: r["fecha"])
    return rows


def process_aave_activity(transfers: list, wallet: str) -> dict:
    """
    Separa la actividad Aave en dos roles:
      - lender:   prestamista (deposita/retira stablecoins, gana intereses)
      - borrower: prestatario (deposita colateral Reental, pide/devuelve USDT)
    Devuelve {"lender": [...], "borrower": [...]}.
    """
    wallet = wallet.lower()

    def _is_debt_token(sym, name):
        """Token de deuda variable/estable de Aave"""
        return sym.startswith("variableDebt") or sym.startswith("stableDebt")

    def _is_lender_atoken(sym, name):
        """aToken de stablecoin (no Reental, no deuda): aMat/aPolUSDT, aMat/aPolUSDC…
        IMPORTANTE: se comprueba DESPUÉS de _is_debt_token porque los debt tokens
        tienen nombre 'Aave Matic Variable Debt USDT' que empieza con 'Aave' y
        pasarían este filtro erróneamente si no se excluyen primero.
        """
        if _is_debt_token(sym, name):
            return False
        prefixed = sym.startswith("aMat") or sym.startswith("aPol") or name.startswith("Aave")
        is_stable = any(s in sym.upper() for s in ("USDT", "USDC", "DAI"))
        is_reental = "Reental" in name or "Reental" in sym
        return prefixed and is_stable and not is_reental

    def _is_collateral_atoken(sym, name):
        """aToken de colateral Reental: aMatReental-…"""
        prefixed = sym.startswith("aMat") or sym.startswith("aPol")
        is_reental = "Reental" in name or "Reental" in sym
        return prefixed and is_reental

    from collections import defaultdict as _dd
    tx_groups = _dd(list)
    for tx in transfers:
        tx_groups[tx["hash"]].append(tx)

    lender   = []
    borrower = []
    seen     = set()

    for tx_hash, group in tx_groups.items():
        dt = datetime.utcfromtimestamp(int(group[0]["timeStamp"]))
        tx_link = f"https://polygonscan.com/tx/{tx_hash}"

        # Clasificar cada transfer del grupo por tipo
        lender_atokens    = []   # aMat/aPolUSDT/USDC
        collateral_tokens = []   # aMatReental
        debt_tokens       = []   # variableDebt*
        stables_in        = []   # USDT/USDC que entran a la wallet
        stables_out       = []   # USDT/USDC que salen de la wallet

        for tx in group:
            sym   = tx.get("tokenSymbol", "")
            name  = tx.get("tokenName", "")
            addr  = tx["contractAddress"].lower()
            dec   = int(tx["tokenDecimal"]) if tx["tokenDecimal"] else 6
            value = int(tx["value"]) / (10 ** dec)
            to_   = tx["to"].lower()
            from_ = tx["from"].lower()
            direction = "in" if to_ == wallet else "out"

            if _is_lender_atoken(sym, name):
                lender_atokens.append({"sym": sym, "value": value, "dir": direction})
            elif _is_collateral_atoken(sym, name):
                collateral_tokens.append({"sym": sym, "value": value, "dir": direction})
            elif _is_debt_token(sym, name):
                debt_tokens.append({"sym": sym, "value": value, "dir": direction})
            elif addr in STABLECOIN_CONTRACTS:
                sym_s, dec_s = STABLECOIN_CONTRACTS[addr]
                val_s = int(tx["value"]) / (10 ** dec_s)
                if direction == "in":
                    stables_in.append({"sym": sym_s, "value": val_s})
                else:
                    stables_out.append({"sym": sym_s, "value": val_s})

        # ── Prestamista ───────────────────────────────────────────────────────
        for at in lender_atokens:
            key = (tx_hash, at["sym"])
            if key in seen:
                continue
            seen.add(key)
            if at["dir"] == "in":
                # Depósito: aToken entra, USDT sale
                stable_amt = sum(s["value"] for s in stables_out)
                stable_sym = stables_out[0]["sym"] if stables_out else "USDT"
                lender.append({
                    "fecha": dt, "fecha_str": dt.strftime("%Y-%m-%d %H:%M"),
                    "tipo": "Depósito préstamo",
                    "atoken": at["sym"], "cantidad_atoken": at["value"],
                    "stable_amount": stable_amt, "stable_symbol": stable_sym,
                    "interest_note": "", "tx_link": tx_link,
                })
            else:
                # Retirada: aToken sale, USDT entra
                stable_amt = sum(s["value"] for s in stables_in)
                stable_sym = stables_in[0]["sym"] if stables_in else "USDT"
                interest_note = ""
                if stable_amt > at["value"] + 0.001:
                    interest_note = f"+{stable_amt - at['value']:,.2f} {stable_sym} intereses"
                lender.append({
                    "fecha": dt, "fecha_str": dt.strftime("%Y-%m-%d %H:%M"),
                    "tipo": "Retirada préstamo",
                    "atoken": at["sym"], "cantidad_atoken": at["value"],
                    "stable_amount": stable_amt, "stable_symbol": stable_sym,
                    "interest_note": interest_note, "tx_link": tx_link,
                })

        # ── Prestatario — garantía ────────────────────────────────────────────
        for ct in collateral_tokens:
            key = (tx_hash, ct["sym"])
            if key in seen:
                continue
            seen.add(key)
            if ct["dir"] == "in":
                borrower.append({
                    "fecha": dt, "fecha_str": dt.strftime("%Y-%m-%d %H:%M"),
                    "tipo": "Garantía depositada",
                    "detalle": ct["sym"], "cantidad": ct["value"],
                    "stable_amount": 0.0, "stable_symbol": "",
                    "tx_link": tx_link,
                })
            else:
                borrower.append({
                    "fecha": dt, "fecha_str": dt.strftime("%Y-%m-%d %H:%M"),
                    "tipo": "Garantía retirada",
                    "detalle": ct["sym"], "cantidad": ct["value"],
                    "stable_amount": 0.0, "stable_symbol": "",
                    "tx_link": tx_link,
                })

        # ── Prestatario — deuda ───────────────────────────────────────────────
        # Aave V3 puede emitir Transfer(0x0→wallet) del debt token durante un
        # repago (minting del interés acumulado antes de quemar el principal).
        # Por eso NO nos fiamos de la dirección del debt token: usamos la
        # dirección del USDT/USDC para distinguir borrow (USDT entra) vs repago
        # (USDT sale). Solo creamos UN evento por TX de deuda.
        if debt_tokens and (tx_hash, "_debt") not in seen:
            seen.add((tx_hash, "_debt"))
            debt_sym = debt_tokens[0]["sym"]
            debt_qty = sum(t["value"] for t in debt_tokens)
            if stables_in:
                # USDT entra → es un préstamo recibido (borrow)
                stable_amt = sum(s["value"] for s in stables_in)
                stable_sym = stables_in[0]["sym"]
                borrower.append({
                    "fecha": dt, "fecha_str": dt.strftime("%Y-%m-%d %H:%M"),
                    "tipo": "Préstamo recibido",
                    "detalle": debt_sym, "cantidad": debt_qty,
                    "stable_amount": stable_amt, "stable_symbol": stable_sym,
                    "tx_link": tx_link,
                })
            else:
                # USDT sale (o no hay USDT si paga con aTokens) → pago de deuda
                stable_amt = sum(s["value"] for s in stables_out)
                if not stable_amt:
                    # Repago con aTokens: importe = aTokens salientes
                    stable_amt = sum(at["value"] for at in lender_atokens if at["dir"] == "out")
                stable_sym = (stables_out[0]["sym"] if stables_out
                              else (lender_atokens[0]["sym"] if lender_atokens else "USDT"))
                borrower.append({
                    "fecha": dt, "fecha_str": dt.strftime("%Y-%m-%d %H:%M"),
                    "tipo": "Pago de deuda",
                    "detalle": debt_sym, "cantidad": debt_qty,
                    "stable_amount": stable_amt, "stable_symbol": stable_sym,
                    "tx_link": tx_link,
                })

    lender.sort(key=lambda m: m["fecha"])
    borrower.sort(key=lambda m: m["fecha"])
    return {"lender": lender, "borrower": borrower}


def get_fecha_fin_display(info: dict, closing_dates: dict) -> str:
    """
    Devuelve el texto a mostrar en 'Fecha real de fin de proyecto'.
    Para tokens Aave usa la dirección subyacente. Si no hay fecha real, devuelve el ESTADO.
    """
    addr = info.get("address", "").lower()
    underlying = info.get("underlying_address", "").lower()
    entry = closing_dates.get(addr) or closing_dates.get(underlying)
    if not entry:
        return "—"
    fecha = entry.get("fecha", "")
    if fecha:
        return fecha
    estado = entry.get("estado", "")
    return estado if estado else "—"


# Distribuidores de dividendos conocidos. Reental ha migrado de contrato con el
# tiempo (p. ej. 0xc163… activo 2023-2025, 0xf9b1… desde 2025), así que un único
# `from` hardcodeado se comería la historia antigua. Esta lista solo acelera el
# caso conocido; la detección REAL (que auto-descubre distribuidores nuevos) es
# por orquestador + método de la transacción (ver tx_es_dividendo).
DIVIDEND_DISTRIBUTORS = {
    "0xf9b135fd84ae6dc9d6e632a97235de5f08c0d61e",
    "0xc1636217ce488540a4fb4aed26839f080c9d56d7",
}
# Contrato orquestador y selector de método con los que Reental reparte los
# dividendos mensuales a la wallet o al vault, con independencia del contrato
# distribuidor que financie el pago en cada época.
DIVIDEND_ORCHESTRATOR = "0x079ce6640e2f4ec39b1da8e4b072b8beecf09a2b"
DIVIDEND_METHOD_ID    = "0xafc13168"
OWNER_SELECTOR        = "0x8da5cb5b"   # owner()

# Selectores de método con los que un inversor opera SU vault (reinvertir el
# saldo en tokens, reclamar, etc.). Son funciones del propio contrato vault, así
# que el destino de una transacción de la wallet que use uno de ellos ES su
# vault. Permite identificarlo a coste cero (el txlist ya está descargado) y sin
# depender de ventanas de 10.000 filas ni de cuándo se cobró el último dividendo.
VAULT_METHOD_IDS = {"0x346476f1", "0x2eb652a9", "0x8696e4ff", "0x753d3c76"}


def _etherscan_proxy(params: dict):
    """GET al endpoint proxy de Etherscan con reintento ante rate-limit. Devuelve
    el JSON, o None si no se pudo. El plan gratuito limita a ~5 llamadas/seg y en
    ese caso responde con un mensaje (no con hex), así que hay que reintentar en
    vez de interpretar la respuesta."""
    params = {**params, "chainid": POLYGON_CHAIN_ID, "apikey": API_KEY}
    esperas = (0, 0.6, 1.2, 2.0, 3.0)
    for intento, espera in enumerate(esperas):
        if espera:
            time.sleep(espera)
        try:
            r = requests.get(ETHERSCAN_V2_BASE, params=params, timeout=15)
            j = r.json()
            if r.status_code == 429 or "rate limit" in str(j).lower():
                continue
            return j
        except Exception:
            if intento == len(esperas) - 1:
                return None
    return None


@st.cache_data(show_spinner=False, ttl=86400)
def read_owner(contract: str):
    """Lee owner() del contrato vía eth_call. Los vaults personales de Reental
    devuelven la wallet propietaria, lo que permite identificar el vault de forma
    determinista. Devuelve la dirección en minúsculas o None.

    Parseo estricto: solo se acepta un valor hexadecimal de 32 bytes; cualquier
    otra cosa (mensaje de rate-limit, revert, EOA sin código) devuelve None, para
    no confundir un mensaje de error con una dirección."""
    if not API_KEY or not contract:
        return None
    j = _etherscan_proxy({"module": "proxy", "action": "eth_call",
                          "to": contract, "data": OWNER_SELECTOR, "tag": "latest"})
    res = (j or {}).get("result", "")
    if isinstance(res, str) and res.startswith("0x") and len(res) == 66:
        return ("0x" + res[-40:]).lower()
    return None


@st.cache_data(show_spinner=False, ttl=86400)
def tx_es_dividendo(tx_hash: str) -> bool:
    """True si la transacción es un reparto de dividendos de Reental, identificado
    por su orquestador + método, sin depender del contrato distribuidor concreto
    (así se auto-detectan distribuidores nuevos que Reental introduzca)."""
    if not API_KEY:
        return False
    j = _etherscan_proxy({"module": "proxy", "action": "eth_getTransactionByHash",
                          "txhash": tx_hash})
    t = (j or {}).get("result", {}) or {}
    to     = (t.get("to") or "").lower()
    method = (t.get("input") or "0x")[:10].lower()
    return to == DIVIDEND_ORCHESTRATOR and method == DIVIDEND_METHOD_ID


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_normal_transactions(wallet: str) -> list:
    """Transacciones normales (ETH) de la wallet — necesarias para detectar el vault.
    Paginado: wallets muy activas pueden superar el límite de 1000 por llamada."""
    if not API_KEY:
        return []
    return fetch_all_account_txs(wallet, API_KEY, action="txlist")


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_distributor_recipients() -> set:
    """Direcciones que han recibido dividendos de los distribuidores conocidos
    (últimos ~10.000 envíos de cada uno). Solo se usa para PRIORIZAR candidatos en
    la detección del vault; no es un filtro duro, así que basta con cubrir los
    repartos recientes."""
    if not API_KEY:
        return set()
    recipients = set()
    for dist in DIVIDEND_DISTRIBUTORS:
        for page in range(1, 11):
            params = {
                "chainid": POLYGON_CHAIN_ID, "module": "account", "action": "tokentx",
                "address": dist,
                "startblock": 0, "endblock": 99999999, "sort": "desc",
                "page": page, "offset": 1000, "apikey": API_KEY,
            }
            try:
                r = requests.get(ETHERSCAN_V2_BASE, params=params, timeout=30)
                result = r.json().get("result")
                if not isinstance(result, list):
                    break
                recipients |= {tx["to"].lower() for tx in result}
                if len(result) < 1000:
                    break
            except Exception:
                break
    return recipients


def detect_vault_address(wallet: str) -> str | None:
    """
    Identifica el vault del inversor de forma determinista y a coste casi cero:
    el vault es el contrato al que la wallet ha enviado transacciones usando un
    método propio de vault (VAULT_METHOD_IDS).

    El método se lee del txlist (ya descargado), así que no dependemos del límite
    de 10.000 filas de la API ni de cuándo se cobró el último dividendo — el fallo
    de la lógica antigua, que cruzaba con los receptores recientes del distribuidor
    y se dejaba fuera los vaults con dividendos antiguos.

    owner() se usa para DESAMBIGUAR/descartar (si devuelve otra dirección, el
    contrato es de un tercero), pero NO para exigir confirmación: como el método
    ya es específico del vault, si owner() no se puede leer (p. ej. rate-limit en
    plena carga del análisis) se acepta igualmente el candidato, para no perder el
    vault por un fallo transitorio de la API.
    """
    wallet = wallet.lower()
    normal_txs = fetch_normal_transactions(wallet)
    if not normal_txs:
        return None

    # Candidatos primarios: destinos llamados con un método propio de vault.
    candidates, seen = [], set()
    for tx in normal_txs:
        if (tx["from"].lower() == wallet and tx.get("to")
                and (tx.get("methodId") or "").lower() in VAULT_METHOD_IDS):
            addr = tx["to"].lower()
            if addr not in seen:
                seen.add(addr)
                candidates.append(addr)

    tentativo = None
    for addr in candidates:
        owner = read_owner(addr)
        if owner == wallet:
            return addr                      # confirmado
        if owner is None and tentativo is None:
            tentativo = addr                 # no confirmable (rate-limit): el método ya es fiable
        # owner == otra dirección → contrato ajeno, se descarta
    if tentativo is not None:
        return tentativo

    # Respaldo: contratos llamados que aparecen como receptores de dividendos.
    called = {tx["to"].lower() for tx in normal_txs
              if tx.get("to") and tx["from"].lower() == wallet and tx["to"].lower() != wallet}
    for addr in (called & fetch_distributor_recipients()):
        if read_owner(addr) == wallet:
            return addr
    return None


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_vault_transfers(vault_address: str) -> list:
    """Obtiene todas las transferencias de stablecoin hacia el vault (paginado)."""
    if not API_KEY or not vault_address:
        return []
    return fetch_all_token_txs(vault_address, API_KEY)


def process_dividends(transfers: list, wallet: str, vault_transfers: list = None,
                      vault_addr: str = None) -> list:
    """
    Detecta pagos de dividendos de Reental hacia:
    a) la wallet directamente, y
    b) el vault personal del usuario (dividendos acumulados sin retirar).

    Un cobro se identifica como dividendo si la transferencia de stablecoin viene
    de un distribuidor conocido O si su transacción usa el orquestador+método de
    reparto (lo que auto-detecta distribuidores nuevos que Reental introduzca sin
    tener que hardcodearlos). La verificación por método se hace una sola vez por
    dirección de origen distinta para acotar las llamadas a la API.
    """
    wallet = wallet.lower()
    # Cachés locales de esta ejecución: fuentes ya clasificadas como (no) dividendo.
    es_dist = set(DIVIDEND_DISTRIBUTORS)
    no_dist = set()

    def _build_events(tx_list: list, recipient: str, destino_label: str) -> list:
        by_hash = defaultdict(list)
        for tx in tx_list:
            if (tx["to"].lower() == recipient
                    and tx["contractAddress"].lower() in STABLECOIN_CONTRACTS
                    and tx["from"].lower() != ZERO_ADDRESS):
                by_hash[tx["hash"]].append(tx)

        evs = []
        for tx_hash, group in by_hash.items():
            frm = group[0]["from"].lower()
            if frm in es_dist:
                pass
            elif frm in no_dist:
                continue
            elif tx_es_dividendo(tx_hash):
                es_dist.add(frm)          # distribuidor nuevo descubierto
            else:
                no_dist.add(frm)
                continue

            dt = datetime.utcfromtimestamp(int(group[0]["timeStamp"]))
            pagos, total, sym = [], 0.0, "USDT"
            for tx in group:
                contract = tx["contractAddress"].lower()
                sym_canon, dec = STABLECOIN_CONTRACTS[contract]
                amount = int(tx["value"]) / (10 ** dec)
                sym = sym_canon
                total += amount
                pagos.append(amount)
            pagos.sort(reverse=True)
            evs.append({
                "fecha": dt,
                "fecha_str": dt.strftime("%Y-%m-%d %H:%M"),
                "total": total,
                "sym": sym,
                "n_proyectos": len(pagos),
                "pagos": pagos,
                "destino": destino_label,
                "tx_link": f"https://polygonscan.com/tx/{tx_hash}",
            })
        return evs

    events = _build_events(transfers, wallet, "Wallet directa")

    if vault_transfers and vault_addr:
        events += _build_events(vault_transfers, vault_addr.lower(), "Vault personal")

    events.sort(key=lambda e: e["fecha"])
    return events


def burning_fee_pct(days_since_deposit: int) -> float:
    """Fee de quema al retirar del farming: 20% → 2% lineal en 180 días."""
    if days_since_deposit >= 180:
        return 2.0
    return round(max(2.0, 20.0 - (18.0 / 180.0) * days_since_deposit), 2)


def process_rnt_ecosystem(transfers: list, wallet: str, nft_transfers: list = None) -> list:
    """
    Detecta y clasifica todos los eventos del ecosistema RNT:
    staking, claim de staking, pool de liquidez, farming y claim de farming.
    nft_transfers: lista de tokennfttx para el contrato xRNT (STAKING_RECEIVER).
    """
    wallet = wallet.lower()
    if nft_transfers is None:
        nft_transfers = []

    # TXs relevantes: cualquiera con al menos un transfer de RNT, SLP, frmRNT o xRNT (NFT)
    relevant_hashes = {
        tx["hash"] for tx in transfers
        if tx["contractAddress"].lower() in RNT_ECOSYSTEM_CONTRACTS
    }
    # Incluir TXs de xRNT NFT aunque no aparezcan en tokentx
    for nft in nft_transfers:
        relevant_hashes.add(nft["hash"])

    if not relevant_hashes:
        return []

    # Indexar NFT transfers por hash y por tokenID
    nft_by_hash = defaultdict(list)
    nft_by_tokenid: dict[str, str] = {}   # tokenID → tx_hash que lo acuñó (para rastrear RNT stakeado)
    for nft in nft_transfers:
        nft_by_hash[nft["hash"]].append(nft)
        if nft.get("from", "").lower() == ZERO_ADDRESS:
            nft_by_tokenid[nft["tokenID"]] = nft["hash"]

    # Mapa tokenID → RNT stakeado (se rellena al procesar cada TX de staking)
    nft_rnt_map: dict[str, float] = {}

    grouped = defaultdict(list)
    for tx in transfers:
        if tx["hash"] in relevant_hashes:
            grouped[tx["hash"]].append(tx)

    # Asegurar que TXs de NFT sin entradas en tokentx también se procesan
    for tx_hash, nft_list in nft_by_hash.items():
        if tx_hash not in grouped:
            # Crear entradas sintéticas para tener el timestamp
            grouped[tx_hash] = []  # se usará nft_list para el timestamp

    events = []

    for tx_hash, group in grouped.items():
        rnt_in = rnt_out = 0.0
        slp_in = slp_out = 0.0
        frmrnt_in = frmrnt_out = 0.0
        usdt_in = usdt_out = 0.0
        usdt_sym = "USDT"
        rnt_from = rnt_to = None
        xrnt_minted        = False   # xRNT NFT acuñado al hacer staking propio
        xrnt_received      = False   # xRNT NFT recibido de otra wallet (staking cedido)
        xrnt_sender        = None    # quién cedió el NFT
        xrnt_received_tids = []      # tokenIDs recibidos (para lookup de RNT)
        xrnt_sent          = False   # xRNT NFT enviado a otra wallet
        xrnt_dest          = None

        # Detectar xRNT NFT transfers desde tokennfttx
        for nft in nft_by_hash.get(tx_hash, []):
            nft_from = nft.get("from", "").lower()
            nft_to   = nft.get("to", "").lower()
            if nft_from == ZERO_ADDRESS and nft_to == wallet:
                xrnt_minted = True
            elif nft_from != ZERO_ADDRESS and nft_to == wallet:
                xrnt_received = True
                xrnt_sender   = nft_from
                xrnt_received_tids.append(nft.get("tokenID", ""))
            elif nft_from == wallet and nft_to != ZERO_ADDRESS:
                xrnt_sent = True
                xrnt_dest = nft_to

        for tx in group:
            contract = tx["contractAddress"].lower()
            dec = int(tx["tokenDecimal"]) if tx["tokenDecimal"] else 18
            value = int(tx["value"]) / (10 ** dec)
            from_addr = tx["from"].lower()
            to_addr   = tx["to"].lower()

            if contract == RNT_CONTRACT:
                if to_addr == wallet:
                    rnt_in += value
                    rnt_from = from_addr
                elif from_addr == wallet:
                    rnt_out += value
                    rnt_to = to_addr
            elif contract == SLP_CONTRACT:
                if to_addr == wallet:
                    slp_in += value
                elif from_addr == wallet:
                    slp_out += value
            elif contract == FRMRNT_CONTRACT:
                if to_addr == wallet:
                    frmrnt_in += value
                elif from_addr == wallet:
                    frmrnt_out += value
            elif contract == STAKING_RECEIVER:
                # ERC-721 NFT de staking (xRNT): value=0, identificado por el tokenId en la API
                if to_addr == wallet and from_addr == ZERO_ADDRESS:
                    xrnt_minted = True
                elif to_addr == wallet and from_addr != ZERO_ADDRESS:
                    xrnt_received = True
                    xrnt_sender   = from_addr
                    tid = tx.get("tokenID") or tx.get("tokenId", "")
                    if tid:
                        xrnt_received_tids.append(tid)
                elif from_addr == wallet and to_addr != ZERO_ADDRESS:
                    xrnt_sent = True
                    xrnt_dest = to_addr
            elif contract in STABLECOIN_CONTRACTS:
                sym2, dec2 = STABLECOIN_CONTRACTS[contract]
                val2 = int(tx["value"]) / (10 ** dec2)
                if to_addr == wallet:
                    usdt_in += val2
                    usdt_sym = sym2
                elif from_addr == wallet:
                    usdt_out += val2
                    usdt_sym = sym2

        # Obtener timestamp de tokentx o de tokennfttx como fallback
        if group:
            ts = int(group[0]["timeStamp"])
        elif nft_by_hash.get(tx_hash):
            ts = int(nft_by_hash[tx_hash][0].get("timeStamp", 0))
        else:
            continue
        dt = datetime.utcfromtimestamp(ts)
        tx_link = f"https://polygonscan.com/tx/{tx_hash}"

        ev = None

        if rnt_in > 0 and slp_in > 0 and rnt_from == STAKING_CLAIM:
            usdt_note = f" + {usdt_in:,.2f} {usdt_sym}" if usdt_in > 0 else ""
            ev = {
                "tipo": "Claim rewards de staking",
                "detalle": f"Recibido: {rnt_in:,.4f} RNT + {slp_in:,.6f} SLP{usdt_note}",
                "rnt_delta": rnt_in, "slp_delta": slp_in, "frmrnt_delta": 0.0, "usdt_delta": usdt_in,
            }
        elif rnt_out > 0 and rnt_to == STAKING_RECEIVER:
            # Registrar cuánto RNT se stakeó en cada NFT para poder restarlo si se transfiere
            for nft in nft_by_hash.get(tx_hash, []):
                if nft.get("from", "").lower() == ZERO_ADDRESS and nft.get("to", "").lower() == wallet:
                    nft_rnt_map[nft["tokenID"]] = rnt_out
            ev = {
                "tipo": "Staking de RNT",
                "detalle": f"Depositado: {rnt_out:,.4f} RNT → recibido xRNT NFT",
                "rnt_delta": -rnt_out, "slp_delta": 0.0, "frmrnt_delta": 0.0, "usdt_delta": 0.0,
                "xrnt_staked_delta": rnt_out, "xrnt_count_delta": 1,
            }
        elif slp_out > 0 and frmrnt_in > 0:
            # Depositar LP en farming (puede coincidir con claim de rewards en la misma TX)
            rnt_note = f" + claim {rnt_in:,.4f} RNT" if rnt_in > 0 and rnt_from == FARMING_CLAIM else ""
            ev = {
                "tipo": "Depositar LP en farming",
                "detalle": f"Depositado: {slp_out:,.6f} SLP → Recibido: {frmrnt_in:,.6f} frmRNT{rnt_note}",
                "rnt_delta": rnt_in if rnt_from == FARMING_CLAIM else 0.0,
                "slp_delta": -slp_out, "frmrnt_delta": frmrnt_in, "usdt_delta": 0.0,
            }
        elif rnt_in > 0 and rnt_from == FARMING_CLAIM:
            ev = {
                "tipo": "Claim rewards de farming",
                "detalle": f"Recibido: {rnt_in:,.4f} RNT",
                "rnt_delta": rnt_in, "slp_delta": 0.0, "frmrnt_delta": 0.0, "usdt_delta": 0.0,
            }
        elif rnt_out > 0 and usdt_out > 0 and slp_in > 0:
            frmrnt_note = f" + {frmrnt_in:,.6f} frmRNT" if frmrnt_in > 0 else ""
            ev = {
                "tipo": "Añadir liquidez al pool RNT/USDT",
                "detalle": (f"Aportado: {rnt_out:,.4f} RNT + {usdt_out:,.2f} {usdt_sym}"
                            f" → Recibido: {slp_in:,.6f} SLP{frmrnt_note}"),
                "rnt_delta": -rnt_out, "slp_delta": slp_in, "frmrnt_delta": frmrnt_in, "usdt_delta": -usdt_out,
            }
        elif frmrnt_out > 0 and slp_in > 0:
            rnt_note = f" + {rnt_in:,.4f} RNT" if rnt_in > 0 else ""
            ev = {
                "tipo": "Retirar LP de farming",
                "detalle": f"Retirado: {frmrnt_out:,.6f} frmRNT → Recibido: {slp_in:,.6f} SLP{rnt_note}",
                "rnt_delta": rnt_in, "slp_delta": slp_in, "frmrnt_delta": -frmrnt_out, "usdt_delta": 0.0,
            }
        elif slp_out > 0 and rnt_in > 0 and usdt_in > 0:
            ev = {
                "tipo": "Retirar liquidez del pool RNT/USDT",
                "detalle": (f"Retirado: {slp_out:,.6f} SLP"
                            f" → Recibido: {rnt_in:,.4f} RNT + {usdt_in:,.2f} {usdt_sym}"),
                "rnt_delta": rnt_in, "slp_delta": -slp_out, "frmrnt_delta": 0.0, "usdt_delta": usdt_in,
            }
        elif rnt_in > 0:
            ev = {
                "tipo": "Recepción de RNT",
                "detalle": f"Recibido: {rnt_in:,.4f} RNT",
                "rnt_delta": rnt_in, "slp_delta": 0.0, "frmrnt_delta": 0.0, "usdt_delta": 0.0,
            }
        elif rnt_out > 0:
            dest = f"{rnt_to[:8]}…{rnt_to[-4:]}" if rnt_to else "—"
            ev = {
                "tipo": "Envío de RNT",
                "detalle": f"Enviado: {rnt_out:,.4f} RNT a {dest}",
                "rnt_delta": -rnt_out, "slp_delta": 0.0, "frmrnt_delta": 0.0, "usdt_delta": 0.0,
            }
        elif xrnt_received:
            sender_str = f"{xrnt_sender[:8]}…{xrnt_sender[-4:]}" if xrnt_sender else "—"
            # Buscar el RNT stakeado consultando la TX de mint original de cada tokenID
            received_rnt = 0.0
            for tid in xrnt_received_tids:
                rnt_mapped = nft_rnt_map.get(tid, 0.0)
                if rnt_mapped > 0:
                    received_rnt += rnt_mapped
                else:
                    received_rnt += fetch_xrnt_staked_rnt(tid)
            rnt_str = f"{received_rnt:,.4f} RNT en staking" if received_rnt > 0 else "RNT en staking no determinado"
            ev = {
                "tipo": "Recepción de xRNT (posición de staking cedida)",
                "detalle": (f"NFT de staking recibido de {sender_str} · {rnt_str}"
                            f" *"),
                "nota_asterisco": "* Recepción del NFT de staking por compra vía FIAT o por cesión de un tercero.",
                "rnt_delta": 0.0, "slp_delta": 0.0, "frmrnt_delta": 0.0, "usdt_delta": 0.0,
                "xrnt_staked_delta": received_rnt, "xrnt_count_delta": len(xrnt_received_tids) or 1,
            }
        elif xrnt_sent:
            dest_str = f"{xrnt_dest[:8]}…{xrnt_dest[-4:]}" if xrnt_dest else "—"
            # Calcular el RNT que se va con el NFT transferido
            transferred_rnt = 0.0
            for nft in nft_by_hash.get(tx_hash, []):
                if nft.get("from", "").lower() == wallet:
                    transferred_rnt += nft_rnt_map.get(nft["tokenID"], 0.0)
            n_sent = len([n for n in nft_by_hash.get(tx_hash, []) if n.get("from", "").lower() == wallet])
            ev = {
                "tipo": "Envío de xRNT (posición de staking)",
                "detalle": (f"NFT de staking transferido a {dest_str}"
                            + (f" ({transferred_rnt:,.4f} RNT)" if transferred_rnt > 0 else "")),
                "rnt_delta": 0.0, "slp_delta": 0.0, "frmrnt_delta": 0.0, "usdt_delta": 0.0,
                "xrnt_staked_delta": -transferred_rnt, "xrnt_count_delta": -(n_sent or 1),
            }
        elif slp_in > 0 or slp_out > 0 or frmrnt_in > 0 or frmrnt_out > 0:
            ev = {
                "tipo": "Movimiento de LP/frmRNT",
                "detalle": (f"SLP: {slp_in-slp_out:+.6f}  frmRNT: {frmrnt_in-frmrnt_out:+.6f}"),
                "rnt_delta": 0.0, "slp_delta": slp_in - slp_out,
                "frmrnt_delta": frmrnt_in - frmrnt_out, "usdt_delta": 0.0,
            }

        if ev:
            ev.update({"fecha": dt, "fecha_str": dt.strftime("%Y-%m-%d %H:%M"), "tx_link": tx_link})
            events.append(ev)

    events.sort(key=lambda e: e["fecha"])
    return events


def calculate_irr(cash_flows: list, max_iter: int = 1000, tol: float = 1e-8):
    """
    Calcula la TIR anualizada usando Newton-Raphson.

    cash_flows: lista de (datetime, float) — negativo = salida de dinero, positivo = entrada.
    Devuelve la tasa anual como decimal (0.05 = 5 %) o None si no converge.
    """
    if not cash_flows:
        return None
    positivos = [v for _, v in cash_flows if v > 0]
    negativos = [v for _, v in cash_flows if v < 0]
    if not positivos or not negativos:
        return None

    t0 = cash_flows[0][0]

    def years(dt):
        return (dt - t0).days / 365.25

    def npv(r):
        return sum(cf / ((1 + r) ** years(dt)) for dt, cf in cash_flows)

    def dnpv(r):
        # Derivada analítica de NPV respecto a r
        result = 0.0
        for dt, cf in cash_flows:
            t = years(dt)
            if t > 0:
                result -= t * cf / ((1 + r) ** (t + 1))
        return result

    # Probar varios puntos de partida para evitar mínimos locales
    for r0 in (0.1, 0.5, -0.1, 0.01, 2.0):
        r = r0
        for _ in range(max_iter):
            f  = npv(r)
            fp = dnpv(r)
            if abs(fp) < 1e-15:
                break
            step = f / fp
            r -= step
            r = max(r, -0.9999)   # evitar división por cero
            if abs(step) < tol:
                if -0.9999 < r < 50:
                    return r
                break
    return None


@st.cache_data(show_spinner=False, ttl=300)
def get_rnt_price_usdt() -> float:
    """Obtiene el precio actual de RNT en USD desde CoinGecko. Devuelve 0.0 si no está disponible."""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "reental", "vs_currencies": "usd"},
            timeout=10,
        )
        r.raise_for_status()
        return float(r.json().get("reental", {}).get("usd", 0.0))
    except Exception:
        return 0.0


@st.cache_data(show_spinner=False, ttl=3600)
@st.cache_data(show_spinner=False, ttl=21600)
def _eurusd_history() -> dict:
    """Tipo de cambio EUR/USD diario de los últimos 365 días, en UNA sola
    llamada (`market_chart/range` de Tether en EUR, invertido) en vez de una
    petición por fecha — el mismo problema de rate-limit que en el precio de
    RNT: consultar fecha a fecha agota el límite del plan gratuito de
    CoinGecko a partir de la 6ª-7ª llamada seguida. Devuelve {"YYYY-MM-DD": tipo}."""
    try:
        hoy = datetime.utcnow()
        desde = hoy - timedelta(days=364)
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/tether/market_chart/range",
            params={"vs_currency": "eur", "from": int(desde.timestamp()), "to": int(hoy.timestamp())},
            timeout=20,
        )
        r.raise_for_status()
        # Tether (USDT) en EUR nos da 1/EURUSD → invertimos
        return {
            datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d"): round(1.0 / float(p), 4)
            for ts, p in r.json().get("prices", []) if p
        }
    except Exception:
        return {}


def get_eurusd_on_date(date_str: str) -> float:
    """Tipo de cambio EUR/USD (USD por 1 EUR) para una fecha dada (YYYY-MM-DD).
    Si la fecha excede la ventana de 365 días de CoinGecko o el histórico no se
    pudo obtener, usa como último recurso el tipo de cambio ACTUAL vía
    Frankfurter/BCE — una aproximación, no el tipo exacto de esa fecha."""
    rate = _eurusd_history().get(date_str[:10])
    if rate:
        return rate
    try:
        r2 = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "EUR", "to": "USD"},
            timeout=10,
        )
        return float(r2.json()["rates"]["USD"])
    except Exception:
        return None


def fiat_values(valor_usd, date_str):
    """Convierte un importe en USD a (usd, eur, tipo_eur_usd) a la fecha dada.

    `date_str` puede ser 'YYYY-MM-DD' o 'YYYY-MM-DD HH:MM' (se recorta a los 10
    primeros caracteres). El tipo devuelto es USD por 1 EUR (p.ej. 1.1422), así
    que EUR = USD / tipo. Si no hay importe o no se obtiene el tipo, el EUR y el
    tipo se devuelven como cadena vacía para no falsear el informe."""
    if valor_usd is None or valor_usd == "":
        return valor_usd, "", ""
    try:
        usd = float(valor_usd)
    except (TypeError, ValueError):
        return valor_usd, "", ""
    rate = get_eurusd_on_date(date_str[:10]) if date_str else None
    if not rate:
        return round(usd, 2), "", ""
    return round(usd, 2), round(usd / rate, 2), rate


# ── UI ───────────────────────────────────────────────────────────────────────

MAX_WALLETS = 5

if "wallet_slots" not in st.session_state:
    st.session_state.wallet_slots = [{"address": "", "alias": ""}]

st.caption(
    "Añade una o varias wallets. Si añades más de una, la cartera se muestra combinada "
    "y los movimientos entre tus propias wallets se identifican automáticamente como "
    "transferencias internas (no como compra/venta)."
)

for idx, slot in enumerate(st.session_state.wallet_slots):
    c_addr, c_alias, c_del = st.columns([3, 2, 1])
    with c_addr:
        st.session_state.wallet_slots[idx]["address"] = st.text_input(
            "🔑 Dirección de la wallet" if idx == 0 else " ",
            value=slot["address"], placeholder="0x1234abcd...",
            key=f"aw_addr_{idx}", label_visibility="visible" if idx == 0 else "collapsed",
        )
    with c_alias:
        st.session_state.wallet_slots[idx]["alias"] = st.text_input(
            "🏷️ Alias (opcional)" if idx == 0 else " ",
            value=slot["alias"], placeholder="ej. Wallet principal",
            key=f"aw_alias_{idx}", label_visibility="visible" if idx == 0 else "collapsed",
        )
    with c_del:
        if idx == 0:
            st.markdown("&nbsp;", unsafe_allow_html=True)
        if len(st.session_state.wallet_slots) > 1:
            if st.button("✕", key=f"aw_del_{idx}", help="Quitar esta wallet", use_container_width=True):
                st.session_state.wallet_slots.pop(idx)
                st.rerun()

if len(st.session_state.wallet_slots) < MAX_WALLETS:
    if st.button("➕ Añadir otra wallet"):
        st.session_state.wallet_slots.append({"address": "", "alias": ""})
        st.rerun()
else:
    st.caption(f"Máximo {MAX_WALLETS} wallets simultáneas (más ralentizaría demasiado la carga).")

filter_date = st.date_input("📅 Filtrar por fecha (opcional — saldo a esa fecha)", value=None)
analyze_btn = st.button("🔍 Analizar cartera", type="primary", use_container_width=True)

if analyze_btn:
    entradas = []
    for slot in st.session_state.wallet_slots:
        addr = slot["address"].strip().lower()
        if not addr:
            continue
        if not addr.startswith("0x") or len(addr) != 42:
            st.error(f"Dirección de wallet inválida: {slot['address']}")
            st.stop()
        entradas.append((addr, slot["alias"].strip()))

    if not entradas:
        st.error("Introduce al menos una dirección de wallet válida (0x... 42 caracteres).")
        st.stop()

    # Deduplicar direcciones repetidas (misma wallet en varias filas) conservando la primera
    seen_addrs, entradas_unicas = set(), []
    for addr, alias in entradas:
        if addr in seen_addrs:
            continue
        seen_addrs.add(addr)
        entradas_unicas.append((addr, alias))
    entradas = entradas_unicas

    own_wallets = {addr: (alias or f"Wallet {addr[:8]}") for addr, alias in entradas}

    progress_bar = st.progress(0, text="⏳ Iniciando análisis…")
    progress_bar.progress(5, text="📋 Cargando lista de tokens de Reental…")
    known_tokens = load_tokens()
    reental_addresses = load_reental_addresses()

    merged_token_data = {}
    raw_transfers_by_wallet = {}
    vault_by_wallet = {}
    n = len(entradas)

    for i, (addr, _alias) in enumerate(entradas):
        wallet_alias = own_wallets[addr]
        base_pct = 10 + int(75 * i / n)

        progress_bar.progress(base_pct, text=f"🔗 [{wallet_alias}] Consultando historial de transacciones…")
        try:
            transfers = fetch_token_transfers(addr)
        except Exception as e:
            progress_bar.empty()
            st.error(f"Error al consultar Polygonscan para «{wallet_alias}» ({addr}): {e}")
            st.stop()
        raw_transfers_by_wallet[addr] = transfers

        progress_bar.progress(base_pct + 3, text=f"🔍 [{wallet_alias}] Procesando {len(transfers)} transferencias…")
        wallet_token_data = process_transfers(transfers, addr, known_tokens, reental_addresses, own_wallets, wallet_alias)
        for contract, data in wallet_token_data.items():
            merged = merged_token_data.setdefault(contract, {"movements": [], "balance": 0.0, "info": data["info"]})
            merged["movements"].extend(data["movements"])
            merged["balance"] += data["balance"]

        progress_bar.progress(base_pct + 6, text=f"🏦 [{wallet_alias}] Buscando vault personal…")
        vault_addr_w = detect_vault_address(addr)
        vault_transfers_w = fetch_vault_transfers(vault_addr_w) if vault_addr_w else []
        vault_by_wallet[addr] = {"vault_addr": vault_addr_w, "vault_transfers": vault_transfers_w}

    for data in merged_token_data.values():
        data["movements"].sort(key=lambda m: m["fecha"])

    progress_bar.progress(95, text="📊 Preparando resultados…")
    st.session_state.update({
        "token_data": merged_token_data,
        "own_wallets": own_wallets,
        "wallets_analyzed": entradas,
        "wallet": entradas[0][0],   # wallet "primaria": solo para compatibilidad en enlaces a Polygonscan
        "filter_date": filter_date,
        "raw_transfers_by_wallet": raw_transfers_by_wallet,
        "vault_by_wallet": vault_by_wallet,
    })
    progress_bar.progress(100, text="✅ Análisis completado")
    progress_bar.empty()

if "token_data" not in st.session_state:
    st.stop()

token_data = st.session_state["token_data"]
own_wallets = st.session_state.get("own_wallets", {})
wallets_analyzed = st.session_state.get("wallets_analyzed", [])
wallet = st.session_state["wallet"]
filter_date = st.session_state["filter_date"]
es_multi_wallet = len(wallets_analyzed) > 1

if not token_data:
    st.info("No se encontraron tokens de Reental en " + ("estas wallets." if es_multi_wallet else "esta wallet."))
    st.stop()

cutoff = filter_date if filter_date else date.today()
use_date_filter = bool(filter_date)

activos, historicos = {}, {}
for contract, data in token_data.items():
    bal = round(balance_at_date(data["movements"], cutoff) if use_date_filter else data["balance"], 6)
    if bal > 0.000001:
        activos[contract] = {**data, "balance_display": bal}
    elif not data["info"].get("is_aave"):
        historicos[contract] = {**data, "balance_display": bal}

# ── Resumen ──────────────────────────────────────────────────────────────────
st.markdown("---")
date_label = f" a fecha **{filter_date}**" if use_date_filter else ""
st.subheader(f"📊 Resumen de cartera a la fecha indicada{date_label}")
if es_multi_wallet:
    _wallets_str = " · ".join(f"«{alias}» `{addr[:8]}…{addr[-4:]}`" for addr, alias in wallets_analyzed)
    st.caption(f"Cartera combinada de {len(wallets_analyzed)} wallets: {_wallets_str}")

all_dates = [m["fecha"] for d in token_data.values() for m in d["movements"]]
primera_fecha = min(all_dates).strftime("%d/%m/%Y") if all_dates else "—"

def kpi_card(icon, label, value, value_color="#1e293b", sublabel="", badge=""):
    return f"""
    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;
                padding:16px 18px;display:flex;flex-direction:column;gap:4px;">
      <div style="font-size:0.72rem;font-weight:600;color:#64748b;
                  letter-spacing:0.05em;text-transform:uppercase;">{icon}&nbsp;{label}</div>
      <div style="font-size:1.45rem;font-weight:700;color:{value_color};
                  line-height:1.2;">{value}</div>
      <div style="font-size:0.72rem;color:#94a3b8;">{sublabel}&nbsp;{badge}</div>
    </div>"""

c1, c2, c3, c4 = st.columns(4)
c1.markdown(kpi_card("🏠", "Tokens con saldo",        str(len(activos)),   sublabel="posiciones activas"),   unsafe_allow_html=True)
c2.markdown(kpi_card("📁", "Tokens históricos",        str(len(historicos)), sublabel="saldo cero"),          unsafe_allow_html=True)
c3.markdown(kpi_card("🔁", "Total movimientos",        str(sum(len(d["movements"]) for d in token_data.values())), sublabel="en esta wallet"), unsafe_allow_html=True)
c4.markdown(kpi_card("📅", "Primer token inmobiliario", primera_fecha,       sublabel="fecha de entrada"),    unsafe_allow_html=True)

# ── KPIs de valor de cartera ──────────────────────────────────────────────────
_tokens_eur = sum(d["balance_display"] for d in activos.values() if d["info"].get("divisa") == "EUR")
_tokens_usd = sum(d["balance_display"] for d in activos.values() if d["info"].get("divisa") != "EUR")

# Valor a precio de emisión
_valor_usd_part = sum(
    d["balance_display"] * (d["info"].get("precio_emision") or 0.0)
    for d in activos.values() if d["info"].get("divisa") != "EUR"
)
_valor_eur_part = sum(
    d["balance_display"] * (d["info"].get("precio_emision") or 0.0)
    for d in activos.values() if d["info"].get("divisa") == "EUR"
)

# Tipo de cambio EUR→USD en la fecha de análisis
_fx_date_str = str(filter_date) if filter_date else str(date.today())
_eurusd = get_eurusd_on_date(_fx_date_str)
_eurusd_label = f"{_eurusd:.4f}" if _eurusd else "N/D"

if _eurusd:
    _valor_total_usd = _valor_usd_part + _valor_eur_part * _eurusd
    _valor_total_str = f"${_valor_total_usd:,.2f}"
    _nota_valor = (
        f"USD directos: ${_valor_usd_part:,.2f} · "
        f"EUR convertidos: €{_valor_eur_part:,.2f} × {_eurusd_label} = ${_valor_eur_part * _eurusd:,.2f}"
    )
else:
    _valor_total_str = "N/D"
    _nota_valor = "No se pudo obtener el tipo de cambio EUR/USD para esta fecha."

st.markdown("<div style='margin-top:18px'></div>", unsafe_allow_html=True)
_kc1, _kc2, _kc3 = st.columns(3)
_kc1.markdown(kpi_card("🇺🇸", "Nº de tokens de proyectos emitidos en USD",
                        f"{_tokens_usd:,.4f}".rstrip("0").rstrip("."),
                        sublabel="suma de saldos activos en proyectos USD"), unsafe_allow_html=True)
_kc2.markdown(kpi_card("🇪🇺", "Nº de tokens de proyectos emitidos en EUR",
                        f"{_tokens_eur:,.4f}".rstrip("0").rstrip("."),
                        sublabel="suma de saldos activos en proyectos EUR"), unsafe_allow_html=True)
_kc3.markdown(kpi_card("💼", "Valor a precio emisión",  _valor_total_str,
                        sublabel=f"tipo cambio EUR/USD {_eurusd_label} · fecha {_fx_date_str}"),
              unsafe_allow_html=True)

with st.expander("ℹ️ ¿Cómo se calcula el valor a precio de emisión?"):
    if _eurusd:
        _eur_en_usd = _valor_eur_part * _eurusd
        st.markdown(f"""
Este KPI estima el **valor total de la cartera en dólares (USD)** usando el precio al que se emitió cada token inmobiliario:

**Proyectos en USD** → nº de tokens × precio de emisión (USD)

**Proyectos en EUR** → nº de tokens × precio de emisión (EUR) convertido a USD aplicando el tipo de cambio EUR/USD del día {_fx_date_str}

Tipo de cambio usado: **1 EUR = {_eurusd_label} USD** (fuente: CoinGecko / Frankfurter/BCE)

**Resultado:**
- Parte USD: **${_valor_usd_part:,.2f}**
- Parte EUR: **€{_valor_eur_part:,.2f}** × {_eurusd_label} = **${_eur_en_usd:,.2f}**
- **Total estimado: {_valor_total_str}**

⚠️ Este valor usa el precio de **emisión original**, no el precio de mercado actual ni el precio OTC.
        """)
    else:
        st.warning(_nota_valor)

# ── Tokens con saldo ─────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("✅ Tokens con saldo" + date_label)
closing_dates = load_closing_dates()

def parse_fecha(s):
    try:
        return datetime.strptime(s, "%d/%m/%Y").date()
    except Exception:
        return None

def polygonscan_token_link(token_addr: str) -> str:
    """Enlace al token en Polygonscan. Con varias wallets no se filtra por dirección
    (el filtro ?a= solo tendría sentido para una wallet concreta)."""
    if es_multi_wallet:
        return f"https://polygonscan.com/token/{token_addr}"
    return f"https://polygonscan.com/token/{token_addr}?a={wallet}"


if activos:
    today_str = date.today().strftime("%d/%m/%Y")

    rows = []
    for d in sorted(activos.values(), key=lambda x: x["info"]["label"]):
        fecha_fin = get_fecha_fin_display(d["info"], closing_dates)
        base_row = {
            "Token": d["info"]["label"],
            "Nombre": d["info"]["name"],
            "Tipo": "🏦 Token Inmobiliario Colateralizado" if d["info"].get("is_aave") else "🏠 Token Inmobiliario",
            "Fecha real de fin de proyecto": fecha_fin,
            "Ver en Polygonscan": polygonscan_token_link(d["info"]["address"]),
        }
        if es_multi_wallet:
            # Un token puede estar repartido entre varias wallets: una fila por wallet con saldo.
            desglose = balances_por_wallet(d["movements"], cutoff, use_date_filter)
            for w_addr, w_info in sorted(desglose.items(), key=lambda x: x[1]["alias"]):
                if round(w_info["balance"], 6) <= 0.000001:
                    continue
                n_mov = sum(1 for m in d["movements"] if m.get("wallet_addr") == w_addr
                            and (not use_date_filter or m["fecha"].date() <= cutoff))
                rows.append({
                    **base_row,
                    "Wallet": w_info["alias"],
                    "Saldo": round(w_info["balance"], 6),
                    "Nº mov.": n_mov,
                })
        else:
            rows.append({
                **base_row,
                "Saldo": d["balance_display"],
                "Nº mov.": len(d["movements"]),
            })

    df_activos = pd.DataFrame(rows)

    def style_fecha_fin(val):
        d = parse_fecha(str(val))
        if d and d < date.today():
            return "background-color: #ffd6d6; color: #a00000; font-weight: 500"
        return ""

    styled_activos = df_activos.style.applymap(style_fecha_fin, subset=["Fecha real de fin de proyecto"])
    st.dataframe(styled_activos, column_config={
        "Ver en Polygonscan": st.column_config.LinkColumn(width="small"),
        "Nº mov.": st.column_config.NumberColumn(width="small"),
        "Tipo": st.column_config.TextColumn(width="small"),
        "Saldo": st.column_config.NumberColumn(width="small"),
        "Wallet": st.column_config.TextColumn(width="small"),
        "Fecha real de fin de proyecto": st.column_config.TextColumn(width="medium"),
    }, hide_index=True, use_container_width=True)
    if es_multi_wallet:
        st.caption("Un mismo token puede aparecer en varias filas si está repartido entre distintas wallets.")
else:
    st.info("No hay tokens con saldo" + (" a esa fecha." if use_date_filter else "."))

# ── Tokens históricos ─────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("🗂️ Tokens históricos (saldo cero" + date_label + ")")
if historicos:
    rows = [{
        "Token": d["info"]["label"],
        "Nombre": d["info"]["name"],
        "Tipo": "🏦 Token Inmobiliario Colateralizado" if d["info"].get("is_aave") else "🏠 Token Inmobiliario",
        "Primer movimiento": d["movements"][0]["fecha_str"],
        "Último movimiento": d["movements"][-1]["fecha_str"],
        "Fecha real de fin de proyecto": get_fecha_fin_display(d["info"], closing_dates),
        "Nº mov.": len(d["movements"]),
        "Ver en Polygonscan": polygonscan_token_link(d["info"]["address"]),
    } for d in sorted(historicos.values(), key=lambda x: x["info"]["label"])]
    def style_fecha_fin_historicos(val):
        d = parse_fecha(str(val))
        if d and d < date.today():
            return "background-color: #d4edda; color: #155724; font-weight: 500"
        return ""

    df_historicos = pd.DataFrame(rows)
    styled_historicos = df_historicos.style.applymap(
        style_fecha_fin_historicos, subset=["Fecha real de fin de proyecto"]
    )
    st.dataframe(styled_historicos, column_config={
        "Ver en Polygonscan": st.column_config.LinkColumn(width="small"),
        "Fecha real de fin de proyecto": st.column_config.TextColumn(width="medium"),
    }, hide_index=True, use_container_width=True)
else:
    st.info("No hay tokens históricos.")

# ── Filtro de wallet (solo detalle; los KPIs y el gráfico siempre son agregados) ──
selected_wallet_filter = None   # None = todas las wallets combinadas
if es_multi_wallet:
    st.markdown("---")
    opciones_wallet = ["Todas (agregado)"] + [alias for _, alias in wallets_analyzed]
    eleccion = st.selectbox(
        "🔍 Ver detalle de:", opciones_wallet,
        help="Los KPIs, el resumen y el gráfico de evolución de más arriba siempre muestran el agregado de todas tus wallets. "
             "Este filtro solo afecta a las tablas de detalle de más abajo.",
    )
    if eleccion != "Todas (agregado)":
        selected_wallet_filter = next(addr for addr, alias in wallets_analyzed if alias == eleccion)


def filtrar_por_wallet(items: list) -> list:
    """Filtra una lista de movimientos/eventos por la wallet elegida (None = sin filtrar)."""
    if selected_wallet_filter is None:
        return items
    return [it for it in items if it.get("wallet_addr") == selected_wallet_filter]


# ── Movimientos por token ─────────────────────────────────────────────────────
st.markdown("---")
st.subheader("📋 Movimientos por token")
st.caption("Todos los movimientos históricos del token seleccionado, con saldo acumulado de tokens y de stablecoins.")

all_tokens_sorted = sorted(
    token_data.items(),
    key=lambda x: (x[1]["balance"] <= 0, x[1]["info"]["label"]),
)
option_labels = [
    f"{d['info']['label']} — {d['info']['name']}{'  ✅' if d['balance'] > 0.000001 else '  🗂️'}"
    for _, d in all_tokens_sorted
]
selected_label = st.selectbox("Selecciona un token:", options=option_labels, key="token_selector")
selected_idx = option_labels.index(selected_label)
_, selected_data = all_tokens_sorted[selected_idx]
movs = filtrar_por_wallet(selected_data["movements"])

if movs:
    rows = []
    saldo_tokens = 0.0
    saldo_stable = 0.0
    for m in movs:
        saldo_tokens += m["cantidad_neta"]
        if m["stable_in"] > 0 and m["cantidad_neta"] < 0:
            saldo_stable += m["stable_in"]
        elif m["stable_out"] > 0 and m["cantidad_neta"] > 0:
            saldo_stable -= m["stable_out"]

        tokens_col = f"+{m['cantidad']:,.4f}" if m["cantidad_neta"] > 0 else f"-{m['cantidad']:,.4f}"

        if m["cantidad_neta"] < 0 and m["stable_in"] > 0:
            stable_col = f"+{m['stable_in']:,.2f} {m['stable_symbol']}"
        elif m["cantidad_neta"] > 0 and m["stable_out"] > 0:
            stable_col = f"-{m['stable_out']:,.2f} {m['stable_symbol']}"
        else:
            stable_col = "—"

        row = {
            "Fecha": m["fecha_str"],
            "Operación": m["tipo"],
            "Origen": m["origen"],
            "Destino": m["destino"],
            "Tokens": tokens_col,
            "Saldo tokens": round(saldo_tokens, 4),
            "Stablecoin": stable_col,
            "Saldo USDT": f"{saldo_stable:,.2f}",
            "TX": m["tx_link"],
        }
        if es_multi_wallet:
            row["Wallet"] = m.get("wallet_alias", "—")
        rows.append(row)

    df_movs = pd.DataFrame(rows)

    def style_address_cell(val):
        if val == "Tu wallet" or str(val).startswith("Tu wallet «") or val in own_wallets.values():
            return "background-color: #d4edda; color: #2d6a4f; font-weight: 500"
        if val == "Wallet Reental":
            return "background-color: #fde8c8; color: #8a4a00; font-weight: 500"
        return ""

    styled = df_movs.style.applymap(style_address_cell, subset=["Origen", "Destino"])
    st.dataframe(styled, column_config={
        "TX": st.column_config.LinkColumn("Ver TX", width="small"),
    }, hide_index=True, use_container_width=True)

    has_entrada_sin_usdt = any("Entrada de Tokens *" in str(r.get("Operación", "")) for r in rows)
    has_salida_sin_usdt  = any("Salida de Tokens *"  in str(r.get("Operación", "")) for r in rows)
    has_transferencia_interna = any("Transferencia interna" in str(r.get("Operación", "")) for r in rows)
    if has_entrada_sin_usdt or has_salida_sin_usdt or has_transferencia_interna:
        partes = []
        if has_transferencia_interna:
            partes.append(
                "**Transferencia interna**: movimiento entre tus propias wallets (declaradas arriba). "
                "No se contabiliza como compra ni venta en el rendimiento acumulado ni en el informe fiscal."
            )
        if has_entrada_sin_usdt:
            partes.append(
                "**Entrada de Tokens \\***: no se identificó contraparte de USDT en la misma transacción. "
                "Posibles causas: (1) compra con dinero FIAT (el pago queda en el banco, no en blockchain); "
                "(2) el USDT fue enviado en una transacción separada; "
                "(3) envío interno entre billeteras del propio inversor no declaradas arriba."
            )
        if has_salida_sin_usdt:
            partes.append(
                "**Salida de Tokens \\***: los tokens salieron de tu billetera sin una contrapartida en USDT o USDC "
                "en la misma transacción por alguna de las siguientes razones: (1) los tokens fueron enviados a una "
                "billetera adicional controlada por el usuario y no declarada arriba; (2) los tokens fueron enviados a una billetera de un "
                "tercero donde la contraprestación pudo haberse recibido vía cripto antes o después del envío, o por "
                "transferencia FIAT — en ese caso habría que analizarlo en el banco."
            )
        st.caption("\\* " + "  \n\\* ".join(partes))
else:
    st.info("Sin movimientos para este token" + (" con la wallet seleccionada." if selected_wallet_filter else "."))

# ── Gráfico combinado ─────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("📈 Evolución de la cartera Reental")

events = []
for contract, data in token_data.items():
    for m in data["movements"]:
        events.append({"fecha": m["fecha"], "contract": contract, "cantidad_neta": m["cantidad_neta"]})
events.sort(key=lambda e: e["fecha"])

running_balances = defaultdict(float)
history = []
for ev in events:
    running_balances[ev["contract"]] += ev["cantidad_neta"]
    proyectos_unicos = sum(1 for b in running_balances.values() if b > 0.000001)
    total_tokens = sum(b for b in running_balances.values() if b > 0.000001)
    history.append({"fecha": ev["fecha"], "proyectos_unicos": proyectos_unicos,
                    "total_tokens": round(total_tokens, 4)})

if history:
    df_ev = pd.DataFrame(history)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df_ev["fecha"], y=df_ev["proyectos_unicos"],
        name="Proyectos únicos",
        mode="lines+markers",
        line=dict(color="#1f77b4", width=2),
        marker=dict(size=4),
        yaxis="y1",
        hovertemplate="%{x|%d %b %Y}<br><b>%{y} proyectos únicos</b><extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=df_ev["fecha"], y=df_ev["total_tokens"],
        name="Total tokens acumulados",
        mode="lines+markers",
        line=dict(color="#2ca02c", width=2),
        marker=dict(size=4),
        yaxis="y2",
        hovertemplate="%{x|%d %b %Y}<br><b>%{y:,.0f} tokens totales</b><extra></extra>",
    ))
    fig.update_layout(
        xaxis=dict(
            tickformat="%b %Y",
            dtick="M1",
            tickangle=-45,
            showgrid=True,
            gridcolor="rgba(128,128,128,0.2)",
        ),
        yaxis=dict(
            title=dict(text="Nº proyectos únicos", font=dict(color="#1f77b4")),
            tickfont=dict(color="#1f77b4"),
            showgrid=True,
            gridcolor="rgba(128,128,128,0.1)",
        ),
        yaxis2=dict(
            title=dict(text="Total tokens acumulados", font=dict(color="#2ca02c")),
            tickfont=dict(color="#2ca02c"),
            overlaying="y",
            side="right",
            showgrid=False,
        ),
        legend=dict(x=0.01, y=0.99),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=60, r=60, t=30, b=80),
        height=420,
    )
    st.plotly_chart(fig, use_container_width=True)

# ── Movimientos de Stablecoins relacionados con Reental ──────────────────────
st.markdown("---")
st.subheader("💵 Compraventa de tokens con USDT/USDC (en la misma transacción)")
st.caption(
    "Solo operaciones donde el intercambio token↔stablecoin ocurrió **en la misma transacción on-chain**: "
    "compras, ventas y reinversiones desde el vault. **No** incluye compras/ventas pagadas en FIAT, dividendos, "
    "flujos de Aave, ni USDT enviado en una transacción aparte (esos se ven en sus secciones y en el informe fiscal)."
)

stable_movs = build_stablecoin_movements(token_data)   # SIN filtrar: se reutiliza en el CSV fiscal
stable_movs_filtered = filtrar_por_wallet(stable_movs)  # para la tabla en pantalla
if stable_movs_filtered:
    saldo = 0.0
    stable_rows = []
    for m in stable_movs_filtered:
        if m["direction"] == "entrada":
            saldo += m["amount"]
            amount_str = f"+{m['amount']:,.2f} {m['stable_symbol']}"
        else:
            saldo -= m["amount"]
            amount_str = f"-{m['amount']:,.2f} {m['stable_symbol']}"
        row = {
            "Fecha": m["fecha_str"],
            "Token inmobiliario": m["token"],
            "Operación": m["operacion"],
            "Importe": amount_str,
            "Saldo acumulado": f"{saldo:,.2f} {m['stable_symbol']}",
            "TX": m["tx_link"],
        }
        if es_multi_wallet:
            row["Wallet"] = m.get("wallet_alias", "—")
        stable_rows.append(row)
    st.dataframe(pd.DataFrame(stable_rows), column_config={
        "TX": st.column_config.LinkColumn("Ver TX", width="small"),
    }, hide_index=True, use_container_width=True)

    # Métricas resumen
    total_invertido = sum(m["amount"] for m in stable_movs_filtered if m["direction"] == "salida")
    total_recibido  = sum(m["amount"] for m in stable_movs_filtered if m["direction"] == "entrada")
    balance_neto = total_recibido - total_invertido
    balance_color = "#16a34a" if balance_neto >= 0 else "#dc2626"
    _balance_sublabel = "USDT / USDC"
    c1, c2, c3 = st.columns(3)
    c1.markdown(kpi_card("💸", "Total invertido",      f"{total_invertido:,.2f}", sublabel="USDT / USDC"), unsafe_allow_html=True)
    c2.markdown(kpi_card("💰", "Total recibido",       f"{total_recibido:,.2f}",  sublabel="de ventas · USDT / USDC"), unsafe_allow_html=True)
    c3.markdown(kpi_card("⚖️", "Balance neto",         f"{balance_neto:,.2f}",    value_color=balance_color, sublabel="USDT / USDC"), unsafe_allow_html=True)
    if balance_neto < 0:
        st.caption("⚠️ Faltaría por considerar las operaciones realizadas con FIAT, como por ejemplo la compra de tokens inmobiliarios.")
else:
    st.info("No se detectaron flujos de stablecoins relacionados con Reental" + (" con la wallet seleccionada." if selected_wallet_filter else " en esta wallet."))

raw_transfers_by_wallet = st.session_state.get("raw_transfers_by_wallet", {})
vault_by_wallet         = st.session_state.get("vault_by_wallet", {})

dividends = []
for _addr, _alias in wallets_analyzed:
    _vinfo = vault_by_wallet.get(_addr, {})
    _evs = process_dividends(
        raw_transfers_by_wallet.get(_addr, []), _addr,
        _vinfo.get("vault_transfers"), _vinfo.get("vault_addr"),
    )
    for _ev in _evs:
        _ev["wallet_addr"], _ev["wallet_alias"] = _addr, _alias
    dividends.extend(_evs)
dividends.sort(key=lambda e: e["fecha"])

vaults_detectados = {
    (alias or own_wallets.get(addr, addr[:8])): vault_by_wallet.get(addr, {}).get("vault_addr")
    for addr, alias in wallets_analyzed
    if vault_by_wallet.get(addr, {}).get("vault_addr")
}

# ── Dividendos recibidos de Reental ──────────────────────────────────────────
st.markdown("---")
st.subheader("💰 Dividendos recibidos de Reental")

if vaults_detectados:
    _detalle_vaults = "; ".join(
        (f"«{alias}» → `{v[:8]}…{v[-4:]}`" if es_multi_wallet else f"`{v[:8]}…{v[-4:]}`")
        for alias, v in vaults_detectados.items()
    )
    _vault_note = f"Incluye dividendos acumulados en vault personal detectado en: {_detalle_vaults}."
else:
    _vault_note = (
        "Solo se muestran dividendos recibidos directamente en wallet "
        "(no se detectó vault personal asociado)."
    )
st.caption(
    "Pagos de rendimientos distribuidos por Reental. "
    "Cada fila es una distribución (puede incluir varios proyectos en el mismo TX). " + _vault_note
)

dividends_filtered = filtrar_por_wallet(dividends)
if dividends_filtered:
    saldo_div = 0.0
    div_rows = []
    for ev in dividends_filtered:
        saldo_div += ev["total"]
        detalle = "  +  ".join(f"{p:,.2f}" for p in ev["pagos"])
        row = {
            "Fecha":          ev["fecha_str"],
            "Destino":        ev.get("destino", "Wallet directa"),
            "Nº proyectos":   ev["n_proyectos"],
            "Detalle pagos":  detalle,
            "Total recibido": f"{ev['total']:,.2f} {ev['sym']}",
            "Acumulado":      f"{saldo_div:,.2f} {ev['sym']}",
            "TX": ev["tx_link"],
        }
        if es_multi_wallet:
            row["Wallet"] = ev.get("wallet_alias", "—")
        div_rows.append(row)

    st.dataframe(pd.DataFrame(div_rows), column_config={
        "TX": st.column_config.LinkColumn("Ver TX", width="small"),
        "Nº proyectos": st.column_config.NumberColumn(width="small"),
    }, hide_index=True, use_container_width=True)

    total_div      = sum(ev["total"] for ev in dividends_filtered)
    total_wallet   = sum(ev["total"] for ev in dividends_filtered if ev.get("destino") == "Wallet directa")
    total_vault    = sum(ev["total"] for ev in dividends_filtered if ev.get("destino") == "Vault personal")
    c1, c2, c3 = st.columns(3)
    c1.markdown(kpi_card("💵", "Total dividendos",       f"{total_div:,.2f}",
                          value_color="#16a34a", sublabel="USDT / USDC (wallet + vault)"), unsafe_allow_html=True)
    c2.markdown(kpi_card("📬", "Distribuciones",          str(len(dividends_filtered)),
                          sublabel="pagos recibidos"), unsafe_allow_html=True)
    c3.markdown(kpi_card("📊", "Media por distribución",  f"{total_div / len(dividends_filtered):,.2f}",
                          sublabel="USDT / USDC"), unsafe_allow_html=True)
    if vaults_detectados and total_vault > 0:
        st.caption(
            f"Desglose: **{total_wallet:,.2f} USDT/USDC** recibidos en wallet directa · "
            f"**{total_vault:,.2f} USDT/USDC** acumulados en vault personal"
        )
else:
    st.info("No se detectaron dividendos recibidos" + (" con la wallet seleccionada." if selected_wallet_filter else " en esta wallet ni en su vault personal."))

# ── Actividad en Aave ────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("🏦 Actividad en Aave (USDT/USDC)")
st.caption("Actividad en el protocolo Aave: préstamos otorgados (prestamista) y créditos tomados usando tokens inmobiliarios como garantía (prestatario).")

aave_lender, aave_borrower = [], []
for _addr, _alias in wallets_analyzed:
    _act = process_aave_activity(raw_transfers_by_wallet.get(_addr, []), _addr)
    for _m in _act["lender"]:
        _m["wallet_addr"], _m["wallet_alias"] = _addr, _alias
    for _m in _act["borrower"]:
        _m["wallet_addr"], _m["wallet_alias"] = _addr, _alias
    aave_lender.extend(_act["lender"])
    aave_borrower.extend(_act["borrower"])
aave_lender.sort(key=lambda m: m["fecha"])
aave_borrower.sort(key=lambda m: m["fecha"])
aave_lending  = aave_lender   # alias para el exportador CSV

# ── Prestamista ───────────────────────────────────────────────────────────────
st.markdown("#### 🏦 Como prestamista")
aave_lender_filtered = filtrar_por_wallet(aave_lender)
if aave_lender_filtered:
    saldo_at = 0.0
    lender_rows = []
    for m in aave_lender_filtered:
        if m["tipo"] == "Depósito préstamo":
            saldo_at += m["cantidad_atoken"]
            importe_str = f"-{m['stable_amount']:,.2f} {m['stable_symbol']}" if m["stable_amount"] else "—"
        else:
            saldo_at -= m["cantidad_atoken"]
            importe_str = f"+{m['stable_amount']:,.2f} {m['stable_symbol']}" if m["stable_amount"] else "—"
        row = {
            "Fecha":        m["fecha_str"],
            "Operación":    m["tipo"],
            "aToken":       m["atoken"],
            "Cant. aToken": f"{m['cantidad_atoken']:,.4f}",
            "USDT/USDC":    importe_str,
            "Saldo aToken": f"{saldo_at:,.4f}",
            "TX":           m["tx_link"],
        }
        if m["interest_note"]:
            row["Intereses"] = m["interest_note"]
        if es_multi_wallet:
            row["Wallet"] = m.get("wallet_alias", "—")
        lender_rows.append(row)

    st.dataframe(pd.DataFrame(lender_rows), column_config={
        "TX": st.column_config.LinkColumn("Ver TX", width="small"),
    }, hide_index=True, use_container_width=True)

    total_dep   = sum(m["stable_amount"] for m in aave_lender_filtered if m["tipo"] == "Depósito préstamo")
    total_ret   = sum(m["stable_amount"] for m in aave_lender_filtered if m["tipo"] == "Retirada préstamo")
    at_dep      = sum(m["cantidad_atoken"] for m in aave_lender_filtered if m["tipo"] == "Depósito préstamo")
    at_ret      = sum(m["cantidad_atoken"] for m in aave_lender_filtered if m["tipo"] == "Retirada préstamo")
    saldo_vivo  = max(0.0, at_dep - at_ret)
    pos_abierta = saldo_vivo > 0.01
    int_netos   = max(0.0, total_ret + saldo_vivo - total_dep)
    rent_pct    = (int_netos / total_dep * 100) if total_dep > 0 else 0.0

    flujos_irr = []
    for m in aave_lender_filtered:
        if m["tipo"] == "Depósito préstamo":
            flujos_irr.append((m["fecha"], -m["stable_amount"]))
        else:
            flujos_irr.append((m["fecha"], +m["stable_amount"]))
    if pos_abierta:
        flujos_irr.append((datetime.utcnow(), +saldo_vivo))
    tir = calculate_irr(flujos_irr)

    pos_badge = (
        '<span style="font-size:0.7rem;background:#fef9c3;color:#854d0e;'
        'border-radius:4px;padding:2px 6px;font-weight:600;">posición abierta</span>'
        if pos_abierta else ""
    )
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.markdown(kpi_card("💵", "Total depositado",   f"{total_dep:,.2f}",  sublabel="USDT / USDC"), unsafe_allow_html=True)
    c2.markdown(kpi_card("🏦", "Total retirado",     f"{total_ret:,.2f}",  sublabel="USDT / USDC"), unsafe_allow_html=True)
    c3.markdown(kpi_card("✨", "Intereses netos",    f"{int_netos:,.2f}",
                         value_color="#16a34a" if int_netos >= 0 else "#dc2626",
                         sublabel="USDT / USDC", badge=pos_badge), unsafe_allow_html=True)
    c4.markdown(kpi_card("📈", "Rentabilidad *",     f"{rent_pct:.2f} %",
                         value_color="#16a34a" if rent_pct >= 0 else "#dc2626",
                         sublabel="sobre capital total"), unsafe_allow_html=True)
    c5.markdown(kpi_card("⚡", "TIR anual *",
                         f"{tir*100:.2f} %" if tir is not None else "—",
                         value_color="#16a34a" if (tir or 0) >= 0 else "#dc2626",
                         sublabel="tasa interna de retorno"), unsafe_allow_html=True)

    with st.expander("📐 Notas metodológicas — Prestamista"):
        st.markdown(f"""
**\\* Rentabilidad total:** (Total retirado + Saldo abierto − Total depositado) / Total depositado
{"— Se incluye saldo abierto estimado a 1:1 con USDT como flujo hipotético a hoy." if pos_abierta else "— Posición cerrada; solo flujos reales."}

**\\* TIR anual:** tasa interna de retorno ponderando importes y fechas de cada flujo. Una TIR del 4 % equivale a un 4 % de interés anual compuesto.

> ⚠️ Los aTokens se valoran a paridad 1:1 con USDT/USDC; variaciones del índice Aave pueden producir ligeras imprecisiones.
        """)
else:
    st.info("No se detectó actividad como prestamista en Aave" + (" con la wallet seleccionada." if selected_wallet_filter else " en esta wallet."))

# ── Prestatario ───────────────────────────────────────────────────────────────
st.markdown("#### 🏛️ Como prestatario")
aave_borrower_filtered = filtrar_por_wallet(aave_borrower)
if aave_borrower_filtered:
    borrower_rows = []
    for m in aave_borrower_filtered:
        if m["tipo"] in ("Préstamo recibido", "Pago de deuda"):
            importe_str = (
                f"+{m['stable_amount']:,.2f} {m['stable_symbol']}" if m["tipo"] == "Préstamo recibido"
                else f"-{m['stable_amount']:,.2f} {m['stable_symbol']}"
            ) if m["stable_amount"] else "—"
        else:
            importe_str = "—"
        row = {
            "Fecha":      m["fecha_str"],
            "Operación":  m["tipo"],
            "Detalle":    m["detalle"],
            "Cantidad":   f"{m['cantidad']:,.4f}",
            "USDT/USDC":  importe_str,
            "TX":         m["tx_link"],
        }
        if es_multi_wallet:
            row["Wallet"] = m.get("wallet_alias", "—")
        borrower_rows.append(row)

    st.dataframe(pd.DataFrame(borrower_rows), column_config={
        "TX": st.column_config.LinkColumn("Ver TX", width="small"),
    }, hide_index=True, use_container_width=True)

    total_prestado  = sum(m["stable_amount"] for m in aave_borrower_filtered if m["tipo"] == "Préstamo recibido")
    total_devuelto  = sum(m["stable_amount"] for m in aave_borrower_filtered if m["tipo"] == "Pago de deuda")
    coste_neto      = max(0.0, total_devuelto - total_prestado)
    deuda_viva      = max(0.0, total_prestado - total_devuelto)
    n_garantias_dep = sum(1 for m in aave_borrower_filtered if m["tipo"] == "Garantía depositada")
    n_garantias_ret = sum(1 for m in aave_borrower_filtered if m["tipo"] == "Garantía retirada")
    garantia_activa = n_garantias_dep > n_garantias_ret

    g_badge = (
        '<span style="font-size:0.7rem;background:#fef9c3;color:#854d0e;'
        'border-radius:4px;padding:2px 6px;font-weight:600;">garantía activa</span>'
        if garantia_activa else ""
    )
    d_badge = (
        '<span style="font-size:0.7rem;background:#fee2e2;color:#991b1b;'
        'border-radius:4px;padding:2px 6px;font-weight:600;">deuda viva</span>'
        if deuda_viva > 0.01 else ""
    )

    b1, b2, b3, b4 = st.columns(4)
    b1.markdown(kpi_card("💸", "Total prestado",   f"{total_prestado:,.2f}", sublabel="USDT / USDC"), unsafe_allow_html=True)
    b2.markdown(kpi_card("↩️", "Total devuelto",   f"{total_devuelto:,.2f}", sublabel="USDT / USDC"), unsafe_allow_html=True)
    b3.markdown(kpi_card("💰", "Coste (intereses)", f"{coste_neto:,.2f}",
                         value_color="#dc2626" if coste_neto > 0 else "#94a3b8",
                         sublabel="USDT / USDC pagado de más"), unsafe_allow_html=True)
    b4.markdown(kpi_card("⚠️", "Deuda pendiente",  f"{deuda_viva:,.2f}",
                         value_color="#dc2626" if deuda_viva > 0.01 else "#16a34a",
                         sublabel="USDT / USDC", badge=d_badge), unsafe_allow_html=True)
else:
    st.info("No se detectó actividad como prestatario en Aave" + (" con la wallet seleccionada." if selected_wallet_filter else " en esta wallet."))

# ── Ecosistema RNT — Staking y Farming ───────────────────────────────────────
st.markdown("---")
st.subheader("🪙 Ecosistema RNT — Staking y Farming")
st.caption(
    "Actividad relacionada con el token RNT: staking, pool de liquidez RNT/USDT (SLP) y farming (frmRNT)."
)

rnt_events = []
for _addr, _alias in wallets_analyzed:
    _xrnt_nft_transfers = fetch_nft_transfers(_addr, STAKING_RECEIVER)
    _evs = process_rnt_ecosystem(raw_transfers_by_wallet.get(_addr, []), _addr, _xrnt_nft_transfers)
    for _ev in _evs:
        _ev["wallet_addr"], _ev["wallet_alias"] = _addr, _alias
    rnt_events.extend(_evs)
rnt_events.sort(key=lambda e: e["fecha"])

rnt_events_filtered = filtrar_por_wallet(rnt_events)
if rnt_events_filtered:
    # Calcular balances acumulados
    bal_rnt = bal_slp = bal_frmrnt = 0.0
    bal_xrnt_staked = 0.0    # RNT total en posición de staking (xRNT NFT)
    bal_xrnt_count  = 0      # número de NFTs xRNT actualmente en wallet
    last_farming_deposit_date = None
    # Acumulados para estimar valor de farming
    total_rnt_farming = 0.0   # RNT aportado al pool
    total_usdt_farming = 0.0  # USDT aportado al pool
    rnt_rows = []

    OPERATION_COLORS = {
        "Staking de RNT":                              "#e8f4fd",
        "Claim rewards de staking":                    "#d4edda",
        "Recepción de xRNT (posición de staking cedida)": "#d4edda",
        "Envío de xRNT (posición de staking)":         "#fff3cd",
        "Depositar LP en farming":             "#e8f4fd",
        "Retirar LP de farming":               "#fff3cd",
        "Claim rewards de farming":            "#d4edda",
        "Añadir liquidez al pool RNT/USDT":   "#e8f4fd",
        "Retirar liquidez del pool RNT/USDT":  "#fff3cd",
        "Recepción de RNT":                    "#d4edda",
        "Envío de RNT":                        "#ffd6d6",
    }

    for ev in rnt_events_filtered:
        bal_rnt    += ev["rnt_delta"]
        bal_slp    += ev["slp_delta"]
        bal_frmrnt += ev["frmrnt_delta"]
        if ev["frmrnt_delta"] > 0:
            last_farming_deposit_date = ev["fecha"]
        # Acumular aportaciones al pool para estimar valor farming
        if ev["tipo"] == "Añadir liquidez al pool RNT/USDT":
            total_rnt_farming  += abs(ev["rnt_delta"])
            total_usdt_farming += abs(ev["usdt_delta"])
        # xRNT: acumular RNT y contar NFTs en wallet
        bal_xrnt_staked += ev.get("xrnt_staked_delta", 0.0)
        bal_xrnt_count  += ev.get("xrnt_count_delta", 0)

        row = {
            "Fecha":       ev["fecha_str"],
            "Operación":   ev["tipo"],
            "Detalle":     ev["detalle"],
            "Saldo RNT":   round(bal_rnt, 4),
            "Saldo frmRNT": round(bal_frmrnt, 6),
            "TX": ev["tx_link"],
        }
        if es_multi_wallet:
            row["Wallet"] = ev.get("wallet_alias", "—")
        rnt_rows.append(row)

    def style_operacion(val):
        color = OPERATION_COLORS.get(val, "")
        return f"background-color: {color}" if color else ""

    df_rnt = pd.DataFrame(rnt_rows)
    styled_rnt = df_rnt.style.applymap(style_operacion, subset=["Operación"])
    st.dataframe(styled_rnt, column_config={
        "TX": st.column_config.LinkColumn("Ver TX", width="small"),
        "Saldo RNT":   st.column_config.NumberColumn(width="small"),
        "Saldo frmRNT": st.column_config.NumberColumn(width="small"),
    }, hide_index=True, use_container_width=True)

    # Notas a pie de tabla (asteriscos de eventos especiales)
    notas_pie = [ev["nota_asterisco"] for ev in rnt_events_filtered if ev.get("nota_asterisco")]
    for nota in dict.fromkeys(notas_pie):  # deduplicar preservando orden
        st.caption(nota)

    # Precio RNT y valor estimado de farming
    rnt_price = get_rnt_price_usdt()
    farming_value_usdt = total_usdt_farming + total_rnt_farming * rnt_price

    # Métricas resumen
    farming_label = (
        f"≈ {farming_value_usdt:,.2f}"
        if rnt_price > 0 else
        f"{total_usdt_farming:,.2f} USDT + {total_rnt_farming:,.4f} RNT"
    )
    farming_sublabel = (
        f"USDT · RNT: {total_rnt_farming:,.4f} · precio: {rnt_price:.4f} $"
        if rnt_price > 0 else "sin precio de mercado"
    )
    if bal_xrnt_count > 0:
        xrnt_label    = f"{bal_xrnt_count} NFT{'s' if bal_xrnt_count > 1 else ''}"
        xrnt_sublabel = (f"{bal_xrnt_staked:,.4f} RNT en staking"
                         if bal_xrnt_staked > 0 else "RNT en staking no determinado *")
        xrnt_color    = "#16a34a"
    else:
        xrnt_label    = "—"
        xrnt_sublabel = "sin posición activa"
        xrnt_color    = "#94a3b8"

    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(kpi_card("🪙", "Saldo RNT",              f"{bal_rnt:,.4f}",   sublabel="RNT en wallet"), unsafe_allow_html=True)
    c2.markdown(kpi_card("🌾", "Posición farming",        f"{bal_frmrnt:,.6f}", sublabel="frmRNT depositados"), unsafe_allow_html=True)
    c3.markdown(kpi_card("💎", "Valor en farming",        farming_label,       sublabel=farming_sublabel), unsafe_allow_html=True)
    c4.markdown(kpi_card("🔒", "Staking xRNT",           xrnt_label,          value_color=xrnt_color, sublabel=xrnt_sublabel), unsafe_allow_html=True)

    # Burning fee calculator
    if last_farming_deposit_date:
        st.markdown("#### 🔥 Burning fee de farming")
        days_elapsed = (datetime.utcnow() - last_farming_deposit_date).days
        fee = burning_fee_pct(days_elapsed)
        days_remaining = max(0, 180 - days_elapsed)
        days_to_min = days_remaining

        col_a, col_b, col_c = st.columns(3)
        col_a.metric(
            "Último depósito de farming",
            last_farming_deposit_date.strftime("%d %b %Y"),
        )
        col_b.metric("Días transcurridos", f"{days_elapsed} días")
        col_c.metric(
            "Fee de quema actual",
            f"{fee:.1f}%",
            delta=f"−{days_to_min} días para llegar al 2%" if days_to_min > 0 else "Mínimo alcanzado (2%)",
            delta_color="inverse" if days_to_min > 0 else "off",
        )
        if days_to_min > 0:
            st.info(
                f"⚠️ Si retiras tu posición de farming hoy, se aplicará un burning fee del **{fee:.1f}%** "
                f"sobre los tokens retirados. El fee alcanzará el mínimo del 2 % en **{days_to_min} días** "
                f"({(last_farming_deposit_date + timedelta(days=180)).strftime('%d %b %Y')})."
            )
        else:
            st.success("✅ Han transcurrido más de 180 días desde el último depósito. El burning fee está en el mínimo: **2%**.")
else:
    st.info(
        "No se detectó actividad en el ecosistema RNT (staking, farming o pool de liquidez)"
        + (" con la wallet seleccionada." if selected_wallet_filter else " en esta wallet.")
    )

# ── Informe fiscal (resumen agregado + exportables) ──────────────────────────
st.markdown("---")
st.subheader("📄 Informe fiscal")
st.caption(
    "Dos documentos complementarios para tu asesor fiscal: un **resumen agregado** con los "
    "totales por naturaleza fiscal (para rellenar las casillas de los modelos) y un **CSV "
    "cronológico y granular** como respaldo. Todos los importes se valoran en USD y en EUR "
    "al tipo de cambio de la fecha de cada operación."
)

@st.cache_data(show_spinner=False, ttl=21600)
def _rnt_price_history_usd() -> dict:
    """Precio diario de RNT en USD para los últimos 365 días, obtenido en UNA
    sola llamada a CoinGecko (`market_chart/range`) en vez de una petición por
    fecha: consultar fecha a fecha (`coins/{id}/history`) agota el límite de
    peticiones del plan gratuito a partir de la 6ª-7ª llamada seguida, lo que
    hacía fallar incluso fechas recientes por rate-limit, no solo las que
    exceden la ventana de 365 días. Devuelve {"YYYY-MM-DD": precio}; {} si la
    consulta falla (todas las fechas quedarán entonces sin precio, nunca en 0)."""
    try:
        hoy = datetime.utcnow()
        desde = hoy - timedelta(days=364)
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/reental/market_chart/range",
            params={"vs_currency": "usd", "from": int(desde.timestamp()), "to": int(hoy.timestamp())},
            timeout=20,
        )
        r.raise_for_status()
        return {
            datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d"): float(p)
            for ts, p in r.json().get("prices", [])
        }
    except Exception:
        return {}


def get_rnt_price_on_date(date_str: str):
    """Precio de RNT en USD para una fecha dada (YYYY-MM-DD). Devuelve None si
    la fecha excede la ventana de 365 días del plan gratuito de CoinGecko, o si
    el histórico no se pudo obtener — nunca se aproxima a 0 en ese caso."""
    return _rnt_price_history_usd().get(date_str[:10])

def _income_items() -> tuple:
    """Contribuciones de rendimiento con valor determinado, como
    (fecha_str, concepto, valor_usd), y por separado las recompensas de RNT
    cuyo precio a la fecha del claim no se pudo obtener.

    Solo rentas reales: dividendos, intereses de Aave como prestamista y
    recompensas de staking/farming (eventos de *claim*, nunca las "Recepción de
    RNT" cuyo origen es ambiguo).

    Cuando CoinGecko no devuelve precio para la fecha del claim (fuera de la
    ventana de 365 días del plan gratuito, o límite de peticiones agotado),
    la recompensa en RNT NUNCA se valora como $0: se reporta aparte en
    `pendientes_rnt` para que se complete con el precio de mercado real."""
    items, pendientes_rnt = [], []
    for ev in (dividends or []):
        items.append((ev["fecha_str"], "Dividendos inmobiliarios", ev.get("total", 0.0)))

    for m in (aave_lender or []):
        if m["tipo"] == "Retirada préstamo":
            interes = (m.get("stable_amount") or 0.0) - (m.get("cantidad_atoken") or 0.0)
            if interes > 0.001:
                items.append((m["fecha_str"], "Intereses Aave (prestamista)", interes))

    claim_events = [ev for ev in (rnt_events or [])
                    if ev["tipo"] in ("Claim rewards de staking", "Claim rewards de farming")]
    fechas = {ev["fecha_str"][:10] for ev in claim_events}
    precios = {d: get_rnt_price_on_date(d) for d in fechas}
    for ev in claim_events:
        precio = precios.get(ev["fecha_str"][:10])
        rnt  = ev.get("rnt_delta", 0.0) or 0.0
        usdt = ev.get("usdt_delta", 0.0) or 0.0
        concepto = "Staking (recompensas)" if "staking" in ev["tipo"] else "Farming (recompensas)"
        if precio:
            items.append((ev["fecha_str"], concepto, rnt * precio + usdt))
            continue
        if usdt:
            items.append((ev["fecha_str"], concepto, usdt))
        if rnt > 0:
            pendientes_rnt.append({
                "Fecha UTC": ev["fecha_str"], "Concepto": concepto,
                "Cantidad RNT": round(rnt, 6),
                "Motivo": ("Precio de RNT no disponible en CoinGecko para esta fecha: fuera de la "
                           "ventana de 365 días del plan gratuito, o límite de peticiones agotado. "
                           "Completar con el precio de mercado real de RNT en la fecha del cobro."),
            })
    return items, pendientes_rnt


def build_capital_gains_fifo() -> dict:
    """Empareja compras y ventas de tokens inmobiliarios por FIFO y calcula la
    ganancia/pérdida de cada operación. Excluye transferencias internas y los
    movimientos de colateral de Aave (no son disposiciones).

    El coste de adquisición es exacto cuando hay USDT en la misma TX; si no, se
    estima con el precio de emisión (marcado como provisional). Cuando no se
    conoce el valor de transmisión (venta a FIAT), la ganancia queda PENDIENTE."""
    from collections import deque
    gains, lots_abiertos = [], []

    for contract, d in token_data.items():
        info   = d["info"]
        label  = info["label"]
        divisa = info.get("divisa", "USD")
        pe     = info.get("precio_emision") or 0.0
        lots, lot_counter = deque(), 0

        for m in d["movements"]:
            if use_date_filter and m["fecha"].date() > cutoff:
                continue
            if m.get("es_transferencia_interna"):
                continue
            if m["tipo"].startswith(("Colateralización", "Descolateralización")):
                continue

            cant = m["cantidad_neta"]
            if cant > 0:
                qty  = cant
                stout = m.get("stable_out", 0.0) or 0.0
                if stout > 0:
                    unit, fuente, pend = stout / qty, "TX exacta", False
                elif pe > 0:
                    rate = get_eurusd_on_date(m["fecha_str"][:10]) if divisa == "EUR" else None
                    unit = pe * rate if (divisa == "EUR" and rate) else pe
                    fuente, pend = "Estimación (precio emisión)", True
                else:
                    unit, fuente, pend = 0.0, "N/D", True
                lot_counter += 1
                lots.append({"id": f"{label}-L{lot_counter}", "qty": qty, "unit": unit,
                             "fecha": m["fecha_str"], "fuente": fuente, "pend": pend})

            elif cant < 0:
                qty_sell = -cant
                stin = m.get("stable_in", 0.0) or 0.0
                proceeds_known = stin > 0
                unit_proceeds = (stin / qty_sell) if proceeds_known else None
                fecha_venta = m["fecha_str"]

                while qty_sell > 1e-9 and lots:
                    lot  = lots[0]
                    take = min(lot["qty"], qty_sell)
                    coste_usd = take * lot["unit"]
                    _, coste_eur, _ = fiat_values(coste_usd, lot["fecha"])
                    coste_eur = coste_eur if coste_eur != "" else None
                    if proceeds_known:
                        trans_usd = take * unit_proceeds
                        _, trans_eur, _ = fiat_values(trans_usd, fecha_venta)
                        trans_eur = trans_eur if trans_eur != "" else None
                    else:
                        trans_usd, trans_eur = None, None

                    if not proceeds_known:
                        estado, gan_usd, gan_eur = "PENDIENTE — falta valor de transmisión", None, None
                    else:
                        estado = "Provisional — coste estimado (revisar)" if lot["pend"] else "Calculada"
                        gan_usd = round(trans_usd - coste_usd, 2)
                        gan_eur = (round(trans_eur - coste_eur, 2)
                                   if (trans_eur is not None and coste_eur is not None) else None)

                    gains.append({
                        "Token": label, "id_lote": lot["id"],
                        "Fecha compra": lot["fecha"], "Fecha venta": fecha_venta,
                        "Cantidad": round(take, 6),
                        "Coste adq. USD": round(coste_usd, 2), "Coste adq. EUR": coste_eur,
                        "Transmisión USD": round(trans_usd, 2) if proceeds_known else None,
                        "Transmisión EUR": trans_eur,
                        "Ganancia/pérdida USD": gan_usd, "Ganancia/pérdida EUR": gan_eur,
                        "Fuente coste": lot["fuente"], "Estado": estado,
                    })
                    lot["qty"] -= take
                    qty_sell  -= take
                    if lot["qty"] <= 1e-9:
                        lots.popleft()

                if qty_sell > 1e-9:
                    # Vendió más de lo que consta adquirido: sin lote de coste.
                    trans_usd = stin * qty_sell / (-cant) if proceeds_known else None
                    if proceeds_known:
                        _, trans_eur, _ = fiat_values(trans_usd, fecha_venta)
                        trans_eur = trans_eur if trans_eur != "" else None
                    else:
                        trans_eur = None
                    gains.append({
                        "Token": label, "id_lote": "(sin lote)",
                        "Fecha compra": None, "Fecha venta": fecha_venta,
                        "Cantidad": round(qty_sell, 6),
                        "Coste adq. USD": None, "Coste adq. EUR": None,
                        "Transmisión USD": round(trans_usd, 2) if proceeds_known else None,
                        "Transmisión EUR": trans_eur,
                        "Ganancia/pérdida USD": None, "Ganancia/pérdida EUR": None,
                        "Fuente coste": "N/D", "Estado": "PENDIENTE — sin coste de adquisición registrado",
                    })
                    qty_sell = 0.0

        for lot in lots:
            if lot["qty"] > 1e-9:
                coste_usd = lot["qty"] * lot["unit"]
                _, coste_eur, _ = fiat_values(coste_usd, lot["fecha"])
                lots_abiertos.append({
                    "Token": label, "id_lote": lot["id"], "Fecha compra": lot["fecha"],
                    "Cantidad": round(lot["qty"], 6),
                    "Coste USD": round(coste_usd, 2),
                    "Coste EUR": coste_eur if coste_eur != "" else None,
                    "Fuente coste": lot["fuente"],
                })

    calc_usd = sum(g["Ganancia/pérdida USD"] for g in gains
                   if g["Estado"] == "Calculada" and g["Ganancia/pérdida USD"] is not None)
    calc_eur = sum(g["Ganancia/pérdida EUR"] for g in gains
                   if g["Estado"] == "Calculada" and g["Ganancia/pérdida EUR"] is not None)
    prov_n = sum(1 for g in gains if g["Estado"].startswith("Provisional"))
    pend_n = sum(1 for g in gains if g["Estado"].startswith("PENDIENTE"))
    gains.sort(key=lambda g: g["Fecha venta"])

    return {
        "gains": gains, "lots_abiertos": sorted(lots_abiertos, key=lambda r: r["Token"]),
        "kpi": {"calc_usd": round(calc_usd, 2), "calc_eur": round(calc_eur, 2),
                "prov_n": prov_n, "pend_n": pend_n},
    }


def build_aggregate_report() -> dict:
    """Totales agregados por naturaleza fiscal, en USD y EUR (convertidos a la
    fecha de cada operación), más la foto de saldos y deuda a la fecha de corte."""
    # ── Rendimientos por (año, concepto) ──────────────────────────────────────
    acc = defaultdict(lambda: {"usd": 0.0, "eur": 0.0})
    tot = defaultdict(lambda: {"usd": 0.0, "eur": 0.0})
    income_items, pendientes_rnt = _income_items()
    for fecha_str, concepto, usd in income_items:
        if not usd:
            continue
        año = fecha_str[:4]
        _, eur, _ = fiat_values(usd, fecha_str)
        for bucket in (acc[(año, concepto)], tot[concepto]):
            bucket["usd"] += usd
            if eur != "":
                bucket["eur"] += eur

    rend_rows = [
        {"Año": año, "Concepto": concepto,
         "Valor USD": round(v["usd"], 2), "Valor EUR": round(v["eur"], 2)}
        for (año, concepto), v in sorted(acc.items())
    ]

    # ── Saldos a fecha de corte (tokens en cartera) ───────────────────────────
    snap_rate = get_eurusd_on_date(cutoff.strftime("%Y-%m-%d"))
    holdings_rows = []
    hold_usd, hold_eur = 0.0, 0.0
    for c, d in activos.items():
        info   = d["info"]
        saldo  = d["balance_display"]
        pe     = info.get("precio_emision") or 0.0
        divisa = info.get("divisa", "USD")
        val_native = saldo * pe
        if divisa == "EUR":
            v_eur = val_native
            v_usd = val_native * snap_rate if snap_rate else ""
        else:
            v_usd = val_native
            v_eur = val_native / snap_rate if snap_rate else ""
        holdings_rows.append({
            "Token": info["label"], "Nombre": info["name"],
            "Saldo": round(saldo, 6), "Divisa emisión": divisa,
            "Precio emisión": pe,
            "Valor USD": round(v_usd, 2) if v_usd != "" else None,
            "Valor EUR": round(v_eur, 2) if v_eur != "" else None,
        })
        hold_usd += v_usd if v_usd != "" else 0.0
        hold_eur += v_eur if v_eur != "" else 0.0

    # ── Deuda viva en Aave a la fecha de corte ────────────────────────────────
    prestado = devuelto = 0.0
    for m in (aave_borrower or []):
        if use_date_filter and m["fecha"].date() > cutoff:
            continue
        if m["tipo"] == "Préstamo recibido":
            prestado += m.get("stable_amount") or 0.0
        elif m["tipo"] == "Pago de deuda":
            devuelto += m.get("stable_amount") or 0.0
    deuda_usd = max(0.0, prestado - devuelto)
    deuda_eur = deuda_usd / snap_rate if snap_rate else ""

    return {
        "rend_rows": rend_rows,
        "rend_tot": dict(tot),
        "rend_pendiente_rnt": sorted(pendientes_rnt, key=lambda r: r["Fecha UTC"]),
        "holdings_rows": sorted(holdings_rows, key=lambda r: r["Token"]),
        "holdings_tot": {"usd": round(hold_usd, 2), "eur": round(hold_eur, 2)},
        "deuda_usd": round(deuda_usd, 2),
        "deuda_eur": round(deuda_eur, 2) if deuda_eur != "" else "",
        "snap_rate": snap_rate,
        "capital_gains": build_capital_gains_fifo(),
    }


TOOL_VERSION = "1.0"

# Mapa de referencia: cómo interpretar fiscalmente cada tipo de operación.
# Es agnóstico a la jurisdicción: describe la NATURALEZA del hecho, no el tipo
# impositivo concreto (que depende del país del inversor).
FISCAL_GLOSARIO = [
    {"Operación": "Compra de tokens (a Reental o a terceros)",
     "Naturaleza fiscal": "Adquisición patrimonial",
     "Tratamiento / nota": "Fija el coste de adquisición del lote; base de la futura ganancia/pérdida. La compra en sí no es hecho imponible."},
    {"Operación": "Venta de tokens",
     "Naturaleza fiscal": "Disposición patrimonial (ganancia/pérdida)",
     "Tratamiento / nota": "Genera ganancia o pérdida = valor de transmisión − coste de adquisición (emparejado por FIFO)."},
    {"Operación": "Reinversión desde vault",
     "Naturaleza fiscal": "Doble: rendimiento + adquisición",
     "Tratamiento / nota": "El dividendo es renta al recibirse; su reinversión crea un nuevo lote cuyo coste es el importe reinvertido."},
    {"Operación": "Entrada / Salida de Tokens * (sin contrapartida USDT en la TX)",
     "Naturaleza fiscal": "Adquisición o disposición de origen no determinado",
     "Tratamiento / nota": "Revisar: posible compra/venta en FIAT o USDT en otra TX. Coste o valor de transmisión a completar por el inversor (ver hoja «Por completar»)."},
    {"Operación": "Transferencia interna (entre wallets propias)",
     "Naturaleza fiscal": "No sujeta",
     "Tratamiento / nota": "Movimiento entre wallets del mismo titular; no altera el patrimonio ni genera renta."},
    {"Operación": "Dividendo recibido de Reental",
     "Naturaleza fiscal": "Rendimiento del capital",
     "Tratamiento / nota": "Renta del ejercicio en que se cobra, al valor en fiat de ese día."},
    {"Operación": "Colateralización / Descolateralización en Aave",
     "Naturaleza fiscal": "No sujeta (no es disposición)",
     "Tratamiento / nota": "Depositar/retirar tokens como garantía no cambia la titularidad económica; NO es una venta."},
    {"Operación": "Préstamo recibido / Pago de deuda (Aave prestatario)",
     "Naturaleza fiscal": "Financiación (no sujeta)",
     "Tratamiento / nota": "El principal recibido/devuelto no es renta ni gasto. Solo los intereses pagados pueden ser deducibles según jurisdicción."},
    {"Operación": "Depósito / Retirada de préstamo (Aave prestamista)",
     "Naturaleza fiscal": "Rendimiento del capital (intereses)",
     "Tratamiento / nota": "Los intereses cobrados (retirada − depósito) son renta; el principal no."},
    {"Operación": "Claim rewards de staking / farming (RNT)",
     "Naturaleza fiscal": "Rendimiento (recompensa)",
     "Tratamiento / nota": "Renta al valor de mercado del RNT en la fecha de cobro; ese valor es el coste de adquisición del RNT para futuras plusvalías."},
    {"Operación": "Recepción / Envío de RNT (no clasificado como recompensa)",
     "Naturaleza fiscal": "Origen a determinar",
     "Tratamiento / nota": "No se computa como renta automáticamente (puede ser compra, traspaso o airdrop). Revisar manualmente."},
]


def build_por_completar_rows() -> list:
    """Adquisiciones y disposiciones cuyo importe no se puede determinar on-chain
    (p.ej. compra/venta en FIAT o USDT en otra TX). El inversor/fiscalista rellena
    las columnas vacías con el dato real que tenga (extracto bancario, etc.)."""
    rows = []
    for contract, d in token_data.items():
        info = d["info"]
        pe = info.get("precio_emision") or 0.0
        for m in d["movements"]:
            if m.get("es_transferencia_interna"):
                continue
            tipo = m["tipo"]
            if tipo.startswith(("Colateralización", "Descolateralización")):
                continue
            cant = m["cantidad_neta"]
            stin  = m.get("stable_in", 0.0) or 0.0
            stout = m.get("stable_out", 0.0) or 0.0
            base = {
                "Fecha UTC": m["fecha_str"], "Token": info["label"],
                "Cantidad": round(cant, 6),
                "Importe FIAT real (a completar)": "", "Divisa": "", "Notas del inversor": "",
                "Wallet/Alias": m.get("wallet_alias", ""),
            }
            if cant > 0 and stout == 0:
                rows.append({**base, "Concepto": "Adquisición — coste a completar",
                             "Coste estimado USD (precio emisión)": round(cant * pe, 2) if pe else ""})
            elif cant < 0 and stin == 0:
                rows.append({**base, "Concepto": "Disposición — valor de transmisión a completar",
                             "Coste estimado USD (precio emisión)": ""})
    rows.sort(key=lambda r: r["Fecha UTC"])
    return rows


def build_report_meta() -> list:
    """Cabecera del informe: metadatos, alcance y disclaimer."""
    wallets_txt = "; ".join(f"{alias} ({addr[:8]}…{addr[-4:]})" for addr, alias in wallets_analyzed)
    ejercicio = (f"Saldos y foto de patrimonio a fecha {cutoff.strftime('%Y-%m-%d')}"
                 if use_date_filter else "Histórico completo (sin filtro de fecha)")
    return [
        {"Campo": "Informe", "Valor": "Reental — Informe fiscal agregado"},
        {"Campo": "Versión de la herramienta", "Valor": TOOL_VERSION},
        {"Campo": "Generado (UTC)", "Valor": datetime.utcnow().strftime("%Y-%m-%d %H:%M")},
        {"Campo": "Wallets analizadas", "Valor": wallets_txt},
        {"Campo": "Alcance temporal", "Valor": ejercicio},
        {"Campo": "Divisas", "Valor": "USD y EUR (tipo de cambio a la fecha de cada operación; USDT asumido a la par del USD)"},
        {"Campo": "Zona horaria", "Valor": "Todas las fechas en UTC"},
        {"Campo": "Método de plusvalías", "Valor": "FIFO (primero en entrar, primero en salir) — ver hoja «Plusvalías»"},
        {"Campo": "Aviso", "Valor": ("Este informe NO es asesoramiento fiscal; las cifras deben validarse por un "
                                     "profesional. Los importes marcados como estimados o pendientes (p.ej. compras "
                                     "en FIAT) deben completarse con los datos del inversor antes de presentar impuestos.")},
    ]


def build_aggregate_xlsx(agg: dict) -> bytes:
    """Documento agregado en XLSX multi-hoja (Informe · Resumen · Rendimientos ·
    RNT sin valorar · Saldos · Plusvalías · Lotes abiertos · Por completar ·
    Glosario) para que el asesor fiscal trabaje con los totales sin hashes ni
    contratos."""
    buf = io.BytesIO()
    rend_tot_usd = sum(v["usd"] for v in agg["rend_tot"].values())
    rend_tot_eur = sum(v["eur"] for v in agg["rend_tot"].values())

    resumen = [
        {"Bloque": f"Rendimientos — {k}", "Valor USD": round(v["usd"], 2), "Valor EUR": round(v["eur"], 2)}
        for k, v in agg["rend_tot"].items()
    ]
    resumen += [
        {"Bloque": "Rendimientos — TOTAL", "Valor USD": round(rend_tot_usd, 2), "Valor EUR": round(rend_tot_eur, 2)},
        {"Bloque": "Patrimonio — Valor tokens en cartera",
         "Valor USD": agg["holdings_tot"]["usd"], "Valor EUR": agg["holdings_tot"]["eur"]},
        {"Bloque": "Patrimonio — Deuda viva en Aave",
         "Valor USD": agg["deuda_usd"], "Valor EUR": agg["deuda_eur"]},
    ]

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(build_report_meta()).to_excel(writer, sheet_name="Informe", index=False)
        pd.DataFrame(resumen).to_excel(writer, sheet_name="Resumen", index=False)
        rend = agg["rend_rows"] or [{"Año": "", "Concepto": "(sin rendimientos en el periodo)",
                                     "Valor USD": "", "Valor EUR": ""}]
        pd.DataFrame(rend).to_excel(writer, sheet_name="Rendimientos", index=False)
        pend_rnt = agg["rend_pendiente_rnt"] or [{"Concepto": "(ningún claim de RNT sin valorar)"}]
        pd.DataFrame(pend_rnt).to_excel(writer, sheet_name="RNT sin valorar", index=False)
        hold = agg["holdings_rows"] or [{"Token": "(cartera vacía a la fecha de corte)"}]
        pd.DataFrame(hold).to_excel(writer, sheet_name="Saldos", index=False)

        cg = agg["capital_gains"]
        gains = cg["gains"] or [{"Estado": "(sin ventas en el periodo)"}]
        pd.DataFrame(gains).to_excel(writer, sheet_name="Plusvalías", index=False)
        lots = cg["lots_abiertos"] or [{"Token": "(sin lotes abiertos)"}]
        pd.DataFrame(lots).to_excel(writer, sheet_name="Lotes abiertos", index=False)

        pc = build_por_completar_rows() or [{"Concepto": "(nada pendiente: todos los costes se determinaron on-chain)"}]
        pd.DataFrame(pc).to_excel(writer, sheet_name="Por completar", index=False)
        pd.DataFrame(FISCAL_GLOSARIO).to_excel(writer, sheet_name="Glosario", index=False)
    return buf.getvalue()


def build_fiscal_csv() -> bytes:
    rows = []

    # ── 1. Tokens inmobiliarios ───────────────────────────────────────────────
    for contract, d in token_data.items():
        info     = d["info"]
        simbolo  = info["label"]
        nombre   = info["name"]
        p_emision = info.get("precio_emision") or 0.0

        for m in d["movements"]:
            cantidad = m["cantidad_neta"]     # positivo = entrada, negativo = salida
            stable_in  = m.get("stable_in",  0.0) or 0.0
            stable_out = m.get("stable_out", 0.0) or 0.0
            es_entrada = cantidad > 0
            es_interna = m.get("es_transferencia_interna", False)

            # Precio unitario
            if es_entrada and stable_out > 0 and abs(cantidad) > 0:
                precio_unit = stable_out / abs(cantidad)
                fuente_precio = "TX exacta"
                nota = ""
            elif not es_entrada and stable_in > 0 and abs(cantidad) > 0:
                precio_unit = stable_in / abs(cantidad)
                fuente_precio = "TX exacta"
                nota = ""
            elif p_emision > 0:
                precio_unit = p_emision
                fuente_precio = "Precio emisión (revisar)"
                nota = (
                    "Precio exacto no determinable. Posibles escenarios: "
                    "(1) compra/venta con FIAT — consultar extracto bancario; "
                    "(2) USDT enviados/recibidos en TX diferente; "
                    "(3) transferencia entre wallets propias."
                )
            else:
                precio_unit = None
                fuente_precio = "N/D"
                nota = "Precio no disponible."

            valor_usd = round(abs(cantidad) * precio_unit, 2) if precio_unit else None
            categoria = "Transferencia interna" if es_interna else "Token inmobiliario"
            if es_interna:
                fuente_precio = "N/A — transferencia interna"
                valor_usd = None
                nota = (
                    "No es una disposición fiscal: movimiento entre wallets propias del mismo titular "
                    f"({m['origen']} → {m['destino']})."
                )

            rows.append({
                "Fecha UTC":        m["fecha_str"],
                "Año fiscal":       m["fecha_str"][:4],
                "Categoría":        categoria,
                "Tipo operación":   m["tipo"],
                "Activo":           f"{nombre} ({simbolo})",
                "Cantidad":         round(cantidad, 6),
                "Precio unit. USD": round(precio_unit, 4) if (precio_unit and not es_interna) else "",
                "Valor USD":        valor_usd if valor_usd else "",
                "Fuente precio":    fuente_precio,
                "Contraparte":      m["destino"] if es_entrada else m["origen"],
                "TX Hash":          m["tx_hash"],
                "Notas":            nota,
                "Wallet/Alias":     m.get("wallet_alias", ""),
            })

    # ── 2. Dividendos ─────────────────────────────────────────────────────────
    for ev in (dividends if dividends else []):
        destino = ev.get("destino", "Wallet directa")
        nota_div = f"{ev['n_proyectos']} proyecto(s) en este pago."
        if destino == "Vault personal":
            nota_div += " Acumulado en vault personal (no retirado a wallet)."
        rows.append({
            "Fecha UTC":        ev["fecha_str"],
            "Año fiscal":       ev["fecha_str"][:4],
            "Categoría":        "Dividendo",
            "Tipo operación":   f"Dividendo recibido ({destino})",
            "Activo":           ev.get("sym", "USDT"),
            "Cantidad":         round(ev["total"], 4),
            "Precio unit. USD": 1.0,
            "Valor USD":        round(ev["total"], 2),
            "Fuente precio":    "TX exacta",
            "Contraparte":      "Reental (distribuidor)",
            "TX Hash":          ev.get("tx_link", "").replace("https://polygonscan.com/tx/", ""),
            "Notas":            nota_div,
            "Wallet/Alias":     ev.get("wallet_alias", ""),
        })

    # ── 3. Stablecoins ────────────────────────────────────────────────────────
    for m in (stable_movs if stable_movs else []):
        signo  =  1.0 if m["direction"] == "entrada" else -1.0
        rows.append({
            "Fecha UTC":        m["fecha_str"],
            "Año fiscal":       m["fecha_str"][:4],
            "Categoría":        "Stablecoin",
            "Tipo operación":   m["operacion"],
            "Activo":           m.get("stable_symbol", "USDT"),
            "Cantidad":         round(signo * m["amount"], 4),
            "Precio unit. USD": 1.0,
            "Valor USD":        round(m["amount"], 2),
            "Fuente precio":    "TX exacta",
            "Contraparte":      "",
            "TX Hash":          m.get("tx_link", "").replace("https://polygonscan.com/tx/", ""),
            "Notas":            f"Relacionado con: {m['token']}",
            "Wallet/Alias":     m.get("wallet_alias", ""),
        })

    # ── 4. Ecosistema RNT ─────────────────────────────────────────────────────
    # Recoger precios únicos por fecha (1 llamada API por día)
    rnt_ev_list = rnt_events if rnt_events else []
    fechas_unicas = {ev["fecha_str"][:10] for ev in rnt_ev_list}
    precios_rnt = {d: get_rnt_price_on_date(d) for d in fechas_unicas}

    for ev in rnt_ev_list:
        fecha_d = ev["fecha_str"][:10]
        precio_rnt = precios_rnt.get(fecha_d)

        # Determinar el activo principal y cantidad neta del evento
        rnt_d = ev.get("rnt_delta", 0.0)
        slp_d = ev.get("slp_delta", 0.0)
        frm_d = ev.get("frmrnt_delta", 0.0)

        if rnt_d != 0:
            activo   = "RNT"
            cantidad = round(rnt_d, 6)
            p_unit   = precio_rnt
            fuente   = "CoinGecko histórico" if precio_rnt else "N/D"
            valor    = round(abs(cantidad) * precio_rnt, 2) if precio_rnt else ""
            nota_rnt = "" if precio_rnt else "Precio RNT no disponible en CoinGecko para esta fecha."
        elif slp_d != 0:
            activo   = "SLP (RNT/USDT)"
            cantidad = round(slp_d, 8)
            p_unit   = ""
            fuente   = "N/D"
            valor    = ""
            nota_rnt = "Precio de LP no disponible automáticamente."
        elif frm_d != 0:
            activo   = "frmRNT"
            cantidad = round(frm_d, 8)
            p_unit   = ""
            fuente   = "N/D"
            valor    = ""
            nota_rnt = "Token de posición de farming."
        else:
            activo   = "xRNT NFT"
            cantidad = ev.get("xrnt_staked_delta", 0.0)
            p_unit   = ""
            fuente   = "N/D"
            valor    = ""
            nota_rnt = ev.get("nota_asterisco", "")

        nota_completa = ev.get("nota_asterisco") or nota_rnt

        rows.append({
            "Fecha UTC":        ev["fecha_str"],
            "Año fiscal":       ev["fecha_str"][:4],
            "Categoría":        "RNT / Ecosistema",
            "Tipo operación":   ev["tipo"],
            "Activo":           activo,
            "Cantidad":         cantidad,
            "Precio unit. USD": p_unit,
            "Valor USD":        valor,
            "Fuente precio":    fuente,
            "Contraparte":      "",
            "TX Hash":          ev.get("tx_link", "").replace("https://polygonscan.com/tx/", ""),
            "Notas":            nota_completa,
            "Wallet/Alias":     ev.get("wallet_alias", ""),
        })

    # ── 5. Aave — Prestamista ─────────────────────────────────────────────────
    for m in (aave_lender if aave_lender else []):
        signo = -1.0 if m["tipo"] == "Depósito préstamo" else 1.0
        rows.append({
            "Fecha UTC":        m["fecha_str"],
            "Año fiscal":       m["fecha_str"][:4],
            "Categoría":        "Aave (prestamista)",
            "Tipo operación":   m["tipo"],
            "Activo":           m.get("stable_symbol", "USDT"),
            "Cantidad":         round(signo * m["stable_amount"], 4),
            "Precio unit. USD": 1.0,
            "Valor USD":        round(m["stable_amount"], 2),
            "Fuente precio":    "TX exacta",
            "Contraparte":      "Aave Protocol",
            "TX Hash":          m.get("tx_link", "").replace("https://polygonscan.com/tx/", ""),
            "Notas":            m.get("interest_note", ""),
            "Wallet/Alias":     m.get("wallet_alias", ""),
        })

    # ── 6. Aave — Prestatario ─────────────────────────────────────────────────
    for m in (aave_borrower if aave_borrower else []):
        tipo = m["tipo"]
        if tipo == "Préstamo recibido":
            signo = 1.0
            nota = "USDT recibido como préstamo. Genera obligación de devolución."
        elif tipo == "Pago de deuda":
            signo = -1.0
            nota = "Devolución (total o parcial) del préstamo + intereses."
        elif tipo == "Garantía depositada":
            signo = -1.0
            nota = "Tokens inmobiliarios depositados como colateral en Aave."
        else:  # Garantía retirada
            signo = 1.0
            nota = "Tokens inmobiliarios retirados del colateral en Aave."

        amt = m.get("stable_amount") or 0.0
        rows.append({
            "Fecha UTC":        m["fecha_str"],
            "Año fiscal":       m["fecha_str"][:4],
            "Categoría":        "Aave (prestatario)",
            "Tipo operación":   tipo,
            "Activo":           m.get("stable_symbol") or m.get("detalle", ""),
            "Cantidad":         round(signo * (amt or m.get("cantidad", 0.0)), 4),
            "Precio unit. USD": 1.0 if amt else "",
            "Valor USD":        round(amt, 2) if amt else "",
            "Fuente precio":    "TX exacta" if amt else "N/A",
            "Contraparte":      "Aave Protocol",
            "TX Hash":          m.get("tx_link", "").replace("https://polygonscan.com/tx/", ""),
            "Notas":            nota,
            "Wallet/Alias":     m.get("wallet_alias", ""),
        })

    # Enriquecer cada fila con su valor en EUR al tipo de cambio de la fecha.
    # get_eurusd_on_date está cacheado, así que fechas repetidas no repiten API.
    for r in rows:
        _, eur, rate = fiat_values(r.get("Valor USD", ""), r.get("Fecha UTC", ""))
        r["Valor EUR"]    = eur
        r["Tipo EUR/USD"] = rate

    # Ordenar todo cronológicamente
    rows.sort(key=lambda r: r["Fecha UTC"])
    df = pd.DataFrame(rows)

    # Colocar las columnas EUR justo detrás de "Valor USD"
    cols = list(df.columns)
    if "Valor USD" in cols and "Valor EUR" in cols:
        for c in ("Valor EUR", "Tipo EUR/USD"):
            cols.remove(c)
        i = cols.index("Valor USD") + 1
        cols[i:i] = ["Valor EUR", "Tipo EUR/USD"]
        df = df[cols]

    return df.to_csv(index=False).encode("utf-8")

with st.spinner("Calculando resumen fiscal agregado…"):
    agg = build_aggregate_report()

# ── Resumen agregado (KPIs) ───────────────────────────────────────────────────
st.markdown("##### 🧾 Resumen agregado")

_orden_conceptos = [
    "Dividendos inmobiliarios", "Intereses Aave (prestamista)",
    "Staking (recompensas)", "Farming (recompensas)",
]
_iconos = {
    "Dividendos inmobiliarios": "🏠", "Intereses Aave (prestamista)": "🏦",
    "Staking (recompensas)": "🔒", "Farming (recompensas)": "🌾",
}
_rend_total_usd = sum(v["usd"] for v in agg["rend_tot"].values())
_rend_total_eur = sum(v["eur"] for v in agg["rend_tot"].values())

st.caption("Rendimientos (rentas del ejercicio), por naturaleza fiscal:")
_rcols = st.columns(4)
for _i, _concepto in enumerate(_orden_conceptos):
    _v = agg["rend_tot"].get(_concepto, {"usd": 0.0, "eur": 0.0})
    _rcols[_i].markdown(kpi_card(
        _iconos[_concepto], _concepto,
        f"${_v['usd']:,.2f}",
        sublabel=f"€{_v['eur']:,.2f}",
    ), unsafe_allow_html=True)

if agg["rend_pendiente_rnt"]:
    _n_pend = len(agg["rend_pendiente_rnt"])
    _rnt_pend_total = sum(r["Cantidad RNT"] for r in agg["rend_pendiente_rnt"])
    st.caption(
        f"🟠 **{_n_pend} recompensa(s) de RNT sin valorar** ({_rnt_pend_total:,.4f} RNT en total): "
        "el precio de RNT no estaba disponible en CoinGecko para esas fechas (fuera de la ventana "
        "de 365 días del plan gratuito, o límite de peticiones). No se han contado como $0 — "
        "revisar la hoja «RNT sin valorar» del XLSX y completar con el precio de mercado real."
    )

st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
st.caption("Patrimonio a la fecha de corte (para modelos de bienes/patrimonio):")
_scols = st.columns(3)
_scols[0].markdown(kpi_card("💰", "Total rendimientos",
                            f"${_rend_total_usd:,.2f}", sublabel=f"€{_rend_total_eur:,.2f}"),
                   unsafe_allow_html=True)
_scols[1].markdown(kpi_card("🏘️", "Valor tokens en cartera",
                            f"${agg['holdings_tot']['usd']:,.2f}", sublabel=f"€{agg['holdings_tot']['eur']:,.2f}"),
                   unsafe_allow_html=True)
_deuda_eur_lbl = f"€{agg['deuda_eur']:,.2f}" if agg["deuda_eur"] != "" else "—"
_scols[2].markdown(kpi_card("⚠️", "Deuda viva en Aave",
                            f"${agg['deuda_usd']:,.2f}", sublabel=_deuda_eur_lbl,
                            value_color="#dc2626" if agg["deuda_usd"] > 0.01 else "#16a34a"),
                   unsafe_allow_html=True)

st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
st.caption("Ganancias patrimoniales por ventas de tokens (método FIFO):")
_cg = agg["capital_gains"]
_gcols = st.columns(3)
_gcols[0].markdown(kpi_card("📈", "Ganancia patrimonial calculable",
                            f"${_cg['kpi']['calc_usd']:,.2f}", sublabel=f"€{_cg['kpi']['calc_eur']:,.2f}",
                            value_color="#16a34a" if _cg['kpi']['calc_usd'] >= 0 else "#dc2626"),
                   unsafe_allow_html=True)
_gcols[1].markdown(kpi_card("🟠", "Operaciones provisionales",
                            str(_cg['kpi']['prov_n']), sublabel="coste estimado (revisar)"),
                   unsafe_allow_html=True)
_gcols[2].markdown(kpi_card("🔴", "Operaciones pendientes",
                            str(_cg['kpi']['pend_n']), sublabel="falta dato — ver «Por completar»"),
                   unsafe_allow_html=True)

if _cg["kpi"]["prov_n"] or _cg["kpi"]["pend_n"]:
    st.caption(
        "⚠️ La ganancia calculable solo suma operaciones con coste y transmisión exactos on-chain. "
        "Las provisionales/pendientes requieren completar el valor real (p.ej. compras o ventas en FIAT) "
        "en la hoja «Por completar» del XLSX."
    )

_exp_cols = st.columns(3) if agg["rend_pendiente_rnt"] else st.columns(2)
if agg["rend_rows"]:
    with _exp_cols[0].expander("Ver rendimientos por año"):
        st.dataframe(pd.DataFrame(agg["rend_rows"]), hide_index=True, use_container_width=True)
if _cg["gains"]:
    with _exp_cols[1].expander("Ver detalle de plusvalías (FIFO)"):
        st.dataframe(pd.DataFrame(_cg["gains"]), hide_index=True, use_container_width=True)
if agg["rend_pendiente_rnt"]:
    with _exp_cols[2].expander("Ver RNT sin valorar"):
        st.dataframe(pd.DataFrame(agg["rend_pendiente_rnt"]), hide_index=True, use_container_width=True)

# ── Descargables ──────────────────────────────────────────────────────────────
st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
_csv_filename_suffix = wallet[:8] if not es_multi_wallet else f"multi{len(wallets_analyzed)}_{wallet[:8]}"

with st.spinner("Preparando resumen agregado (XLSX)…"):
    xlsx_agg = build_aggregate_xlsx(agg)
st.caption(
    "**Resumen agregado (XLSX)** — hojas Informe · Resumen · Rendimientos · RNT sin valorar · "
    "Saldos · Plusvalías (FIFO) · Lotes abiertos · Por completar · Glosario, con los totales listos "
    "para las casillas de los modelos y la lista de importes que el inversor debe completar "
    "(compras en FIAT, recompensas de RNT sin precio histórico disponible). Sin hashes ni contratos."
)
st.download_button(
    "⬇️ Descargar resumen agregado (XLSX)",
    data=xlsx_agg,
    file_name=f"reental_resumen_fiscal_{_csv_filename_suffix}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    type="primary",
    use_container_width=True,
)

st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
with st.spinner("Preparando CSV granular…"):
    csv_fiscal = build_fiscal_csv()

st.caption(
    "**CSV granular** — todos los movimientos en orden cronológico (tokens inmobiliarios, "
    "dividendos, stablecoins, ecosistema RNT y Aave), con columna Wallet/Alias" +
    (" y transferencias internas marcadas" if es_multi_wallet else "") + ". Respaldo legal para la autoridad fiscal."
)
st.download_button(
    "⬇️ Descargar CSV granular (respaldo)",
    data=csv_fiscal,
    file_name=f"reental_informe_fiscal_{_csv_filename_suffix}.csv",
    mime="text/csv",
    type="primary",
    use_container_width=True,
)

with st.expander("📖 Glosario — cómo interpretar fiscalmente cada operación"):
    st.dataframe(pd.DataFrame(FISCAL_GLOSARIO), hide_index=True, use_container_width=True)
    st.caption(
        "Este informe **no es asesoramiento fiscal**: describe la naturaleza de cada operación de forma "
        "agnóstica a la jurisdicción; el tipo impositivo concreto lo determina el país del inversor. "
        "Las fechas están en UTC y el USDT se asume a la par del USD. Los importes marcados como estimados "
        "o pendientes (p.ej. compras en FIAT) deben completarse con los datos del inversor."
    )
