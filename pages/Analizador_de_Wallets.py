import os
import json
import io
import unicodedata
from datetime import datetime, date, timedelta
from collections import defaultdict

import streamlit as st
import pandas as pd
import requests
import plotly.graph_objects as go
from dotenv import load_dotenv

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


@st.cache_data(show_spinner=False, ttl=86400)
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

        mint_log     = logs[0]
        mint_tx_hash = mint_log["transactionHash"].lower()
        block_num    = mint_log["blockNumber"]

        # 2. En ese mismo bloque, buscar Transfer de RNT hacia STAKING_RECEIVER
        receiver_topic = "0x000000000000000000000000" + STAKING_RECEIVER[2:].lower()
        r2 = requests.get(ETHERSCAN_V2_BASE, params={
            "chainid": POLYGON_CHAIN_ID, "module": "logs", "action": "getLogs",
            "address": RNT_CONTRACT,
            "fromBlock": block_num, "toBlock": block_num,
            "topic0": TRANSFER_TOPIC, "topic0_2_opr": "and", "topic2": receiver_topic,
            "apikey": API_KEY,
        }, timeout=20)
        for log in (r2.json().get("result") or []):
            if log.get("transactionHash", "").lower() == mint_tx_hash:
                return int(log["data"], 16) / 1e18
    except Exception:
        pass
    return 0.0


def fetch_token_transfers(wallet: str) -> list:
    if not API_KEY:
        raise ValueError("No hay API Key configurada. Añade ETHERSCAN_API_KEY en el archivo .env")
    params = {"chainid": POLYGON_CHAIN_ID, "module": "account", "action": "tokentx",
              "address": wallet, "startblock": 0, "endblock": 99999999,
              "sort": "asc", "apikey": API_KEY}
    try:
        r = requests.get(ETHERSCAN_V2_BASE, params=params, timeout=30)
    except requests.exceptions.RequestException as e:
        raise ValueError(f"Error de red: {e}")
    if not r.text.strip():
        raise ValueError("Respuesta vacía de la API.")
    try:
        data = r.json()
    except Exception:
        raise ValueError(f"Respuesta inesperada (HTTP {r.status_code}): {r.text[:300]}")
    if data.get("status") == "0":
        msg = data.get("result") or data.get("message", "")
        if msg not in ("No transactions found", "No records found", ""):
            raise ValueError(f"API: {msg}")
    return data.get("result") or []


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


def label_address(addr: str, wallet: str, atoken_contracts: dict, reental_addresses: set = None) -> str:
    addr = addr.lower()
    if addr == wallet:
        return "Tu wallet"
    if addr == ZERO_ADDRESS:
        return "Protocolo"
    if addr in atoken_contracts:
        return f"Aave — {atoken_contracts[addr]}"
    if reental_addresses and addr in reental_addresses:
        return "Wallet Reental"
    return "Wallet de un tercero"


