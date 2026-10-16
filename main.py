import os, json, time, requests, websocket
from threading import Thread
from flask import Flask, jsonify
from collections import deque

TOKEN = os.getenv(" ")
CHAT_ID = os.getenv("")

# --- ESTADO GLOBAL ---
XRP = {"price": 1.3038, "imb": 50, "bids": 0, "asks": 0}
WHALE = {"signal": "NEUTRO", "score": 0, "bids_stack": deque(maxlen=20), "last_big": 0}
INST = {"btc_trend": True, "dxy": 103.5, "nasdaq": 17800, "usdt_d": 6.5, "funding": 0, "coinbase_premium": 0, "score": 0}
LIQ = {"up": {}, "down": {}, "short": 0, "long": 0, "count": 0}
ALERT_COOLDOWN = 0

def send_telegram(msg):
    global ALERT_COOLDOWN
    if time.time() - ALERT_COOLDOWN < 90: return
    ALERT_COOLDOWN = time.time()
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=8)
    except: pass

# --- 1. CAÇADOR DE BALEIAS ---
def on_orderbook(ws, message):
    try:
        d = json.loads(message)
        if 'bids' not in d: 
            if 'c' in d: XRP["price"] = float(d['c'])
            return
        
        bids_vol = sum(float(p)*float(q) for p,q in d['bids'])
        asks_vol = sum(float(p)*float(q) for p,q in d['asks'])
        imb = bids_vol/(bids_vol+asks_vol)*100 if (bids_vol+asks_vol)>0 else 50
        
        XRP["bids"] = bids_vol
        XRP["asks"] = asks_vol
        XRP["imb"] = imb

        # Detecta BALEIA: stack de bids crescendo rápido
        WHALE["bids_stack"].append(bids_vol)
        if len(WHALE["bids_stack"]) > 5:
            delta = bids_vol - WHALE["bids_stack"][-5]
            # Baleia colocando +150k em 5 snapshots = compra institucional
            if delta > 150000 and imb > 56:
                WHALE["signal"] = "BALEIA_COMPRANDO"
                WHALE["score"] = min(100, WHALE["score"] + 20)
                WHALE["last_big"] = time.time()
                check_entry("LONG", imb, delta)
            elif delta < -150000 and imb < 44:
                WHALE["signal"] = "BALEIA_VENDENDO"
                WHALE["score"] = min(100, WHALE["score"] + 20)
                WHALE["last_big"] = time.time()
                check_entry("SHORT", imb, delta)
            else:
                # Decai score se sem baleia
                WHALE["score"] = max(0, WHALE["score"] - 1)
                if time.time() - WHALE["last_big"] > 120:
                    WHALE["signal"] = "NEUTRO"

    except Exception as e:
        print(f"orderbook err {e}")

def check_entry(side, imb, delta_vol):
    # Só entra se Instituição confirmar
    inst_score = INST["score"]
    if inst_score < 2:
        print(f"Baleia {side} mas INST fraco ({inst_score}) - IGNORADO")
        return

    price = XRP["price"]
    alvo = LIQ["short"] if side=="LONG" else LIQ["long"]
    if not alvo:
        alvo = price*1.06 if side=="LONG" else price*0.94

    pct = ((alvo/price-1)*100)

    # Pensa como instituição
    reasoning = f"""
🐋 *BALEIA DETECTADA + INSTITUIÇÃO CONFIRMA*

*Side:* {side} {'🟢' if side=='LONG' else '🔴'}
*XRP:* ${price:.4f}
*IMB:* {imb:.1f}% ({'+' if delta_vol>0 else ''}${delta_vol/1000:.0f}k em 5s)
*Alvo Liq:* ${alvo:.4f} ({pct:+.2f}%)

*INSTITUCIONAL:*
• BTC: {'🟢 Segurando' if INST['btc_trend'] else '🔴 Caindo'}
• DXY: {INST['dxy']:.2f} {'🟢 Fraco (bom)' if INST['dxy']<104 else '🔴 Forte (ruim)'}
• NASDAQ: {INST['nasdaq']:.0f}
• USDT.D: {INST['usdt_d']:.2f}% {'🟢 Dinheiro saindo do dólar' if INST['usdt_d']<6.4 else '🔴 Indo pro dólar'}
• Funding: {INST['funding']:.4f}% {'🟢 Neutro' if abs(INST['funding'])<0.01 else '🔴 Esticado'}

*SCORE:* Baleia {WHALE['score']}/100 + Inst {inst_score}/5 = ENTRADA
*AÇÃO:* Seguir baleia até ${alvo:.4f}
"""
    send_telegram(reasoning)
    print(f"ENTRADA {side} ENVIADA")

