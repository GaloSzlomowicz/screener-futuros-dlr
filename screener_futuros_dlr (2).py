# -*- coding: utf-8 -*-
# =============================================================================
# SCREENER - Valuación de Futuros DLR (Primary API / Matba Rofex)
# =============================================================================
# Lógica: Para cada contrato DLR activo calcula:
#   - Teórico: F* = S × (1 + r_ars × t/365) / (1 + r_usd × t/365)
#   - Basis: close − teórico
#   - TNA implícita ARS del precio de mercado
#   - Pase implícito TNA entre vencimientos consecutivos
#   - Signal: COMPRAR/VENDER si |basis_pct| > umbral configurable
#
# Conexión: mismo patrón que CIvs24HS.py (Primary WebSocket + REST fallback)
# Tasas ARS: YTM de LECAP (curva pesos) matcheada por plazo
#
# Opcional: matplotlib para dashboard gráfico (pip install matplotlib)
# =============================================================================

import os
import sys
import logging
import time
import threading
import requests
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv
import pyRofex

# =============================================================================
# LOGGING Y .ENV — mismo patrón que CIvs24HS.py
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('screener_futuros_dlr.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

try:
    script_dir = os.path.dirname(os.path.abspath(__file__))
except NameError:
    script_dir = os.getcwd()

downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
env_paths = [
    os.path.join(downloads_dir, '.env'),
    os.path.join(downloads_dir, '.envGGAL'),
    os.path.join(script_dir, '.env'),
    os.path.join(os.path.dirname(script_dir), '.env'),
    os.path.join(os.getcwd(), '.env'),
]
env_loaded = False
for env_path in env_paths:
    if os.path.exists(env_path):
        env_loaded = load_dotenv(env_path, override=True)
        if env_loaded:
            print(f"[OK] .env cargado desde: {env_path}")
            break
if not env_loaded:
    load_dotenv()


# =============================================================================
# HELPERS
# =============================================================================

def _short_label(ticker: str) -> str:
    if not ticker:
        return ''
    s = ticker.strip()
    if s.upper().startswith('MERV - XMEV - '):
        return s[14:].strip()
    return s




# =============================================================================
# SPOT A3500 — API BCRA (serie 272)
# =============================================================================
# Publica valor del día hábil. No es intraday — refresh cada 5 min es suficiente.

BCRA_A3500_URL = "https://api.bcra.gob.ar/estadisticas/v3.0/Monetarias/272"
# Fallback: API alternativa (serie 272 = Tipo de Cambio Referencia Com. A 3500)
BCRA_A3500_FALLBACK_URL = "https://api.estadisticasbcra.com/usd_of"
BCRA_TIMEOUT   = 10
BCRA_REFRESH   = 300  # segundos

def fetch_spot_bcra() -> Optional[float]:
    """Dólar mayorista A3500 desde la API del BCRA. Serie 272. Fallback a estadisticasbcra.com."""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    headers = {"Accept": "application/json", "User-Agent": "ScreenerFuturosDLR/1.0"}

    # Intento 1: BCRA v3 Monetarias 272
    try:
        resp = requests.get(
            BCRA_A3500_URL,
            timeout=BCRA_TIMEOUT,
            verify=False,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results") or data.get("data") or []
        if not results:
            return None
        last = results[-1]
        val = last.get("valor") or last.get("value") or last.get("v")
        if val is not None:
            return float(val)
    except Exception as e:
        logger.debug(f"[BCRA] v3 272: {e}")

    # Intento 2: estadisticasbcra.com (requiere token; evita 403)
    token = os.getenv("BCRA_ESTADISTICAS_TOKEN") or os.getenv("BCRA_TOKEN")
    if token:
        try:
            h = dict(headers)
            h["Authorization"] = f"BEARER {token.strip()}"
            resp = requests.get(
                BCRA_A3500_FALLBACK_URL,
                timeout=BCRA_TIMEOUT,
                verify=False,
                headers=h,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                last = data[-1]
                val = last.get("v") if isinstance(last, dict) else None
                if val is not None:
                    return float(val)
        except Exception as e:
            logger.warning(f"[BCRA] Fallback estadisticasbcra: {e}")
    return None

def _parse_md(message: Dict) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Extrae (last, bid, offer) del mensaje WebSocket de Primary.
    Patrón exacto del archivo homologado:
      - message.get('marketData', {})  ← payload dentro de 'marketData'
      - BI / OF / LA son arrays/dicts dentro de marketData
    """
    md = message.get('marketData', {})
    if not md:
        return None, None, None

    # BID
    bids = md.get('BI', [])
    bid_px = None
    if isinstance(bids, list) and bids and isinstance(bids[0], dict):
        bp = bids[0].get('price')
        if isinstance(bp, (int, float)):
            bid_px = float(bp)

    # OFFER
    offers = md.get('OF', [])
    offer_px = None
    if isinstance(offers, list) and offers and isinstance(offers[0], dict):
        op = offers[0].get('price')
        if isinstance(op, (int, float)):
            offer_px = float(op)

    # LAST
    last_obj = md.get('LA')
    last_px = None
    if isinstance(last_obj, dict):
        lp = last_obj.get('price')
        if isinstance(lp, (int, float)):
            last_px = float(lp)

    return last_px, bid_px, offer_px


# =============================================================================
# VALUACIÓN
# =============================================================================

def _teorico(spot: float, r_ars: float, r_usd: float, dias: int) -> float:
    """F* = S × (1 + r_ars × t/365) / (1 + r_usd × t/365)"""
    t = dias / 365.0
    return spot * (1.0 + r_ars * t) / (1.0 + r_usd * t)


def _tna_implicita(spot: float, close: float, r_usd: float, dias: int) -> float:
    """Despeja r_ars de la paridad de tasas."""
    t = dias / 365.0
    return (close / spot * (1.0 + r_usd * t) - 1.0) / t


def _pase_tna(px_near: float, px_far: float, dias_diff: int) -> Optional[float]:
    if dias_diff <= 0 or px_near <= 0:
        return None
    return (px_far / px_near - 1.0) * (365.0 / dias_diff)


# =============================================================================
# CLASE PRINCIPAL
# =============================================================================

class ScreenerFuturosDLR:
    """
    Screener de valuación de futuros DLR usando paridad de tasas.
    Conexión Primary WebSocket — mismo patrón que CIvs24HS.py.
    """

    # LECAP tickers para curva de tasas ARS
    # Mismo set que usa el Excel (hoja "curva pesos") — son instrumentos BYMA
    # Formato Primary BYMA: "MERV - XMEV - TICKER - 48hs" o similar
    # Se suscriben para obtener precio de mercado y calcular YTM por plazo
    LECAP_INSTRUMENTS = [
        # (ticker_primary, vencimiento, coupon_TEM)
        # Datos de la hoja curva pesos del Excel
        ('S30J5', '20250630', 0.039),
        ('S31L5', '20250731', 0.0398),
        ('S15G5', '20250815', 0.039),
        ('S29G5', '20250829', 0.0388),
        ('S12S5', '20250912', 0.0395),
        ('S30S5', '20250930', 0.0398),
        ('S28N5', '20251128', 0.0226),
        ('T17O5', '20251017', 0.039),
        ('T15D5', '20251215', 0.0389),
        ('T30E6', '20260130', 0.0265),
        ('T13F6', '20260213', 0.0260),
        ('T30J6', '20260630', 0.0215),
        ('T15E7', '20270115', 0.0205),
    ]
    # Plazos de caución como fallback si no hay LECAP prices
    CAUCION_PLAZOS = [1, 7, 14, 30, 60, 90, 120, 180, 270, 365]

    def __init__(self, config: Dict):
        self.config = config

        self.futuros_tickers: List[str] = config.get('futuros_tickers', [
            'DLR/MAR26', 'DLR/ABR26', 'DLR/MAY26', 'DLR/JUN26',
            'DLR/JUL26', 'DLR/AGO26', 'DLR/SEP26', 'DLR/OCT26',
            'DLR/NOV26', 'DLR/DIC26',
        ])
        self.r_usd: float              = float(config.get('r_usd', 0.043))
        self.r_ars_fallback: float     = float(config.get('r_ars_fallback', 0.30))
        self.basis_signal_pct: float   = float(config.get('basis_signal_pct', 0.5))
        self.display_interval: float   = float(config.get('display_interval_seconds', 5.0))
        self.max_data_age: float       = float(config.get('max_data_age_seconds', 20.0))
        self.ws_timeout: float         = float(config.get('websocket_timeout_seconds', 90.0))
        self.max_reconnect: int        = int(config.get('max_reconnect_attempts', 5))
        self.log_detail: bool          = config.get('log_ultra_detallado', False)
        self.export_csv: bool          = config.get('export_csv', False)
        self.use_dashboard: bool       = config.get('use_dashboard', True)

        # Estado interno
        self.running              = False
        self.ws_connected         = False
        self._sub_sent            = False
        self.market_data: Dict    = {}     # ticker → {last, bid, offer, timestamp}
        self.lecap_prices: Dict   = {}     # ticker → last_price (para calcular YTM)
        self.ars_curve: Dict      = {}     # plazo_dias → tna_decimal (curva interpolada)
        self.spot_price           = None
        self.vencimientos: Dict   = {}     # ticker → datetime
        self._account_id          = None
        self._last_display        = 0.0
        self._reconnect_attempts  = 0
        self.last_md_time         = None
        self._symbols_logged: set = set()
        self._last_rows: List[Tuple]  = []   # para dashboard
        self._fig = None
        self._ax_basis = None
        self._ax_precio = None

    # ─────────────────────────────────────────────────────────────────────────
    # INICIALIZACIÓN — pipeline idéntico a CIvs24HS.py
    # ─────────────────────────────────────────────────────────────────────────

    def initialize(self) -> bool:
        def clean(v):
            if not v:
                return None
            v = str(v).strip().strip('{}"\' ')
            return v or None

        user    = clean(os.getenv('PRIMARY_USERNAME') or os.getenv('PRIMARY_USER'))
        passwd  = clean(os.getenv('PRIMARY_PASSWORD') or os.getenv('PRIMARY_PASS'))
        account = clean(os.getenv('PRIMARY_ACCOUNT')  or os.getenv('PRIMARY_ACC'))
        if not user or not passwd:
            logger.error("PRIMARY_USERNAME y PRIMARY_PASSWORD requeridos en .env")
            return False
        account = account or user

        api_url = clean(os.getenv('PRIMARY_API_URL') or os.getenv('MATRIZ_API_URL'))
        ws_url  = clean(os.getenv('PRIMARY_WS_URL')  or os.getenv('MATRIZ_WS_URL'))
        is_eco  = ('eco' in (user or '').lower() or
                   os.getenv('USE_ECO_URLS', '').lower() == 'true')
        if not api_url and not ws_url and is_eco:
            api_url = 'https://api.eco.xoms.com.ar/'
            ws_url  = 'wss://api.eco.xoms.com.ar/'

        logger.info("=" * 60)
        logger.info("  SCREENER FUTUROS DLR — Primary API / Matba Rofex")
        logger.info("=" * 60)
        logger.info("[PIPELINE] URLs → Auth → WebSocket → Suscripciones")

        if api_url:
            try:
                pyRofex._set_environment_parameter('url', api_url, pyRofex.Environment.LIVE)
                logger.info(f"[CONFIG] API URL: {api_url}")
            except Exception:
                pass
        if ws_url:
            try:
                pyRofex._set_environment_parameter('ws', ws_url, pyRofex.Environment.LIVE)
                logger.info(f"[CONFIG] WS URL: {ws_url}")
            except Exception:
                pass
        if not api_url and not ws_url:
            logger.info("[CONFIG] Usando URLs por defecto de pyRofex (LIVE)")

        logger.info(f"[AUTH] Usuario: {user} | Account: {account}")
        try:
            pyRofex.initialize(user=user, password=passwd,
                               account=account, environment=pyRofex.Environment.LIVE)
        except Exception as e1:
            try:
                pyRofex.initialize(user=user, password=passwd,
                                   environment=pyRofex.Environment.LIVE)
                account = None
            except Exception as e2:
                logger.error(f"Auth falló: {e1}; {e2}")
                return False
        self._account_id = account or user
        logger.info(f"[OK] Autenticado | Ambiente: LIVE (PRODUCCIÓN)")

        logger.info("[PIPELINE] Obteniendo vencimientos vía REST...")
        self._fetch_vencimientos()

        logger.info("[PIPELINE] Conectando WebSocket...")
        try:
            pyRofex.init_websocket_connection(
                market_data_handler=self._md_handler,
                order_report_handler=lambda m: None,
                error_handler=self._error_handler,
                exception_handler=self._exception_handler,
            )
        except Exception as e:
            logger.error(f"WebSocket falló: {e}")
            return False
        self.running = True
        self.ws_connected = True
        logger.info("[OK] WebSocket inicializado")
        time.sleep(2)

        all_tickers = self._build_tickers()
        logger.info(f"[SUBSCRIPTION] Suscribiendo {len(all_tickers)} tickers | LAST + BIDS + OFFERS")
        for t in all_tickers:
            logger.info(f"[SUBSCRIPTION]   - {_short_label(t)}")
        try:
            pyRofex.market_data_subscription(
                tickers=all_tickers,
                entries=[pyRofex.MarketDataEntry.LAST,
                         pyRofex.MarketDataEntry.BIDS,
                         pyRofex.MarketDataEntry.OFFERS],
                depth=1,
            )
            self._sub_sent = True
            logger.info("[OK] Suscripción enviada (sin market; compatible Eco/Primary)")
        except Exception as e:
            logger.warning(f"[SUBSCRIPTION] {e}")

        time.sleep(1)
        logger.info("=" * 60)
        logger.info("[OK] PIPELINE COMPLETADO — Screener activo")
        logger.info(f"[CONFIG] r_usd={self.r_usd:.2%} | signal |basis|>{self.basis_signal_pct:.1f}%"
                    f" | display cada {self.display_interval:.0f}s")
        # Fetch spot inicial desde BCRA
        spot = fetch_spot_bcra()
        if spot:
            self.spot_price = spot
            logger.info(f"[BCRA] Spot A3500 inicial: {spot:,.2f}")
        else:
            logger.warning("[BCRA] No se pudo obtener spot inicial — se reintentará en el loop")
        logger.info("=" * 60)
        return True

    def _build_tickers(self) -> List[str]:
        t = list(self.futuros_tickers)
        # Agregar LECAP para curva de tasas ARS
        # Primary usa el ticker directo (sin prefijo MERV) para estos instrumentos
        for ticker, _, _ in self.LECAP_INSTRUMENTS:
            t.append(ticker)
        return list(dict.fromkeys(t))

    def _fetch_vencimientos(self):
        try:
            resp = pyRofex.get_all_instruments()
            available_dlr = []
            for inst in resp.get('instruments', []):
                sym = (inst.get('instrumentId') or {}).get('symbol', '')
                if 'DLR/' not in sym:
                    continue
                try:
                    vto = datetime.strptime(inst.get('maturityDate', ''), '%Y%m%d')
                    self.vencimientos[sym] = vto
                    if vto.date() >= datetime.now().date():
                        available_dlr.append(sym)
                except ValueError:
                    pass
            # Suscribir solo futuros DLR que existan y no hayan vencido (evita "Product don't exist")
            if available_dlr:
                self.futuros_tickers = [t for t in self.futuros_tickers if t in self.vencimientos and self.vencimientos[t].date() >= datetime.now().date()]
                if not self.futuros_tickers:
                    self.futuros_tickers = sorted(available_dlr, key=lambda s: self.vencimientos[s])
            logger.info(f"[INSTRUMENTOS] {len(self.vencimientos)} futuros DLR | activos (vto>=hoy): {len(self.futuros_tickers)}")
        except Exception as e:
            logger.warning(f"[INSTRUMENTOS] {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # HANDLERS WebSocket — patrón CIvs24HS.py
    # ─────────────────────────────────────────────────────────────────────────

    def _md_handler(self, message):
        try:
            # Patrón homologado: type == 'Md' (M mayúscula, d minúscula)
            if message.get('type') != 'Md':
                return
            # Patrón homologado: instrumentId.symbol
            symbol = message.get('instrumentId', {}).get('symbol', '')
            if not symbol:
                return

            if symbol not in self._symbols_logged:
                self._symbols_logged.add(symbol)
                logger.info(f"[MD] Recibiendo datos: {_short_label(symbol)}")

            last_px, bid_px, offer_px = _parse_md(message)
            now = time.time()
            existing = self.market_data.get(symbol, {})
            self.market_data[symbol] = {
                'last':      last_px  if last_px  is not None else existing.get('last'),
                'bid':       bid_px   if bid_px   is not None else existing.get('bid'),
                'offer':     offer_px if offer_px is not None else existing.get('offer'),
                'timestamp': now,
            }

            # LECAP — capturar precio para calcular YTM por plazo
            lecap_tickers = {t for t, _, _ in self.LECAP_INSTRUMENTS}
            if symbol in lecap_tickers and last_px is not None and last_px > 0:
                self.lecap_prices[symbol] = last_px
                # Recalcular curva ARS con todos los precios disponibles
                self._recalculate_ars_curve()
                if self.log_detail:
                    logger.info(f"[LECAP] {symbol} precio={last_px:.4f}")

            self.last_md_time = now

            if self._sub_sent and now - self._last_display >= self.display_interval:
                self._last_display = now
                self._display()

        except Exception as e:
            logger.error(f"[MD HANDLER] {e}")

    def _error_handler(self, message):
        logger.warning(f"[WS ERROR] {message}")
        if any(k in str(message).lower() for k in ('connection', 'timeout', 'disconnected', 'closed', 'broken')):
            self.ws_connected = False
            self._reconnect()

    def _exception_handler(self, e):
        logger.error(f"[WS EXCEPTION] {e}")
        if any(k in str(e).lower() for k in ('connection', 'timeout', 'broken', 'io', 'socket')):
            self.ws_connected = False
            self._reconnect()

    def _reconnect(self, delay: float = 5.0):
        if self._reconnect_attempts >= self.max_reconnect:
            logger.error(f"[WS] Máximo de reconexiones ({self.max_reconnect}) alcanzado")
            return
        self._reconnect_attempts += 1
        logger.info(f"[WS] Reconectando (intento {self._reconnect_attempts}/{self.max_reconnect})...")
        try:
            time.sleep(delay)
            try:
                pyRofex.close_websocket_connection()
                time.sleep(1)
            except Exception:
                pass
            pyRofex.init_websocket_connection(
                market_data_handler=self._md_handler,
                order_report_handler=lambda m: None,
                error_handler=self._error_handler,
                exception_handler=self._exception_handler,
            )
            time.sleep(2)
            pyRofex.market_data_subscription(
                tickers=self._build_tickers(),
                entries=[pyRofex.MarketDataEntry.LAST,
                         pyRofex.MarketDataEntry.BIDS,
                         pyRofex.MarketDataEntry.OFFERS],
                depth=1,
            )
            self._sub_sent = True
            self.ws_connected = True
            self.last_md_time = time.time()
            self._reconnect_attempts = 0
            for t in self.futuros_tickers:
                if t in self.market_data:
                    self.market_data[t]['timestamp'] = 0
            logger.info("[WS] Reconectado y re-suscrito. Datos invalidados.")
        except Exception as e:
            logger.error(f"[WS] Error reconectando: {e}")

    def _check_ws_health(self):
        if not self.ws_connected or not self._sub_sent:
            return
        if self.last_md_time and time.time() - self.last_md_time > self.ws_timeout:
            logger.warning(f"[WS] Sin MD por {self.ws_timeout:.0f}s — reconectando...")
            self.ws_connected = False
            self._reconnect()

    # ─────────────────────────────────────────────────────────────────────────
    # TASAS — curva ARS desde LECAP (YTM/cupón por plazo)
    # ─────────────────────────────────────────────────────────────────────────

    def _recalculate_ars_curve(self):
        """Construye ars_curve (plazo_dias -> TNA) desde precios LECAP y cupón TEM."""
        today = datetime.now().date()
        self.ars_curve.clear()
        for ticker, vto_str, coupon_tem in self.LECAP_INSTRUMENTS:
            if ticker not in self.lecap_prices or self.lecap_prices[ticker] <= 0:
                continue
            try:
                vto = datetime.strptime(vto_str, '%Y%m%d').date()
                dias = max(1, (vto - today).days)
                # TNA aproximada desde TEM: (1+TEM)^12 - 1
                tna = (1.0 + float(coupon_tem)) ** 12 - 1.0
                self.ars_curve[dias] = tna
            except (ValueError, TypeError):
                continue

    def _get_r_ars(self, dias: int) -> float:
        if not self.ars_curve:
            return self.r_ars_fallback
        plazos = sorted(self.ars_curve.keys())
        tasas  = [self.ars_curve[p] for p in plazos]
        if dias <= plazos[0]:
            return tasas[0]
        if dias >= plazos[-1]:
            return tasas[-1]
        for i in range(len(plazos) - 1):
            if plazos[i] <= dias <= plazos[i + 1]:
                w = (dias - plazos[i]) / (plazos[i + 1] - plazos[i])
                return tasas[i] + w * (tasas[i + 1] - tasas[i])
        return tasas[-1]

    # ─────────────────────────────────────────────────────────────────────────
    # DISPLAY
    # ─────────────────────────────────────────────────────────────────────────

    def _display(self):
        spot = self.spot_price
        if spot is None:
            logger.info("[SCREENER] Esperando spot I.DLR...")
            return

        now    = time.time()
        ts     = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        sep    = "─" * 125
        rows   = []

        for i, ticker in enumerate(self.futuros_tickers):
            md = self.market_data.get(ticker)
            if not md or now - md.get('timestamp', 0) > self.max_data_age:
                continue

            close = md.get('last') or md.get('bid')
            if not close or close <= 0 or close > 50_000:  # validación datos sucios
                continue

            vto = self.vencimientos.get(ticker)
            if not vto:
                continue
            dias    = max(1, (vto - datetime.now()).days)
            vto_str = vto.strftime('%d/%m/%y')

            r_ars   = self._get_r_ars(dias)
            teo     = _teorico(spot, r_ars, self.r_usd, dias)
            basis   = close - teo
            bpct    = basis / spot * 100.0
            tna_imp = _tna_implicita(spot, close, self.r_usd, dias)

            # Pase con contrato siguiente
            pase = None
            if i + 1 < len(self.futuros_tickers):
                nxt     = self.futuros_tickers[i + 1]
                nxt_md  = self.market_data.get(nxt)
                nxt_vto = self.vencimientos.get(nxt)
                if nxt_md and nxt_vto:
                    nxt_close = nxt_md.get('last') or nxt_md.get('bid')
                    nxt_dias  = max(1, (nxt_vto - datetime.now()).days)
                    if nxt_close and nxt_dias > dias:
                        pase = _pase_tna(close, nxt_close, nxt_dias - dias)

            signal = ''
            if abs(bpct) > self.basis_signal_pct:
                signal = '◀ COMPRAR' if bpct < 0 else '▶ VENDER '

            rows.append((ticker, vto_str, dias, close, teo,
                         basis, bpct, tna_imp, pase, r_ars, signal))

        # Print
        print(f"\n{sep}")
        print(f"  SCREENER FUTUROS DLR  │  Spot: {spot:>10,.2f}  │  {ts}"
              f"  │  r_usd={self.r_usd:.2%}  │  curva_ars={len(self.ars_curve)} plazos")
        print(sep)
        print(f"  {'Ticker':<13} {'Vto':>8} {'Días':>5} "
              f"{'Close':>10} {'Teórico':>10} {'Basis':>8} {'Basis%':>8} "
              f"{'TNA Impl':>10} {'Pase TNA':>10} {'r_ars':>8}  Signal")
        print(sep)

        for (tkr, vto_s, dias, close, teo,
             basis, bpct, tna_imp, pase, r_ars, signal) in rows:
            sign  = '+' if basis >= 0 else ''
            pase_s = f"{pase:.2%}" if pase is not None else '     —  '
            print(f"  {tkr:<13} {vto_s:>8} {dias:>5} "
                  f"{close:>10,.2f} {teo:>10,.2f} "
                  f"{sign}{basis:>7,.2f} {sign}{bpct:>6.2f}% "
                  f"{tna_imp:>10.2%} {pase_s:>10} "
                  f"{r_ars:>7.2%}  {signal}")

        fuente = f"LECAP {len(self.ars_curve)} puntos" if self.ars_curve else f"fallback {self.r_ars_fallback:.0%}"
        print(sep)
        print(f"  r_ars: LECAP YTM {fuente}  │  señal si |basis%|>{self.basis_signal_pct:.1f}%"
              f"  │  datos frescos <{self.max_data_age:.0f}s")
        print(sep)

        if self.export_csv and rows:
            self._csv(rows, ts)

        # Guardar para dashboard
        self._last_rows = rows

    def _csv(self, rows, ts):
        fname = f"screener_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        try:
            with open(fname, 'w') as f:
                f.write("ticker,vto,dias,close,teorico,basis,basis_pct,tna_implicita,pase_tna,r_ars,signal\n")
                for r in rows:
                    tkr,vto_s,dias,close,teo,basis,bpct,tna_imp,pase,r_ars,signal = r
                    f.write(f"{tkr},{vto_s},{dias},{close:.4f},{teo:.4f},"
                            f"{basis:.4f},{bpct:.4f},{tna_imp:.4f},"
                            f"{pase:.4f if pase else ''},"
                            f"{r_ars:.4f},{signal.strip()}\n")
            logger.info(f"[CSV] {fname}")
        except Exception as e:
            logger.warning(f"[CSV] {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # DASHBOARD (matplotlib)
    # ─────────────────────────────────────────────────────────────────────────

    def _update_dashboard(self, rows: List[Tuple]) -> None:
        """Actualiza el gráfico con la última data. Llamar desde el thread principal."""
        if not rows:
            return
        try:
            import matplotlib
            matplotlib.use('TkAgg')
            import matplotlib.pyplot as plt
        except Exception:
            return
        try:
            if self._fig is None:
                self._fig, (self._ax_basis, self._ax_precio) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
                self._fig.suptitle('Screener Futuros DLR — Paridad de tasas', fontsize=12)
                plt.ion()
                plt.show(block=False)
            tickers = [r[0].replace('DLR/', '') for r in rows]
            basis_pct = [r[6] for r in rows]
            close = [r[3] for r in rows]
            teo = [r[4] for r in rows]
            colors = ['#2ecc71' if b < 0 else '#e74c3c' for b in basis_pct]

            self._ax_basis.clear()
            self._ax_basis.bar(tickers, basis_pct, color=colors, edgecolor='#333')
            self._ax_basis.axhline(0, color='#333', linewidth=0.8)
            self._ax_basis.axhline(self.basis_signal_pct, color='orange', linestyle='--', alpha=0.7, label=f'Señal ±{self.basis_signal_pct}%')
            self._ax_basis.axhline(-self.basis_signal_pct, color='orange', linestyle='--', alpha=0.7)
            self._ax_basis.set_ylabel('Basis %')
            self._ax_basis.set_title('Basis % vs spot (verde=comprar, rojo=vender)')
            self._ax_basis.legend(loc='upper right')
            self._ax_basis.tick_params(axis='x', rotation=45)

            self._ax_precio.clear()
            x = range(len(tickers))
            self._ax_precio.plot(x, close, 'o-', color='#3498db', linewidth=2, markersize=8, label='Close')
            self._ax_precio.plot(x, teo, 's--', color='#9b59b6', linewidth=1.5, markersize=6, label='Teórico')
            self._ax_precio.set_xticks(x)
            self._ax_precio.set_xticklabels(tickers, rotation=45)
            self._ax_precio.set_ylabel('Precio')
            self._ax_precio.set_title('Close vs Teórico')
            self._ax_precio.legend(loc='upper right')
            self._ax_precio.grid(True, alpha=0.3)

            self._fig.tight_layout()
            self._fig.canvas.draw_idle()
            self._fig.canvas.flush_events()
        except Exception as e:
            logger.debug(f"[DASHBOARD] {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # RUN LOOP — patrón CIvs24HS.py
    # ─────────────────────────────────────────────────────────────────────────

    def run(self, duration_seconds: Optional[float] = None):
        logger.info("[RUN] Screener corriendo (Ctrl+C para detener)")
        last_health = time.time()
        last_bcra   = 0.0
        last_plot   = 0.0
        deadline = time.time() + duration_seconds if duration_seconds else None
        try:
            while self.running:
                if deadline and time.time() >= deadline:
                    break
                time.sleep(1)
                now = time.time()
                # Dashboard: actualizar gráfico en el thread principal
                if self.use_dashboard and self._last_rows and (now - last_plot) >= self.display_interval:
                    self._update_dashboard(self._last_rows)
                    last_plot = now
                # Refresh spot BCRA cada BCRA_REFRESH segundos
                if now - last_bcra >= BCRA_REFRESH:
                    spot = fetch_spot_bcra()
                    if spot:
                        self.spot_price = spot
                        logger.info(f"[BCRA] Spot A3500 actualizado: {spot:,.2f}")
                    last_bcra = now
                if now - last_health >= 30:
                    self._check_ws_health()
                    last_health = now
        except KeyboardInterrupt:
            pass
        self.running = False
        logger.info("[STOP] Screener detenido")


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("\n" + "=" * 60)
    print("  SCREENER FUTUROS DLR — Primary API / Matba Rofex")
    print("  F* = S*(1+r_ars*t/365) / (1+r_usd*t/365)")
    print("=" * 60)

    # ── Inputs del trader (TNA y spot) ───────────────────────────────────────
    def input_float(prompt: str, default: float, pct: bool = False) -> float:
        s = input(prompt).strip()
        if not s:
            return default
        try:
            v = float(s.replace(',', '.'))
            return v / 100.0 if pct and v > 1 else v
        except ValueError:
            return default

    print("\n  [Tasas y spot — Enter para usar valor por defecto]\n")
    r_usd = input_float("  TNA USD (ej. 4.3 para 4.3%%): ", 0.043, pct=True)
    r_ars = input_float("  TNA ARS fallback (ej. 30 para 30%%): ", 0.30, pct=True)
    spot_str = input("  Spot A3500 (vacío = intentar BCRA/API): ").strip()
    spot_manual = None
    if spot_str:
        try:
            spot_manual = float(spot_str.replace(',', '.'))
        except ValueError:
            pass

    config = {
        # ── Tickers (sin FEB26 para evitar "Product don't exist") ─────────────
        'futuros_tickers': [
            'DLR/MAR26', 'DLR/ABR26', 'DLR/MAY26', 'DLR/JUN26',
            'DLR/JUL26', 'DLR/AGO26', 'DLR/SEP26', 'DLR/OCT26',
            'DLR/NOV26', 'DLR/DIC26',
        ],
        # ── Tasas (ingresadas por el trader) ──────────────────────────────────
        'r_usd':                r_usd,
        'r_ars_fallback':       r_ars,

        # ── Señales ──────────────────────────────────────────────────────────
        'basis_signal_pct':     0.5,

        # ── Display ─────────────────────────────────────────────────────────
        'display_interval_seconds': 5.0,
        'export_csv':               False,
        'use_dashboard':            True,

        # ── Freshness ───────────────────────────────────────────────────────
        'max_data_age_seconds':     20.0,

        # ── WebSocket ───────────────────────────────────────────────────────
        'websocket_timeout_seconds': 90.0,
        'max_reconnect_attempts':     5,

        # ── Logging ─────────────────────────────────────────────────────────
        'log_ultra_detallado':      False,
    }

    screener = ScreenerFuturosDLR(config)
    if screener.initialize():
        if spot_manual is not None:
            screener.spot_price = spot_manual
            logger.info(f"[SPOT] Usando valor manual: {spot_manual:,.2f}")
        screener.run()
    else:
        print("[ERROR] No se pudo inicializar")


if __name__ == '__main__':
    main()