def process_transfers(transfers: list, wallet: str, known_tokens: dict, reental_addresses: set = None) -> dict:
    wallet = wallet.lower()
    reental_addresses = reental_addresses or set()

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

            if is_aave:
                if direction == "entrada":
                    tipo = "Colateralización en Aave"
                    origen = label_address(from_addr, wallet, atoken_contracts, reental_addresses)
                    destino = "Tu wallet"
                else:
                    tipo = "Descolateralización en Aave"
                    origen = "Tu wallet"
                    destino = label_address(to_addr, wallet, atoken_contracts, reental_addresses)
            elif direction == "entrada":
                if atoken_out:
                    # El usuario quemó aTokens en el mismo TX → está recuperando su colateral
                    tipo = "Descolateralización en Aave"
                    origen = label_address(from_addr, wallet, atoken_contracts, reental_addresses)
                    destino = "Tu wallet"
                elif stable_out > 0:
                    # Compra directa: el usuario pagó USDT desde su propia wallet
                    if from_addr in reental_addresses:
                        tipo = f"Compra de Tokens a Reental ({stable_out:,.2f} {stable_symbol})"
                    else:
                        tipo = f"Compra de Tokens a terceros ({stable_out:,.2f} {stable_symbol})"
                    origen = label_address(from_addr, wallet, atoken_contracts, reental_addresses)
                    destino = "Tu wallet"
                else:
                    # Comprobar patrón reinversión vault: USDT sale del vault → vendedor en misma TX
                    vault_info = get_vault_payment(tx["hash"], wallet)
                    if vault_info:
                        vault_addr, vault_amount, vault_sym = vault_info
                        vault_sym = normalize_stable_symbol(vault_sym)
                        tipo = f"Reinversión desde vault ({vault_amount:,.2f} {vault_sym})"
                        origen = f"Vault ({vault_addr[:8]}…{vault_addr[-4:]})"
                        destino = "Tu wallet"
                        stable_out = vault_amount
                        stable_symbol = vault_sym
                    else:
                        tipo = "Entrada de Tokens *"
                        origen = label_address(from_addr, wallet, atoken_contracts, reental_addresses)
                        destino = "Tu wallet"
            else:  # salida
                if atoken_in:
                    tipo = "Colateralización en Aave"
                    origen = "Tu wallet"
                    destino = label_address(to_addr, wallet, atoken_contracts, reental_addresses)
                elif stable_in > 0:
                    tipo = f"Venta de tokens ({stable_in:,.2f} {stable_symbol})"
                    origen = "Tu wallet"
                    destino = "Protocolo liquidación proyecto" if to_addr == ZERO_ADDRESS else label_address(to_addr, wallet, atoken_contracts, reental_addresses)
                else:
                    # El USDT puede ir al vault del usuario en lugar de a la wallet directamente
                    vault_info = get_vault_payment(tx["hash"], wallet)
                    if vault_info:
                        vault_addr, vault_amount, vault_sym = vault_info
                        vault_sym = normalize_stable_symbol(vault_sym)
                        tipo = f"Venta de tokens ({vault_amount:,.2f} {vault_sym} al vault)"
                        origen = "Tu wallet"
                        destino = "Protocolo liquidación proyecto" if to_addr == ZERO_ADDRESS else label_address(to_addr, wallet, atoken_contracts, reental_addresses)
                        stable_in = vault_amount
                        stable_symbol = vault_sym
                    else:
                        destino_label = label_address(to_addr, wallet, atoken_contracts, reental_addresses)
                        if destino_label == "Wallet Reental":
                            tipo = "Salida de Tokens *"
                        else:
                            tipo = "Salida de tokens"
                        origen = "Tu wallet"
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
            })

    # Ordenar movimientos por fecha dentro de cada token
    for data in token_data.values():
        data["movements"].sort(key=lambda m: m["fecha"])

    return dict(token_data)


def balance_at_date(movements: list, cutoff: date) -> float:
    return sum(m["cantidad_neta"] for m in movements if m["fecha"].date() <= cutoff)


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


DIVIDEND_DISTRIBUTOR = "0xf9b135fd84ae6dc9d6e632a97235de5f08c0d61e"

@st.cache_data(show_spinner=False, ttl=3600)
def fetch_normal_transactions(wallet: str) -> list:
    """Transacciones normales (ETH) de la wallet — necesarias para detectar el vault."""
    if not API_KEY:
        return []
    params = {
        "chainid": POLYGON_CHAIN_ID, "module": "account", "action": "txlist",
        "address": wallet, "startblock": 0, "endblock": 99999999,
        "sort": "asc", "apikey": API_KEY,
    }
    try:
        r = requests.get(ETHERSCAN_V2_BASE, params=params, timeout=30)
        data = r.json()
        return data.get("result") or [] if data.get("status") == "1" else []
    except Exception:
        return []


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_distributor_recipients() -> set:
    """Conjunto de todas las direcciones que han recibido dividendos del distribuidor."""
    if not API_KEY:
        return set()
    params = {
        "chainid": POLYGON_CHAIN_ID, "module": "account", "action": "tokentx",
        "address": DIVIDEND_DISTRIBUTOR,
        "startblock": 0, "endblock": 99999999, "sort": "desc", "apikey": API_KEY,
    }
    try:
        r = requests.get(ETHERSCAN_V2_BASE, params=params, timeout=30)
        data = r.json()
        return {tx["to"].lower() for tx in (data.get("result") or [])}
    except Exception:
        return set()


