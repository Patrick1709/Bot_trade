import pandas as pd
import ta
import time
import csv
import os
import asyncio
from datetime import datetime
from binance.client import Client
from binance.enums import *
from telegram import Bot
from data import API_KEY, API_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

# === НАСТРОЙКИ ===
TIMEFRAME = '1h'
LIMIT = 100
POSITION_UNIT = 100  # В USDT
STOP_LOSS_PCT = 0.015  # 2% СТОП ЛОСС
TAKE_PROFIT_PCT = 0.03  # 4% ТЕЙК ПРОФИТ
TRADES_FILE = 'trades.csv'
POSITIONS_FILE = 'positions.csv'
CLOSED_TRADES_FILE = 'closed_trades.csv'
LEVERAGE = 5  # Кредитное плечо
RISK_PER_TRADE = 0.01  # 1% от депозита на сделку
MAX_CONCURRENT_TRADES = 15  # Максимум 15 сделок одновременно
MIN_TRADE_SIZE = 1.2       # Минимальный размер позиции 1 USDT
TRAILING_STOP_CALLBACK = 1.5  # 1.5% откат от максимума
# Инициализация клиента для фьючерсов
client = Client(API_KEY, API_SECRET)
bot = Bot(token=TELEGRAM_TOKEN)
loop = asyncio.get_event_loop()

# Глобальные переменные
last_top_update = 0
top_symbols = []
last_reported_pnl = {}  # Будет хранить последние зафиксированные значения PnL
PNL_CHANGE_THRESHOLD = 5  # Порог изменения PnL для уведомления (в USDT)

# Глобальный словарь для хранения минимальных сумм
MIN_NOTIONAL_VALUES = {}

async def load_min_notional():
    """Загружает минимальные суммы для всех USDT-пар"""
    global MIN_NOTIONAL_VALUES
    try:
        symbols_info = client.futures_exchange_info()['symbols']
        for symbol_info in symbols_info:
            if symbol_info['symbol'].endswith('USDT'):
                min_notional = None
                for f in symbol_info['filters']:
                    if f['filterType'] == 'MIN_NOTIONAL':
                        min_notional = float(f['notional'])
                        break
                if min_notional:
                    MIN_NOTIONAL_VALUES[symbol_info['symbol']] = min_notional
        print("Минимальные объемы загружены:", MIN_NOTIONAL_VALUES)
    except Exception as e:
        print(f"Ошибка загрузки минимальных объемов: {e}")


async def send_telegram_message(text):
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
    except Exception as e:
        print(f"[Telegram Error] {e}")


async def get_top_30_futures_symbols():
    global last_top_update
    try:
        tickers = client.futures_ticker()
        symbols_info = client.futures_exchange_info()['symbols']
        allowed_symbols = {
            s['symbol']
            for s in symbols_info
            if s['contractType'] == 'PERPETUAL' and s['status'] == 'TRADING'
        }

        usdt_pairs = [
            t for t in tickers
            if t['symbol'].endswith('USDT')
            and t['symbol'] not in ['BTCUSDT', 'ETHUSDT']
            and t['symbol'] in allowed_symbols
        ]

        sorted_by_volume = sorted(usdt_pairs, key=lambda x: float(x['quoteVolume']), reverse=True)
        top_symbols = sorted_by_volume[:28]

        message = "\n📊 Топ 28 фьючерсов по объёму (без BTC/ETH):\n" + "\n".join([
            f"{i + 1}. {t['symbol']} | Объём: {float(t['quoteVolume']):,.0f}$ | Цена: {float(t['lastPrice']):.4f}"
            for i, t in enumerate(top_symbols)
        ])
        await send_telegram_message(message)
        last_top_update = time.time()
        return [t['symbol'] for t in top_symbols]

    except Exception as e:
        print(f"Ошибка при получении топ-30: {e}")
        return []



async def get_futures_balance():
    try:
        balance = next(a for a in client.futures_account_balance() if a['asset'] == 'USDT')
        return float(balance['balance'])
    except Exception as e:
        print(f"Ошибка получения баланса: {e}")
        return 0