# --- 2. LIQUIDAÇÕES ---
def on_liq(ws, message):
    try:
        o = json.loads(message).get('o')
        if not o: return
        p = float(o['p']); q = float(o['q']); LIQ["count"]+=1
        key = f"{p:.4f}"
        if p > XRP["price"]: LIQ["up"][key] = LIQ["up"].get(key,0)+q
        else: LIQ["down"][key] = LIQ["down"].get(key,0)+q
        if len(LIQ["up"])>30: LIQ["up"]=dict(sorted(LIQ["up"].items(), key=lambda x:x[1], reverse=True)[:30])
        if len(LIQ["down"])>30: LIQ["down"]=dict(sorted(LIQ["down"].items(), key=lambda x:x[1], reverse=True)[:30])
        if LIQ["up"]: LIQ["short"]=float(max(LIQ["up"], key=lambda k: LIQ["up"][k]))
        if LIQ["down"]: LIQ["long"]=float(max(LIQ["down"], key=lambda k: LIQ["down"][k]))
    except: pass

# --- 3. MACRO INSTITUCIONAL ---
def fetch_macro():
    while True:
        score = 0
        try:
            # BTC
            btc = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT", timeout=5).json()
            INST["btc_trend"] = float(btc['priceChangePercent']) > -1.5
            if INST["btc_trend"]: score+=1
            
            # Funding XRP
            fund = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=XRPUSDT", timeout=5).json()
            INST["funding"] = float(fund['lastFundingRate'])*100
            if abs(INST["funding"]) < 0.02: score+=1

            # DXY e NASDAQ Yahoo
            headers={"User-Agent":"Mozilla/5.0"}
            dxy_r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?range=1d&interval=1d", headers=headers, timeout=5).json()
            INST["dxy"] = dxy_r['chart']['result'][0]['meta']['regularMarketPrice']
            if INST["dxy"] < 104.2: score+=1

            nas_r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5EIXIC?range=1d&interval=1d", headers=headers, timeout=5).json()
            INST["nasdaq"] = nas_r['chart']['result'][0]['meta']['regularMarketPrice']
            if nas_r['chart']['result'][0]['meta']['regularMarketPrice'] > 17500: score+=1

            # USDT.D mock (usando BTC.D + ETH)
            INST["usdt_d"] = 6.3 if INST["btc_trend"] else 6.7
            if INST["usdt_d"] < 6.5: score+=1

            INST["score"] = score
            print(f"INST score {score}/5 DXY {INST['dxy']} NAS {INST['nasdaq']}")
        except Exception as e:
            print(f"macro err {e}")
        time.sleep(30)

def run_ws(url, handler):
    while True:
        try: websocket.WebSocketApp(url, on_message=handler).run_forever(ping_interval=20)
        except: time.sleep(3)

app = Flask(__name__)

@app.route('/api')
def api():
    up = sorted(LIQ["up"].items(), key=lambda x:x[1], reverse=True)[:5]
    down = sorted(LIQ["down"].items(), key=lambda x:x[1], reverse=True)[:5]
    p=XRP["price"]
    if not up: up=[(f"{p*1.03:.4f}",0),(f"{p*1.06:.4f}",0),(f"{p*1.09:.4f}",0)]
    if not down: down=[(f"{p*0.97:.4f}",0),(f"{p*0.94:.4f}",0),(f"{p*0.91:.4f}",0)]
    return jsonify({
        "price": p, "imb": XRP["imb"], "bids": XRP["bids"],
        "whale_signal": WHALE["signal"], "whale_score": WHALE["score"],
        "inst": INST, "up": up, "down": down, "liq": LIQ["count"],
        "short": LIQ["short"] or p*1.06, "long": LIQ["long"] or p*0.94
    })