def detect_vault_address(wallet: str) -> str | None:
    """
    Detecta el vault personal del inversor cruzando dos fuentes:
    1. Contratos que la wallet ha llamado directamente (txlist normal).
    2. Direcciones que han recibido USDT del distribuidor de Reental.
    La intersección es el vault personal.
    """
    wallet = wallet.lower()
    normal_txs = fetch_normal_transactions(wallet)
    if not normal_txs:
        return None

    called = {tx["to"].lower() for tx in normal_txs
              if tx.get("to") and tx["from"].lower() == wallet
              and tx["to"].lower() != wallet}  # excluir self-transfers

    if not called:
        return None

    dist_recipients = fetch_distributor_recipients()
    candidates = called & dist_recipients

    if not candidates:
        return None
    # Si hay varios candidatos, devolver el primero (raro en la práctica)
    return next(iter(candidates))


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_vault_transfers(vault_address: str) -> list:
    """Obtiene todas las transferencias de stablecoin hacia el vault."""
    if not API_KEY or not vault_address:
        return []
    params = {
        "chainid": POLYGON_CHAIN_ID, "module": "account", "action": "tokentx",
        "address": vault_address,
        "startblock": 0, "endblock": 99999999, "sort": "asc", "apikey": API_KEY,
    }
    try:
        r = requests.get(ETHERSCAN_V2_BASE, params=params, timeout=30)
        data = r.json()
        if data.get("status") == "0":
            return []
        return data.get("result") or []
    except Exception:
        return []


def process_dividends(transfers: list, wallet: str, vault_transfers: list = None) -> list:
    """
    Detecta pagos de dividendos desde el distribuidor de Reental hacia:
    a) la wallet directamente, y
    b) el vault personal del usuario (dividendos acumulados sin retirar).
    Combina ambas fuentes y devuelve la lista ordenada por fecha.
    """
    wallet = wallet.lower()

    def _build_events(tx_list: list, recipient: str, destino_label: str) -> list:
        div_txs = defaultdict(list)
        for tx in tx_list:
            if (tx["from"].lower() == DIVIDEND_DISTRIBUTOR
                    and tx["to"].lower() == recipient
                    and tx["contractAddress"].lower() in STABLECOIN_CONTRACTS):
                div_txs[tx["hash"]].append(tx)
        evs = []
        for tx_hash, group in div_txs.items():
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

    if vault_transfers:
        # La vault puede tener cualquier dirección; buscamos al distribuidor como from
        vault_addr = next(
            (tx["to"].lower() for tx in vault_transfers
             if tx["from"].lower() == DIVIDEND_DISTRIBUTOR
             and tx["contractAddress"].lower() in STABLECOIN_CONTRACTS),
            None,
        )
        if vault_addr:
            events += _build_events(vault_transfers, vault_addr, "Vault personal")

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
def get_eurusd_on_date(date_str: str) -> float:
    """Tipo de cambio EUR/USD para una fecha dada (YYYY-MM-DD). Usa CoinGecko (euro vs usd).
    Devuelve None si no se puede obtener."""
    try:
        dd = date_str[8:10]; mm = date_str[5:7]; yyyy = date_str[:4]
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/tether/history",
            params={"date": f"{dd}-{mm}-{yyyy}", "localization": "false"},
            timeout=10,
        )
        # Tether (USDT) en EUR nos da 1/EURUSD → invertimos
        eur_price = r.json().get("market_data", {}).get("current_price", {}).get("eur")
        if eur_price and float(eur_price) > 0:
            return round(1.0 / float(eur_price), 4)
    except Exception:
        pass
    # Fallback: ECB via exchangerate.host (sin API key)
    try:
        r2 = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "EUR", "to": "USD"},
            timeout=10,
        )
        return float(r2.json()["rates"]["USD"])
    except Exception:
        return None