def get_ohlcv(symbol):
    try:
        klines = client.futures_klines(symbol=symbol, interval=TIMEFRAME, limit=LIMIT)
        df = pd.DataFrame(klines, columns=[
            'time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'number_of_trades',
            'taker_buy_base', 'taker_buy_quote', 'ignore'
        ])
        df['close'] = df['close'].astype(float)
        return df
    except Exception as e:
        print(f"Ошибка при получении данных для {symbol}: {e}")
        return None

async def process_trade(symbol, signals):
    # Добавьте в начало функции:
    positions = client.futures_position_information()
    if any(p['symbol'] == symbol and float(p['positionAmt']) != 0 for p in positions):
        return

### Новый код!
def log_trade(symbol, position_type, entry_price, stop_loss, take_profit, position_size, signals):
    file_exists = os.path.isfile(TRADES_FILE)
    with open(TRADES_FILE, mode='a', newline='') as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(
                ['timestamp', 'symbol', 'type', 'entry', 'stop_loss', 'take_profit', 'position_size', 'signals'])
        writer.writerow([
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            symbol,
            position_type,
            entry_price,
            stop_loss,
            take_profit,
            position_size,
            "|".join(signals)
        ])

def log_closed_trade(symbol, entry_price, close_price, pnl):
    file_exists = os.path.isfile(CLOSED_TRADES_FILE)
    with open(CLOSED_TRADES_FILE, mode='a', newline='') as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(['timestamp', 'symbol', 'entry', 'exit', 'pnl'])
        writer.writerow([
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            symbol,
            entry_price,
            close_price,
            pnl
        ])


async def report_closed_trades():
    if not os.path.exists(CLOSED_TRADES_FILE) or os.path.getsize(CLOSED_TRADES_FILE) == 0:
        return

    df = pd.read_csv(CLOSED_TRADES_FILE)
    if df.empty:
        return

    total_pnl = df['pnl'].sum()
    last_hour = datetime.now() - pd.Timedelta(hours=6)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    recent_trades = df[df['timestamp'] > last_hour]

    msg = f"📈 Отчёт по сделкам за 6 часов:\nЗакрыто: {len(recent_trades)}\nОбщий PnL: {recent_trades['pnl'].sum():.2f} USDT"
    await send_telegram_message(msg)

    # Очищаем данные по закрытым позициям
    for symbol in recent_trades['symbol'].unique():
        if symbol in last_reported_pnl:
            del last_reported_pnl[symbol]  # Удаляем из отслеживания PnL

def analyze(df):
    df = df.copy()
    # Индикаторы
    df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
    df['ema9'] = ta.trend.EMAIndicator(df['close'], window=9).ema_indicator()
    df['ema21'] = ta.trend.EMAIndicator(df['close'], window=21).ema_indicator()
    bb = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
    df['bb_lower'] = bb.bollinger_lband()
    df['bb_upper'] = bb.bollinger_hband()
    df['bb_middle'] = bb.bollinger_mavg()
    return df


def get_signals(df):
    signals = []
    last_row = df.iloc[-1]
    prev_row = df.iloc[-2]

    # Лонг сигналы
    if last_row['rsi'] < 30:
        signals.append('RSI_LONG')
    if (prev_row['ema9'] < prev_row['ema21']) and (last_row['ema9'] > last_row['ema21']):
        signals.append('EMA_CROSS_LONG')
    if last_row['close'] < last_row['bb_lower']:
        signals.append('BB_LOWER_LONG')

    # Шорт сигналы
    if last_row['rsi'] > 70:
        signals.append('RSI_SHORT')
    if (prev_row['ema9'] > prev_row['ema21']) and (last_row['ema9'] < last_row['ema21']):
        signals.append('EMA_CROSS_SHORT')
    if last_row['close'] > last_row['bb_upper']:
        signals.append('BB_UPPER_SHORT')

    return signals


