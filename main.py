import os
import json
import time
import requests
import websocket
from threading import Thread
from flask import Flask

# === SEU TELEGRAM - COLOCA AQUI OU NO RENDER ===
TOKEN = os.getenv("359523867:AAHspJgzTocY2k--UBrmLGQnKmilN9CSw8s")
CHAT_ID = os.getenv("270814176")

XRP_PRICE = 1.3038
IMB = 47.5
BIDS = 430000
cluster_up = {}
cluster_down = {}
current_short = 0
current_long = 0
last_alert = 0

def send_telegram(msg):
    global last_alert
    if time.time() - last_alert < 60:  # 1 alerta por minuto pra não floodar
        return
    last_alert = time.time()
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=5)
        print(f"Telegram enviado: {msg[:50]}")
    except Exception as e:
        print(f"Erro telegram: {e}")

def calc_atr_targets(price):
    # Alvos dinâmicos que mudam com preço - nunca fica Buscando...
    return {
        "up": [price*1.03, price*1.06, price*1.09],
        "down": [price*0.97, price*0.94, price*0.91]
    }

def on_message_price(ws, message):
    global XRP_PRICE, IMB, BIDS, cluster_up, cluster_down, current_short, current_long
    try:
        d = json.loads(message)
        if 'c' in d:
            XRP_PRICE = float(d['c'])
        if 'bids' in d:
            bids = sum(float(p)*float(q) for p,q in d['bids'])
            asks = sum(float(p)*float(q) for p,q in d['asks'])
            imb = bids/(bids+asks)*100 if (bids+asks)>0 else 50
            IMB = imb
            BIDS = bids

            # Se IMB forte, manda Telegram
            if imb > 56 and current_short:
                targets = calc_atr_targets(XRP_PRICE)
                send_telegram(f"🐋 *BALEIA COMPRANDO*\nXRP: ${XRP_PRICE:.4f}\nIMB: {imb:.1f}% - COMPRA FORTE\nBIDS: ${bids/1e6:.2f}M\n\n🎯 *ALVOS SHORTS*\n↑ ${targets['up'][0]:.4f} (+3%)\n🔥 ${targets['up'][1]:.4f} (+6%)\n↑ ${targets['up'][2]:.4f} (+9%)\n\n💰 Entra acompanhando baleia")
            elif imb < 44 and current_long:
                targets = calc_atr_targets(XRP_PRICE)
                send_telegram(f"🐋 *BALEIA VENDENDO*\nXRP: ${XRP_PRICE:.4f}\nIMB: {imb:.1f}% - VENDA FORTE\n\n🎯 *ALVOS LONGS*\n↓ ${targets['down'][0]:.4f} (-3%)\n🔥 ${targets['down'][1]:.4f} (-6%)\n↓ ${targets['down'][2]:.4f} (-9%)")

    except Exception as e:
        print(e)

def on_message_liq(ws, message):
    global cluster_up, cluster_down, current_short, current_long
    try:
        msg = json.loads(message)
        if 'o' not in msg:
            return
        o = msg['o']
        price = float(o['p'])
        qty = float(o['q'])
        key = f"{price:.4f}"
        if price > XRP_PRICE:
            cluster_up[key] = cluster_up.get(key, 0) + qty
        else:
            cluster_down[key] = cluster_down.get(key, 0) + qty

        # Mantém só os 20 maiores
        if len(cluster_up) > 20:
            cluster_up = dict(sorted(cluster_up.items(), key=lambda x: x[1], reverse=True)[:20])
        if len(cluster_down) > 20:
            cluster_down = dict(sorted(cluster_down.items(), key=lambda x: x[1], reverse=True)[:20])

        # Atualiza alvos atuais
        if cluster_up:
            current_short = float(max(cluster_up, key=lambda k: cluster_up[k]))
        if cluster_down:
            current_long = float(max(cluster_down, key=lambda k: cluster_down[k]))

    except Exception as e:
        print(f"liq erro: {e}")

def run_price_ws():
    while True:
        try:
            print("Conectando price WS...")
            ws = websocket.WebSocketApp(
                "wss://stream.binance.com:9443/ws/xrpusdt@ticker/xrpusdt@depth20@100ms",
                on_message=on_message_price
            )
            ws.run_forever(ping_interval=30)
        except Exception as e:
            print(f"price ws caiu: {e}")
            time.sleep(5)

def run_liq_ws():
    while True:
        try:
            print("Conectando liq WS...")
            ws = websocket.WebSocketApp(
                "wss://fstream.binance.com/ws/xrpusdt@forceOrder",
                on_message=on_message_liq
            )
            ws.run_forever(ping_interval=30)
        except Exception as e:
            print(f"liq ws caiu: {e}, usando ATR")
            time.sleep(5)
            # Fallback ATR
            global current_short, current_long
            t = calc_atr_targets(XRP_PRICE)
            current_short = t["up"][1]
            current_long = t["down"][1]

# === SERVIDOR WEB PRA RENDER NÃO DORMIR ===
app = Flask(__name__)

@app.route('/')
def home():
    t = calc_atr_targets(XRP_PRICE)
    up_sorted = sorted(cluster_up.items(), key=lambda x: x[1], reverse=True)[:3] if cluster_up else [(f"{v:.4f}", 0) for v in t["up"]]
    down_sorted = sorted(cluster_down.items(), key=lambda x: x[1], reverse=True)[:3] if cluster_down else [(f"{v:.4f}", 0) for v in t["down"]]
    
    html = f"""
    <h1>HACKMON V12 PYTHON 24H</h1>
    <p>XRP: ${XRP_PRICE:.4f} | IMB: {IMB:.1f}% | BIDS: ${BIDS/1e6:.2f}M</p>
    <h3>SHORTS (cima)</h3>
    <ul>{''.join([f'<li>${k} - {v:.0f} XRP</li>' for k,v in up_sorted])}</ul>
    <h3>LONGS (baixo)</h3>
    <ul>{''.join([f'<li>${k} - {v:.0f} XRP</li>' for k,v in down_sorted])}</ul>
    <p>Ultima atualizacao: {time.strftime('%H:%M:%S')}</p>
    <p>Telegram: ATIVO - Vai avisar IMB >56% ou <44%</p>
    """
    return html

if __name__ == "__main__":
    Thread(target=run_price_ws, daemon=True).start()
    Thread(target=run_liq_ws, daemon=True).start()
    time.sleep(2)
    send_telegram(f"✅ *HACKMON V12 PYTHON ONLINE 24H*\nXRP: ${XRP_PRICE:.4f}\nIMB: {IMB:.1f}%\n\nServidor na nuvem ativo. Vou te avisar de baleias >56% IMB e alvos SHORT/LONG ao vivo.")
    print("Servidor iniciado na porta 10000")
    app.run(host="0.0.0.0", port=10000)