# ── UI ───────────────────────────────────────────────────────────────────────

wallet_input = st.text_input("🔑 Dirección de la wallet del inversor", placeholder="0x1234abcd...")
filter_date = st.date_input("📅 Filtrar por fecha (opcional — saldo a esa fecha)", value=None)
analyze_btn = st.button("🔍 Analizar cartera", type="primary", use_container_width=True)

if analyze_btn:
    wallet = wallet_input.strip().lower()
    if not wallet or not wallet.startswith("0x") or len(wallet) != 42:
        st.error("Introduce una dirección de wallet válida (0x... 42 caracteres).")
        st.stop()

    progress_bar = st.progress(0, text="⏳ Iniciando análisis…")

    progress_bar.progress(10, text="📋 Cargando lista de tokens de Reental…")
    known_tokens = load_tokens()
    reental_addresses = load_reental_addresses()

    progress_bar.progress(30, text="🔗 Consultando historial de transacciones en Polygon…")
    try:
        transfers = fetch_token_transfers(wallet)
    except Exception as e:
        progress_bar.empty()
        st.error(f"Error al consultar Polygonscan: {e}")
        st.stop()

    progress_bar.progress(55, text=f"🔍 Procesando {len(transfers)} transferencias encontradas…")
    token_data = process_transfers(transfers, wallet, known_tokens, reental_addresses)

    n_vault_candidates = sum(
        1 for data in token_data.values()
        for m in data["movements"]
        if m["tipo"] == "Recepción / Distribución"
    )
    if n_vault_candidates:
        progress_bar.progress(75, text=f"🏦 Verificando {n_vault_candidates} posibles reinversiones desde vault…")

    progress_bar.progress(88, text="🏦 Buscando vault personal del inversor…")
    vault_addr = detect_vault_address(wallet)
    vault_transfers = fetch_vault_transfers(vault_addr) if vault_addr else []

    progress_bar.progress(95, text="📊 Preparando resultados…")
    st.session_state.update({
        "token_data": token_data, "wallet": wallet,
        "filter_date": filter_date,
        "raw_transfers": transfers,
        "vault_addr": vault_addr,
        "vault_transfers": vault_transfers,
    })
    progress_bar.progress(100, text="✅ Análisis completado")
    progress_bar.empty()

if "token_data" not in st.session_state:
    st.stop()

token_data = st.session_state["token_data"]
wallet = st.session_state["wallet"]
filter_date = st.session_state["filter_date"]

if not token_data:
    st.info("No se encontraron tokens de Reental en esta wallet.")
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

if activos:
    today_str = date.today().strftime("%d/%m/%Y")

    def parse_fecha(s):
        try:
            return datetime.strptime(s, "%d/%m/%Y").date()
        except Exception:
            return None

    rows = []
    for d in sorted(activos.values(), key=lambda x: x["info"]["label"]):
        fecha_fin = get_fecha_fin_display(d["info"], closing_dates)
        rows.append({
            "Token": d["info"]["label"],
            "Nombre": d["info"]["name"],
            "Tipo": "🏦 Token Inmobiliario Colateralizado" if d["info"].get("is_aave") else "🏠 Token Inmobiliario",
            "Saldo": d["balance_display"],
            "Fecha real de fin de proyecto": fecha_fin,
            "Nº mov.": len(d["movements"]),
            "Ver en Polygonscan": f"https://polygonscan.com/token/{d['info']['address']}?a={wallet}",
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
        "Fecha real de fin de proyecto": st.column_config.TextColumn(width="medium"),
    }, hide_index=True, use_container_width=True)
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
        "Ver en Polygonscan": f"https://polygonscan.com/token/{d['info']['address']}?a={wallet}",
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
movs = selected_data["movements"]