async def execute_trade(symbol, position_type, entry_price, stop_loss, take_profit, position_size_usdt):
    try:
        # Получаем информацию о символе
        symbol_info = next(s for s in client.futures_exchange_info()['symbols'] if s['symbol'] == symbol)
        price_precision = symbol_info['pricePrecision']
        qty_precision = symbol_info['quantityPrecision']
        min_qty = float(symbol_info['filters'][1]['minQty'])

        # Безопасно получаем minNotional
        min_notional = None
        for f in symbol_info['filters']:
            if f['filterType'] == 'MIN_NOTIONAL':
                min_notional = float(f['notional'])
                break

        if not min_notional:
            await send_telegram_message(f"⚠️ У пары {symbol} нет MIN_NOTIONAL, пропускаем.")
            return False

        # Рассчитываем количество контрактов
        qty = round(max(position_size_usdt, MIN_TRADE_SIZE) / entry_price, qty_precision)

        if qty < min_qty or qty * entry_price < min_notional * 1.01:
            await send_telegram_message(f"❌ Объём {qty} слишком мал для {symbol}.")
            return False

        # Устанавливаем плечо
        client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)

        # Открытие позиции
        client.futures_create_order(
            symbol=symbol,
            side=SIDE_BUY if position_type == 'LONG' else SIDE_SELL,
            type=ORDER_TYPE_MARKET,
            quantity=qty
        )

        # STOP-LOSS
        client.futures_create_order(
            symbol=symbol,
            side=SIDE_SELL if position_type == 'LONG' else SIDE_BUY,
            type=FUTURE_ORDER_TYPE_STOP_MARKET,
            stopPrice=round(stop_loss, price_precision),
            closePosition='true',
            priceProtect='true'
        )

        # TAKE-PROFIT
        client.futures_create_order(
            symbol=symbol,
            side=SIDE_SELL if position_type == 'LONG' else SIDE_BUY,
            type=FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
            stopPrice=round(take_profit, price_precision),
            closePosition='true',
            priceProtect='true'
        )

        # TRAILING STOP
        activation_price = entry_price * (1.01 if position_type == 'LONG' else 0.99)

        client.futures_create_order(
            symbol=symbol,
            side=SIDE_SELL if position_type == 'LONG' else SIDE_BUY,
            type='TRAILING_STOP_MARKET',
            callbackRate=TRAILING_STOP_CALLBACK,
            activationPrice=round(activation_price, price_precision),
            closePosition='true',
            priceProtect='true'
        )

        # Telegram уведомление
        msg = (f"✅ Открыта позиция {position_type} {symbol}\n"
               f"Объём: {qty} контрактов (~{qty * entry_price:.2f} USDT)\n"
               f"Цена входа: {entry_price:.4f}\n"
               f"SL: {stop_loss:.4f} | TP: {take_profit:.4f}\n"
               f"Трейлинг: {TRAILING_STOP_CALLBACK}%")
        await send_telegram_message(msg)

        # Запоминаем PnL
        last_reported_pnl[symbol] = 0.0
        return True

    except Exception as e:
        await send_telegram_message(f"❌ Ошибка при открытии позиции {symbol}: {e}")
        return False


async def process_trade(symbol, signals):
    df = get_ohlcv(symbol)
    if df is None:
        return

    # Определяем тип позиции
    position_type = 'LONG'
    if any(s in signals for s in ['RSI_SHORT', 'EMA_CROSS_SHORT', 'BB_UPPER_SHORT']):
        position_type = 'SHORT'

    # Расчет параметров сделки
    entry_price = df['close'].iloc[-1]

    if position_type == 'LONG':
        stop_loss = entry_price * (1 - STOP_LOSS_PCT)
        take_profit = entry_price * (1 + TAKE_PROFIT_PCT)
    else:
        stop_loss = entry_price * (1 + STOP_LOSS_PCT)
        take_profit = entry_price * (1 - TAKE_PROFIT_PCT)

    # Фиксированный размер позиции 1$ с плечом 5x
    position_size = max(MIN_TRADE_SIZE, 1.0) * LEVERAGE

    # Исполняем сделку
    await execute_trade(symbol, position_type, entry_price, stop_loss, take_profit, position_size)


def get_total_pnl():
    if not os.path.exists(CLOSED_TRADES_FILE):
        return 0.0
    df = pd.read_csv(CLOSED_TRADES_FILE)
    if df.empty:
        return 0.0
    return df['pnl'].sum()


def update_equity_log():
    total = get_total_pnl()
    with open("equity.csv", "a", newline="") as f:
        writer = csv.writer(f)
        if f.tell() == 0:
            writer.writerow(["timestamp", "equity"])
        writer.writerow([datetime.now().isoformat(), total])



