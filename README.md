# 📈 DLR Futures Screener — Matba Rofex / Primary API

> Real-time valuation screener for USD futures (DLR) traded on Matba Rofex, based on covered interest rate parity.

---

## 🇬🇧 English

### What it does

This tool connects to the **Primary WebSocket API** (Matba Rofex) and continuously prices all active DLR futures contracts using the **covered interest rate parity** formula:

```
F* = S × (1 + r_ARS × t/365) / (1 + r_USD × t/365)
```

For each active contract it calculates:

| Metric | Description |
|---|---|
| **Theoretical price** | Fair value under interest rate parity |
| **Basis** | Market close minus theoretical price |
| **Basis %** | Relative mispricing vs spot |
| **Implied ARS rate** | TNA implied by the market price |
| **Roll / Pase TNA** | Annualized rate between consecutive maturities |
| **Signal** | `BUY` / `SELL` if `|basis%| > threshold` |

### Data sources

- **Spot rate (A3500):** BCRA official API (Serie 272) with fallback to `estadisticasbcra.com`
- **ARS rate curve:** Live LECAP YTM bootstrapped from market prices (13 instruments), with caución fallback
- **USD rate:** Configurable (default: 4.3% TNA)
- **Futures prices:** Primary WebSocket — real-time LAST / BID / OFFER

### Architecture

```
BCRA API ──────────────────────────────┐
                                        ▼
Primary WebSocket → market_data_handler → Valuation Engine → Console + CSV + Dashboard
     ↑                                        ↑
LECAP prices ────── ARS curve (YTM) ──────────┘
```

- **WebSocket** with auto-reconnect (configurable attempts and timeout)
- **Thread-safe** market data store with staleness detection
- **Optional matplotlib dashboard** (real-time bar chart + price curve)
- **CSV export** on each refresh cycle

### Requirements

```bash
pip install pyRofex requests python-dotenv matplotlib
```

### Configuration (`.env`)

```
PRIMARY_USERNAME=your_user
PRIMARY_PASSWORD=your_password
PRIMARY_ACCOUNT=your_account
BCRA_ESTADISTICAS_TOKEN=optional_token   # for BCRA fallback
```

### Usage

```bash
python screener_futuros_dlr.py
```

The script prompts for:
- USD TNA (default 4.3%)
- ARS TNA fallback (default 30%)
- Manual spot override (optional — fetched from BCRA if blank)

### Output sample

```
══════════════════════════════════════════════════════════════════════════════
  Ticker        Vto    Días      Close    Teórico    Basis  Basis%   TNA Impl
══════════════════════════════════════════════════════════════════════════════
  DLR/MAR26   MAR26      28   1,085.00   1,083.20    +1.80  +0.17%     31.2%
  DLR/ABR26   ABR26      59   1,110.00   1,108.50    +1.50  +0.14%     30.8%
  DLR/MAY26   MAY26      90   1,138.00   1,142.10    -4.10  -0.36% ◀SELL
══════════════════════════════════════════════════════════════════════════════
```

### Skills demonstrated

- Interest rate parity modeling on live market data
- WebSocket streaming with reconnection logic
- ARS yield curve construction from LECAP instruments (YTM bootstrapping)
- Real-time signal generation with configurable thresholds
- Multi-source data ingestion (BCRA API + Primary WS)

---

## 🇦🇷 Español

### Qué hace

Este screener se conecta al **WebSocket de Primary API** (Matba Rofex) y valúa en tiempo real todos los contratos activos de futuros DLR usando la **paridad cubierta de tasas de interés**:

```
F* = S × (1 + r_ARS × t/365) / (1 + r_USD × t/365)
```

Para cada contrato calcula:

| Métrica | Descripción |
|---|---|
| **Precio teórico** | Valor justo según paridad de tasas |
| **Basis** | Precio de mercado menos teórico |
| **Basis %** | Desvío relativo respecto al spot |
| **TNA implícita ARS** | Tasa que implica el precio de mercado |
| **Pase TNA** | Tasa anualizada entre vencimientos consecutivos |
| **Señal** | `COMPRAR` / `VENDER` si `|basis%| > umbral` |

### Fuentes de datos

- **Spot A3500:** API oficial del BCRA (Serie 272) con fallback a `estadisticasbcra.com`
- **Curva ARS:** YTM de LECAP construida desde precios de mercado (13 instrumentos), con fallback a tasas de caución
- **Tasa USD:** Configurable (default: 4.3% TNA)
- **Precios de futuros:** WebSocket de Primary — LAST / BID / OFFER en tiempo real

### Arquitectura

```
API BCRA ──────────────────────────────┐
                                        ▼
WebSocket Primary → market_data_handler → Motor de valuación → Consola + CSV + Dashboard
     ↑                                           ↑
Precios LECAP ─── Curva ARS (YTM) ──────────────┘
```

- **WebSocket** con reconexión automática (intentos y timeout configurables)
- **Almacenamiento thread-safe** con detección de datos vencidos
- **Dashboard opcional con matplotlib** (gráfico de barras basis + curva de precios)
- **Exportación a CSV** en cada ciclo de actualización

### Requisitos

```bash
pip install pyRofex requests python-dotenv matplotlib
```

### Configuración (`.env`)

```
PRIMARY_USERNAME=tu_usuario
PRIMARY_PASSWORD=tu_contraseña
PRIMARY_ACCOUNT=tu_cuenta
BCRA_ESTADISTICAS_TOKEN=token_opcional   # para el fallback del BCRA
```

### Uso

```bash
python screener_futuros_dlr.py
```

El script solicita al iniciar:
- TNA USD (default 4.3%)
- TNA ARS fallback (default 30%)
- Spot manual (opcional — se obtiene del BCRA si se deja vacío)

### Skills que demuestra

- Modelado de paridad de tasas sobre datos de mercado en tiempo real
- Streaming WebSocket con lógica de reconexión
- Construcción de curva de rendimientos ARS desde LECAPs (bootstrapping de YTM)
- Generación de señales en tiempo real con umbrales configurables
- Ingesta de datos multi-fuente (API BCRA + WebSocket Primary)

---

## Author

**unabomber1618** · [github.com/unabomber1618](https://github.com/unabomber1618)

> *Built for live trading on Argentine fixed income and FX futures markets.*