if movs:
    rows = []
    saldo_tokens = 0.0
    saldo_stable = 0.0
    rendimiento_acum = 0.0
    for m in movs:
        saldo_tokens += m["cantidad_neta"]
        if m["stable_in"] > 0 and m["cantidad_neta"] < 0:
            saldo_stable += m["stable_in"]
        elif m["stable_out"] > 0 and m["cantidad_neta"] > 0:
            saldo_stable -= m["stable_out"]

        # Rendimiento: únicamente USDT recibido al vender ESE token
        # Las reinversiones desde vault NO computan: representan dividendos de OTROS tokens anteriores
        es_venta = m["cantidad_neta"] < 0 and m["stable_in"] > 0
        if es_venta:
            rendimiento_acum += m["stable_in"]
        rendimiento_col = f"{rendimiento_acum:,.2f}" if rendimiento_acum > 0 else "—"

        tokens_col = f"+{m['cantidad']:,.4f}" if m["cantidad_neta"] > 0 else f"-{m['cantidad']:,.4f}"

        if m["cantidad_neta"] < 0 and m["stable_in"] > 0:
            stable_col = f"+{m['stable_in']:,.2f} {m['stable_symbol']}"
        elif m["cantidad_neta"] > 0 and m["stable_out"] > 0:
            stable_col = f"-{m['stable_out']:,.2f} {m['stable_symbol']}"
        else:
            stable_col = "—"

        rows.append({
            "Fecha": m["fecha_str"],
            "Operación": m["tipo"],
            "Origen": m["origen"],
            "Destino": m["destino"],
            "Tokens": tokens_col,
            "Saldo tokens": round(saldo_tokens, 4),
            "Stablecoin": stable_col,
            "Saldo USDT": f"{saldo_stable:,.2f}",
            "Rendimiento acum.": rendimiento_col,
            "TX": m["tx_link"],
        })

    df_movs = pd.DataFrame(rows)

    def style_address_cell(val):
        if val == "Tu wallet":
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
    if has_entrada_sin_usdt or has_salida_sin_usdt:
        partes = []
        if has_entrada_sin_usdt:
            partes.append(
                "**Entrada de Tokens \\***: no se identificó contraparte de USDT en la misma transacción. "
                "Posibles causas: (1) compra con dinero FIAT (el pago queda en el banco, no en blockchain); "
                "(2) el USDT fue enviado en una transacción separada; "
                "(3) envío interno entre billeteras del propio inversor."
            )
        if has_salida_sin_usdt:
            partes.append(
                "**Salida de Tokens \\***: los tokens salieron de tu billetera sin una contrapartida en USDT o USDC "
                "en la misma transacción por alguna de las siguientes razones: (1) los tokens fueron enviados a una "
                "billetera adicional controlada por el usuario; (2) los tokens fueron enviados a una billetera de un "
                "tercero donde la contraprestación pudo haberse recibido vía cripto antes o después del envío, o por "
                "transferencia FIAT — en ese caso habría que analizarlo en el banco."
            )
        st.caption("\\* " + "  \n\\* ".join(partes))
else:
    st.info("Sin movimientos para este token.")

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
st.subheader("💵 Movimientos de Stablecoins relacionados con Reental")
st.caption("Solo flujos de USDT/USDC directamente vinculados a compras, ventas o reinversiones de tokens inmobiliarios.")