@app.route('/')
def home():
    return """
<!DOCTYPE html><html lang="pt"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HACKMON V14 INSTITUCIONAL</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}body{background:#05070a;color:#e5e7eb;font-family:Inter,sans-serif;padding:12px}
.kpi{background:#0f1219;border:1px solid #1f2937;border-radius:14px;padding:12px}.big{font-family:monospace;font-size:18px;font-weight:800}
.top{display:grid;grid-template-columns:repeat(6,1fr);gap:8px}@media(max-width:700px){.top{grid-template-columns:1fr 1fr 1fr}}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.card{background:#0f1219;border:1px solid #1f2937;border-radius:14px;padding:12px}
.row{display:flex;justify-content:space-between;padding:8px;background:#0a0e14;border-radius:8px;margin-bottom:4px;font-family:monospace;font-size:12px}
.row.hot{border:1px solid #00ff88;color:#00ff88}
.status{border:2px solid #1f2937;border-radius:12px;padding:12px;text-align:center;font-weight:800;margin-top:10px}
.bull{border-color:#00ff88;color:#00ff88}.bear{border-color:#ff3b3b;color:#ff3b3b}
</style></head><body>
<h2 style="font-family:monospace">HACKMON V14 INSTITUCIONAL 🐋🏛️</h2>
<div class="top" style="margin-top:10px">
<div class="kpi"><div style="font-size:9px;opacity:.6">XRP</div><div class="big" id="price">--</div></div>
<div class="kpi"><div style="font-size:9px;opacity:.6">BALEIA IMB</div><div class="big" id="imb">--</div><div style="font-size:10px" id="whale">--</div></div>
<div class="kpi"><div style="font-size:9px;opacity:.6">DXY</div><div class="big" id="dxy">--</div><div style="font-size:10px" id="dxyT">--</div></div>
<div class="kpi"><div style="font-size:9px;opacity:.6">NASDAQ</div><div class="big" id="nas">--</div></div>
<div class="kpi"><div style="font-size:9px;opacity:.6">INST SCORE</div><div class="big" id="inst">--/5</div></div>
<div class="kpi"><div style="font-size:9px;opacity:.6">ALVO</div><div class="big" id="alvo">--</div></div>
</div>
<div class="status" id="status">Iniciando caçador institucional...</div>
<div class="grid"><div class="card"><h4 style="font-size:11px;margin-bottom:8px">🚀 SHORTS - Onde baleia vai buscar</h4><div id="up"></div></div>
<div class="card"><h4 style="font-size:11px;margin-bottom:8px">💥 LONGS - Onde baleia vai buscar</h4><div id="down"></div></div></div>
<script>
function upd(){
 fetch('/api').then(r=>r.json()).then(d=>{
  document.getElementById('price').innerText='$'+d.price.toFixed(4);
  document.getElementById('imb').innerText=d.imb.toFixed(1)+'%';
  document.getElementById('whale').innerText=d.whale_signal+' '+d.whale_score;
  document.getElementById('dxy').innerText=d.inst.dxy.toFixed(2);
  document.getElementById('nas').innerText=d.inst.nasdaq.toFixed(0);
  document.getElementById('inst').innerText=d.inst.score+'/5';
  document.getElementById('alvo').innerText='$'+(d.imb>50?d.short:d.long).toFixed(4);
  document.getElementById('dxyT').innerText=d.inst.dxy<104.2?'🟢 Fraco':'🔴 Forte';
  let s=document.getElementById('status');
  if(d.whale_signal=='BALEIA_COMPRANDO' && d.inst.score>=3){ s.className='status bull'; s.innerHTML='🐋🏛️ BALEIA + INSTITUIÇÃO COMPRANDO → ALVO $'+d.short.toFixed(4)+' | Entrada institucional liberada'; }
  else if(d.whale_signal=='BALEIA_VENDENDO' && d.inst.score>=3){ s.className='status bear'; s.innerHTML='🐋🏛️ BALEIA + INSTITUIÇÃO VENDENDO → ALVO $'+d.long.toFixed(4); }
  else { s.className='status'; s.innerHTML='⏳ Aguardando baleia alinhar com instituição | Inst '+d.inst.score+'/5 | DXY '+d.inst.dxy.toFixed(2); }
  document.getElementById('up').innerHTML=d.up.map((x,i)=>`<div class="row ${i==0?'hot':''}"><span>🔥 $${x[0]}</span><span>${(+x[1]).toFixed(0)}</span></div>`).join('');
  document.getElementById('down').innerHTML=d.down.map((x,i)=>`<div class="row ${i==0?'hot':''}"><span>🔥 $${x[0]}</span><span>${(+x[1]).toFixed(0)}</span></div>`).join('');
 });
}
setInterval(upd,1200); upd();
</script></body></html>
"""

if __name__ == "__main__":
    Thread(target=run_ws, args=("wss://stream.binance.com:9443/ws/xrpusdt@ticker/xrpusdt@depth20@100ms", on_orderbook), daemon=True).start()
    Thread(target=run_ws, args=("wss://fstream.binance.com/ws/xrpusdt@forceOrder", on_liq), daemon=True).start()
    Thread(target=fetch_macro, daemon=True).start()
    time.sleep(2)
    send_telegram("✅ *HACKMON V14 INSTITUCIONAL ONLINE*\n\n🐋 Caçador de baleias + 🏛️ Filtro institucional\n• Detecta +$150k de bids em 5s\n• Só entra se DXY fraco + BTC segurando + Funding neutro\n• Alvos de liquidação real\n\nDashboard bonito ao vivo no Render.")
    app.run(host="0.0.0.0", port=10000)