async def check_positions():
    """Проверяет открытые позиции и отправляет уведомления при значительных изменениях PnL"""
    global last_reported_pnl

    try:
        positions = client.futures_position_information()
        messages = []

        for pos in positions:
            pos_amount = float(pos['positionAmt'])
            symbol = pos['symbol']

            if pos_amount == 0:
                # Закрытая позиция — логируем, если была
                if symbol in last_reported_pnl:
                    entry_price = float(pos['entryPrice'])
                    close_price = float(pos['markPrice'])
                    pnl = float(pos['unRealizedProfit'])

                    log_closed_trade(symbol, entry_price, close_price, pnl)

                    await send_telegram_message(
                        f"🔔 Сделка по {symbol} закрыта\n"
                        f"Цена входа: {entry_price:.4f}\n"
                        f"Цена выхода: {close_price:.4f}\n"
                        f"PnL: {pnl:.2f} USDT"
                    )

                    # Новый функционал: лог equity + суммарный PnL
                    update_equity_log()
                    total_pnl = get_total_pnl()
                    await send_telegram_message(
                        f"📊 Общий PnL с начала: {total_pnl:.2f} USDT"
                    )

                    del last_reported_pnl[symbol]
                continue

            # Открытая позиция
            current_pnl = float(pos['unRealizedProfit'])
            last_pnl = last_reported_pnl.get(symbol, 0)

            if abs(current_pnl - last_pnl) >= PNL_CHANGE_THRESHOLD:
                entry = float(pos['entryPrice'])
                mark_price = float(pos['markPrice'])
                roe = (current_pnl / float(pos['initialMargin'])) * 100

                messages.append(
                    f"📊 {symbol} {'LONG' if pos_amount > 0 else 'SHORT'}\n"
                    f"ΔPnL: {current_pnl - last_pnl:+.2f} USDT\n"
                    f"Всего PnL: {current_pnl:.2f} USDT ({roe:.1f}%)\n"
                    f"Цена: {mark_price:.4f} (Вход: {entry:.4f})"
                )

                last_reported_pnl[symbol] = current_pnl

        if messages:
            await send_telegram_message("\n\n".join(messages))

    except Exception as e:
        error_msg = f"❌ Ошибка при проверке позиций: {str(e)}"
        logging.error(error_msg)
        await send_telegram_message(error_msg)


async def main():
    global top_symbols

    print("Запуск бота...")
    await send_telegram_message("🟢 Бот запущен")

    # Первоначальная загрузка топ-30
    top_symbols = await get_top_30_futures_symbols()

    while True:
        try:
            # Проверяем количество открытых позиций
            positions = client.futures_position_information()
            active_positions = sum(1 for p in positions if float(p['positionAmt']) != 0)

            if active_positions < MAX_CONCURRENT_TRADES:
                # Обновляем топ каждые 6 часов
                if time.time() - last_top_update > 6 * 3600:
                    top_symbols = await get_top_30_futures_symbols()

                # Проверяем сигналы только если есть место для новых позиций
                for symbol in top_symbols:
                    df = get_ohlcv(symbol)
                    if df is None:
                        continue

                    signals = get_signals(analyze(df))
                    if not signals:
                        continue

                    # Пропускаем если уже есть открытая позиция
                    if any(p['symbol'] == symbol and float(p['positionAmt']) != 0 for p in positions):
                        continue

                    # Логика входа
                    await process_trade(symbol, signals)

            # Проверяем позиции каждые 30 минут
            await check_positions()
            await report_closed_trades()
            await asyncio.sleep(1800)  # 30 минут

        except Exception as e:
            error_msg = f"🔴 Критическая ошибка: {str(e)}"
            print(error_msg)
            await send_telegram_message(error_msg)
            await asyncio.sleep(60)


if __name__ == '__main__':
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("Бот остановлен")
        loop.run_until_complete(send_telegram_message("🔴 Бот остановлен вручную"))
    except Exception as e:
        error_msg = f"🔴 Критическая ошибка: {str(e)}"
        print(error_msg)
        loop.run_until_complete(send_telegram_message(error_msg))