stable_movs = build_stablecoin_movements(token_data)
if stable_movs:
    saldo = 0.0
    stable_rows = []
    for m in stable_movs:
        if m["direction"] == "entrada":
            saldo += m["amount"]
            amount_str = f"+{m['amount']:,.2f} {m['stable_symbol']}"
        else:
            saldo -= m["amount"]
            amount_str = f"-{m['amount']:,.2f} {m['stable_symbol']}"
        stable_rows.append({
            "Fecha": m["fecha_str"],
            "Token inmobiliario": m["token"],
            "Operación": m["operacion"],
            "Importe": amount_str,
            "Saldo acumulado": f"{saldo:,.2f} {m['stable_symbol']}",
            "TX": m["tx_link"],
        })
    st.dataframe(pd.DataFrame(stable_rows), column_config={
        "TX": st.column_config.LinkColumn("Ver TX", width="small"),
    }, hide_index=True, use_container_width=True)

    # Métricas resumen
    total_invertido = sum(m["amount"] for m in stable_movs if m["direction"] == "salida")
    total_recibido  = sum(m["amount"] for m in stable_movs if m["direction"] == "entrada")
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
    st.info("No se detectaron flujos de stablecoins relacionados con Reental en esta wallet.")

raw_transfers    = st.session_state.get("raw_transfers", [])
vault_addr       = st.session_state.get("vault_addr")
vault_transfers  = st.session_state.get("vault_transfers", [])
dividends = process_dividends(raw_transfers, wallet, vault_transfers)

# ── Dividendos recibidos de Reental ──────────────────────────────────────────
st.markdown("---")
st.subheader("💰 Dividendos recibidos de Reental")

_vault_note = (
    f"Incluye dividendos acumulados en tu vault personal (`{vault_addr[:8]}…{vault_addr[-4:]}`) "
    "además de los recibidos directamente en esta wallet."
    if vault_addr else
    "Solo se muestran dividendos recibidos directamente en esta wallet "
    "(no se detectó vault personal asociado)."
)
st.caption(
    "Pagos de rendimientos distribuidos por Reental. "
    "Cada fila es una distribución (puede incluir varios proyectos en el mismo TX). " + _vault_note
)

if dividends:
    saldo_div = 0.0
    div_rows = []
    for ev in dividends:
        saldo_div += ev["total"]
        detalle = "  +  ".join(f"{p:,.2f}" for p in ev["pagos"])
        div_rows.append({
            "Fecha":          ev["fecha_str"],
            "Destino":        ev.get("destino", "Wallet directa"),
            "Nº proyectos":   ev["n_proyectos"],
            "Detalle pagos":  detalle,
            "Total recibido": f"{ev['total']:,.2f} {ev['sym']}",
            "Acumulado":      f"{saldo_div:,.2f} {ev['sym']}",
            "TX": ev["tx_link"],
        })

    st.dataframe(pd.DataFrame(div_rows), column_config={
        "TX": st.column_config.LinkColumn("Ver TX", width="small"),
        "Nº proyectos": st.column_config.NumberColumn(width="small"),
    }, hide_index=True, use_container_width=True)

    total_div      = sum(ev["total"] for ev in dividends)
    total_wallet   = sum(ev["total"] for ev in dividends if ev.get("destino") == "Wallet directa")
    total_vault    = sum(ev["total"] for ev in dividends if ev.get("destino") == "Vault personal")
    c1, c2, c3 = st.columns(3)
    c1.markdown(kpi_card("💵", "Total dividendos",       f"{total_div:,.2f}",
                          value_color="#16a34a", sublabel="USDT / USDC (wallet + vault)"), unsafe_allow_html=True)
    c2.markdown(kpi_card("📬", "Distribuciones",          str(len(dividends)),
                          sublabel="pagos recibidos"), unsafe_allow_html=True)
    c3.markdown(kpi_card("📊", "Media por distribución",  f"{total_div / len(dividends):,.2f}",
                          sublabel="USDT / USDC"), unsafe_allow_html=True)
    if vault_addr and total_vault > 0:
        st.caption(
            f"Desglose: **${total_wallet:,.2f}** recibidos en wallet directa · "
            f"**${total_vault:,.2f}** acumulados en vault personal"
        )
else:
    st.info("No se detectaron dividendos recibidos en esta wallet ni en su vault personal.")

# ── Actividad en Aave ────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("🏦 Actividad en Aave (USDT/USDC)")
st.caption("Actividad en el protocolo Aave: préstamos otorgados (prestamista) y créditos tomados usando tokens inmobiliarios como garantía (prestatario).")

aave_activity = process_aave_activity(raw_transfers, wallet)
aave_lender   = aave_activity["lender"]
aave_borrower = aave_activity["borrower"]
aave_lending  = aave_lender   # alias para el exportador CSV

# ── Prestamista ───────────────────────────────────────────────────────────────
st.markdown("#### 🏦 Como prestamista")
if aave_lender:
    saldo_at = 0.0
    lender_rows = []
    for m in aave_lender:
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
        lender_rows.append(row)

    st.dataframe(pd.DataFrame(lender_rows), column_config={
        "TX": st.column_config.LinkColumn("Ver TX", width="small"),
    }, hide_index=True, use_container_width=True)

    total_dep   = sum(m["stable_amount"] for m in aave_lender if m["tipo"] == "Depósito préstamo")
    total_ret   = sum(m["stable_amount"] for m in aave_lender if m["tipo"] == "Retirada préstamo")
    at_dep      = sum(m["cantidad_atoken"] for m in aave_lender if m["tipo"] == "Depósito préstamo")
    at_ret      = sum(m["cantidad_atoken"] for m in aave_lender if m["tipo"] == "Retirada préstamo")
    saldo_vivo  = max(0.0, at_dep - at_ret)
    pos_abierta = saldo_vivo > 0.01
    int_netos   = max(0.0, total_ret + saldo_vivo - total_dep)
    rent_pct    = (int_netos / total_dep * 100) if total_dep > 0 else 0.0

    flujos_irr = []
    for m in aave_lender:
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
    st.info("No se detectó actividad como prestamista en Aave en esta wallet.")

# ── Prestatario ───────────────────────────────────────────────────────────────
st.markdown("#### 🏛️ Como prestatario")
if aave_borrower:
    borrower_rows = []
    for m in aave_borrower:
        if m["tipo"] in ("Préstamo recibido", "Pago de deuda"):
            importe_str = (
                f"+{m['stable_amount']:,.2f} {m['stable_symbol']}" if m["tipo"] == "Préstamo recibido"
                else f"-{m['stable_amount']:,.2f} {m['stable_symbol']}"
            ) if m["stable_amount"] else "—"
        else:
            importe_str = "—"
        borrower_rows.append({
            "Fecha":      m["fecha_str"],
            "Operación":  m["tipo"],
            "Detalle":    m["detalle"],
            "Cantidad":   f"{m['cantidad']:,.4f}",
            "USDT/USDC":  importe_str,
            "TX":         m["tx_link"],
        })

    st.dataframe(pd.DataFrame(borrower_rows), column_config={
        "TX": st.column_config.LinkColumn("Ver TX", width="small"),
    }, hide_index=True, use_container_width=True)

    total_prestado  = sum(m["stable_amount"] for m in aave_borrower if m["tipo"] == "Préstamo recibido")
    total_devuelto  = sum(m["stable_amount"] for m in aave_borrower if m["tipo"] == "Pago de deuda")
    coste_neto      = max(0.0, total_devuelto - total_prestado)
    deuda_viva      = max(0.0, total_prestado - total_devuelto)
    n_garantias_dep = sum(1 for m in aave_borrower if m["tipo"] == "Garantía depositada")
    n_garantias_ret = sum(1 for m in aave_borrower if m["tipo"] == "Garantía retirada")
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
    st.info("No se detectó actividad como prestatario en Aave en esta wallet.")

# ── Ecosistema RNT — Staking y Farming ───────────────────────────────────────
st.markdown("---")
st.subheader("🪙 Ecosistema RNT — Staking y Farming")
st.caption(
    "Actividad relacionada con el token RNT: staking, pool de liquidez RNT/USDT (SLP) y farming (frmRNT)."
)

xrnt_nft_transfers = fetch_nft_transfers(wallet, STAKING_RECEIVER)
rnt_events = process_rnt_ecosystem(raw_transfers, wallet, xrnt_nft_transfers)

if rnt_events:
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

    for ev in rnt_events:
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

        rnt_rows.append({
            "Fecha":       ev["fecha_str"],
            "Operación":   ev["tipo"],
            "Detalle":     ev["detalle"],
            "Saldo RNT":   round(bal_rnt, 4),
            "Saldo frmRNT": round(bal_frmrnt, 6),
            "TX": ev["tx_link"],
        })

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
    notas_pie = [ev["nota_asterisco"] for ev in rnt_events if ev.get("nota_asterisco")]
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
    st.info("No se detectó actividad en el ecosistema RNT (staking, farming o pool de liquidez) en esta wallet.")

# ── Exportar (informe fiscal completo) ───────────────────────────────────────
st.markdown("---")
st.subheader("📄 Exportar informe fiscal completo")
st.caption(
    "Todos los movimientos de la wallet en orden cronológico: tokens inmobiliarios, "
    "dividendos, stablecoins, ecosistema RNT y Aave. Diseñado para asesores fiscales."
)

@st.cache_data(show_spinner=False, ttl=86400)
def get_rnt_price_on_date(date_str: str):
    """
    Devuelve el precio de RNT en USD para una fecha dada (formato YYYY-MM-DD).
    Usa CoinGecko historical data. Devuelve None si no disponible.
    """
    try:
        dd, mm, yyyy = date_str[8:10], date_str[5:7], date_str[:4]
        cg_date = f"{dd}-{mm}-{yyyy}"
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/reental/history",
            params={"date": cg_date, "localization": "false"},
            timeout=10,
        )
        price = r.json().get("market_data", {}).get("current_price", {}).get("usd")
        return float(price) if price else None
    except Exception:
        return None

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

            rows.append({
                "Fecha UTC":        m["fecha_str"],
                "Año fiscal":       m["fecha_str"][:4],
                "Categoría":        "Token inmobiliario",
                "Tipo operación":   m["tipo"],
                "Activo":           f"{nombre} ({simbolo})",
                "Cantidad":         round(cantidad, 6),
                "Precio unit. USD": round(precio_unit, 4) if precio_unit else "",
                "Valor USD":        valor_usd if valor_usd else "",
                "Fuente precio":    fuente_precio,
                "Contraparte":      m["destino"] if es_entrada else m["origen"],
                "TX Hash":          m["tx_hash"],
                "Notas":            nota,
            })

    # ── 2. Dividendos ─────────────────────────────────────────────────────────
    for ev in (dividends if dividends else []):
        rows.append({
            "Fecha UTC":        ev["fecha_str"],
            "Año fiscal":       ev["fecha_str"][:4],
            "Categoría":        "Dividendo",
            "Tipo operación":   "Dividendo recibido",
            "Activo":           ev.get("sym", "USDT"),
            "Cantidad":         round(ev["total"], 4),
            "Precio unit. USD": 1.0,
            "Valor USD":        round(ev["total"], 2),
            "Fuente precio":    "TX exacta",
            "Contraparte":      "",
            "TX Hash":          ev.get("tx_link", "").replace("https://polygonscan.com/tx/", ""),
            "Notas":            f"{ev['n_proyectos']} proyecto(s) en este pago.",
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
        })

    # ── 5. Aave ───────────────────────────────────────────────────────────────
    for m in (aave_lending if aave_lending else []):
        signo   = -1.0 if m["tipo"] == "Depósito préstamo" else 1.0
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
        })

    # Ordenar todo cronológicamente
    rows.sort(key=lambda r: r["Fecha UTC"])
    return pd.DataFrame(rows).to_csv(index=False).encode("utf-8")

with st.spinner("Preparando informe fiscal…"):
    csv_fiscal = build_fiscal_csv()

st.download_button(
    "⬇️ Descargar informe fiscal completo (CSV)",
    data=csv_fiscal,
    file_name=f"reental_informe_fiscal_{wallet[:8]}.csv",
    mime="text/csv",
    type="primary",
    use_container_width=True,
)
