"""Public web page for a shared design: drag-to-rotate front/back viewer + phone login + Razorpay checkout."""
import base64
import html
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from config_store import LABELS, SIZES, load_config
from core import db
from store_routes import load_store

router = APIRouter(prefix="/api")

SHAPES = {
    "tee": {
        "front": "M105 18 L58 30 L8 78 L42 126 L72 106 L72 330 Q150 338 228 330 L228 106 L258 126 L292 78 L242 30 L195 18 Q150 62 105 18 Z",
        "back": "M105 18 L58 30 L8 78 L42 126 L72 106 L72 330 Q150 338 228 330 L228 106 L258 126 L292 78 L242 30 L195 18 Q150 34 105 18 Z",
        "handles": "",
    },
    "tote": {"front": "M38 112 L262 112 L272 332 L28 332 Z", "back": "M38 112 L262 112 L272 332 L28 332 Z",
             "handles": "M98 112 L98 72 Q98 24 150 24 Q202 24 202 72 L202 112"},
}


def _face(design: dict, side: str, area: dict, image: str | None) -> str:
    color = html.escape(design["color"])
    if design.get("product") not in ("tee", "tote"):  # 3D garments show their rendered photos
        inner = f'<img class="shot" src="{html.escape(image)}" alt="{side}">' if image else f'<div class="shot" style="background:{color}"></div>'
        return f'<div class="face {side}">{inner}</div>'
    shp = SHAPES["tote" if design.get("product") == "tote" else "tee"]
    handles = (f'<path d="{shp["handles"]}" stroke="#111" stroke-width="14" fill="none"/>'
               f'<path d="{shp["handles"]}" stroke="{color}" stroke-width="10" fill="none"/>') if shp["handles"] else ""
    layers = []
    for l in design[side]:
        src = html.escape(l["src"]) if l["kind"] == "image" else "data:image/svg+xml;base64," + base64.b64encode(l["src"].encode()).decode()
        layers.append(
            f'<img class="layer" src="{src}" style="left:{l["x"]*100:.2f}%;top:{l["y"]*100:.2f}%;width:{60*l["scale"]:.2f}%;'
            f'transform:translate(-50%,-50%) rotate({l["rotation"]}deg)">'
        )
    return (
        f'<div class="face {side}"><svg viewBox="0 0 300 340">{handles}<path d="{shp[side]}" fill="{color}" stroke="#111" stroke-width="2"/></svg>'
        f'<div class="area" style="left:{area["x"]*100}%;top:{area["y"]*100}%;width:{area["w"]*100}%;height:{area["h"]*100}%">{"".join(layers)}</div></div>'
    )


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ · DropForge</title><meta property="og:title" content="__TITLE__ on DropForge">
<style>
*{box-sizing:border-box}body{margin:0;font-family:ui-monospace,Menlo,monospace;background:#fff;color:#111}
header{border-bottom:2px solid #111;padding:14px 16px;display:flex;justify-content:space-between;align-items:center}
h1{font-family:Arial Black,Arial,sans-serif;margin:0;font-size:22px}.wrap{max-width:980px;margin:0 auto;display:flex;flex-wrap:wrap}
.stage{flex:1 1 360px;background:#f2f2f2;min-height:420px;display:flex;align-items:center;justify-content:center;perspective:1400px;touch-action:none;cursor:grab;border-bottom:2px solid #111}
.card{width:min(80vw,340px);aspect-ratio:300/340;position:relative;transform-style:preserve-3d}
.face{position:absolute;inset:0;backface-visibility:hidden;-webkit-backface-visibility:hidden}.face.back{transform:rotateY(180deg)}
.face svg{width:100%;height:100%;display:block}.area{position:absolute;overflow:hidden}.layer{position:absolute;aspect-ratio:1;object-fit:contain}
.shot{width:100%;height:100%;object-fit:contain}.panel{flex:1 1 320px;padding:16px;display:flex;flex-direction:column;gap:10px}
label{font-size:11px;font-weight:bold;letter-spacing:1px}input,select{height:46px;border:2px solid #111;padding:0 10px;font:inherit;font-size:15px;width:100%;background:#fff}
button{height:52px;border:2px solid #111;background:#FF4400;color:#fff;font:inherit;font-weight:bold;letter-spacing:1px;cursor:pointer}
button.dark{background:#111}button:disabled{opacity:.4}.row{display:flex;gap:8px}.row>*{flex:1}.price{font-size:22px;font-weight:bold}
.muted{color:#757575;font-size:12px}.err{color:#D50000;font-weight:bold;font-size:12px}.ok{color:#00C853;font-weight:bold}.hidden{display:none}
.hint{position:absolute;margin-top:-28px;font-size:10px;color:#757575}.badge{background:#FFD600;padding:6px 8px;font-size:12px;font-weight:bold;border:2px solid #111}
</style></head><body>
<header><h1>DROPFORGE</h1><span class="muted">__PRODUCT__ · __COLOR__</span></header>
<div class="wrap"><div class="stage" id="stage"><div class="card" id="card">__FRONT____BACK__</div></div>
<div class="panel"><h1 style="font-size:26px">__TITLE__</h1><span class="muted">DRAG THE PREVIEW TO SPIN IT 360°</span>
__FIRST__
<div class="row"><div><label>SIZE</label><select id="size">__SIZES__</select></div><div><label>QTY</label><input id="qty" type="number" min="1" max="20" value="1"></div>
<div><label>CURRENCY</label><select id="cur"><option>INR</option><option>USD</option><option>GBP</option><option>EUR</option><option>AED</option></select></div></div>
<div class="price" id="price"></div>
<div id="auth"><label>YOUR PHONE (WITH COUNTRY CODE)</label><input id="phone" placeholder="+91 98765 43210">
<div id="codeBox" class="hidden"><label>CODE</label><input id="code" inputmode="numeric"></div><button class="dark" id="authBtn">SEND CODE</button></div>
<div id="buy" class="hidden"><label>DISCOUNT CODE</label><div class="row"><input id="dc" placeholder="OPTIONAL"><button class="dark" id="applyBtn" style="flex:0 0 100px">APPLY</button></div>
<div id="quote" class="muted"></div>
<label>NAME</label><input id="s_name"><label>PHONE</label><input id="s_phone"><label>ADDRESS</label><input id="s_line1">
<div class="row"><div><label>CITY</label><input id="s_city"></div><div><label>POSTAL</label><input id="s_postal"></div></div>
<label>COUNTRY</label><input id="s_country"><button id="payBtn">PAY BY CARD</button></div>
<div id="msg"></div></div></div>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>
const SLUG=__SLUG__,PRICES=__PRICES__,API='/api';let rot=0,start=null,tok=localStorage.getItem('df_token');
const $=id=>document.getElementById(id),card=$('card'),stage=$('stage');
const sym={INR:'₹',USD:'$',GBP:'£',EUR:'€',AED:'AED '};const fmt=(m,c)=>sym[c]+(m/100).toFixed(2);
stage.onpointerdown=e=>{start=[e.clientX,rot];stage.setPointerCapture(e.pointerId)};
stage.onpointermove=e=>{if(start){rot=start[1]+(e.clientX-start[0])*0.6;card.style.transform='rotateY('+rot+'deg)'}};stage.onpointerup=()=>start=null;
function msg(t,ok){$('msg').className=ok?'ok':'err';$('msg').textContent=t}
async function call(p,b){const r=await fetch(API+p,{method:b?'POST':'GET',headers:Object.assign({'Content-Type':'application/json'},tok?{Authorization:'Bearer '+tok}:{}),body:b?JSON.stringify(b):undefined});
const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:'Something went wrong');return d}
function body(){return{share_slug:SLUG,size:$('size').value,qty:+$('qty').value||1,currency:$('cur').value,discount_code:$('dc').value||null}}
function showPrice(){$('price').textContent=fmt(Math.round(PRICES[$('cur').value]*100)*(+$('qty').value||1),$('cur').value)}
async function quote(){showPrice();if(!tok)return;try{const q=await call('/orders/quote',body());$('quote').textContent=q.discount?('SUBTOTAL '+fmt(q.subtotal,q.currency)+' · '+q.discount_label+' -'+q.discount_pct+'% · TOTAL '+fmt(q.total,q.currency)):'TOTAL '+fmt(q.total,q.currency);msg(q.code_error||'')}catch(e){$('quote').textContent='';msg(e.message)}}
['size','qty','cur'].forEach(i=>$(i).onchange=quote);$('applyBtn').onclick=quote;
function signedIn(){$('auth').classList.add('hidden');$('buy').classList.remove('hidden');quote()}
let sent=false;$('authBtn').onclick=async()=>{const phone=$('phone').value.replace(/[\\s()-]/g,'');try{if(!sent){await call('/auth/send-otp',{phone});sent=true;$('codeBox').classList.remove('hidden');$('authBtn').textContent='VERIFY & CONTINUE';msg('Code sent',true)}
else{const r=await call('/auth/verify-otp',{phone,code:$('code').value});tok=r.token;localStorage.setItem('df_token',tok);$('s_phone').value=phone;signedIn()}}catch(e){msg(e.message)}};
$('payBtn').onclick=async()=>{const s={};['name','phone','line1','city','postal','country'].forEach(k=>s[k]=$('s_'+k).value.trim());
if(Object.values(s).some(v=>v.length<2))return msg('Fill in all shipping fields');$('payBtn').disabled=true;
try{const r=await call('/orders',Object.assign(body(),{shipping:s}));const c=r.checkout;
new Razorpay({key:c.key_id,amount:c.amount,currency:c.currency,name:c.name,description:c.description,order_id:c.order_id,prefill:c.prefill,theme:{color:'#FF4400'},
handler:async x=>{try{await call('/orders/verify',x);msg('ORDER PAID! Track it in the DropForge app under Orders.',true);$('buy').classList.add('hidden')}catch(e){msg(e.message)}},
modal:{ondismiss:()=>{$('payBtn').disabled=false;msg('Payment cancelled')}}}).open()}catch(e){msg(e.message);$('payBtn').disabled=false}};
if(tok)call('/auth/me').then(signedIn).catch(()=>{tok=null;localStorage.removeItem('df_token');showPrice()});else showPrice();
</script></body></html>"""


@router.get("/s/{slug}", response_class=HTMLResponse)
async def share_page(slug: str):
    sh = await db.shares.find_one({"slug": slug}, {"_id": 0})
    if not sh:
        raise HTTPException(404, "Link not found")
    d = sh["design"]
    product = d.get("product", "tee")
    cfg = await load_config()
    areas = cfg.products[product].model_dump()
    store = await load_store()
    first = (f'<div class="badge">FIRST ORDER? GET {store.first_order_pct:g}% OFF AUTOMATICALLY</div>' if store.first_order_pct else "")
    title = html.escape(d["name"])
    page = (
        PAGE.replace("__TITLE__", title)
        .replace("__PRODUCT__", LABELS.get(product, product.upper()))
        .replace("__COLOR__", html.escape(d["color"]))
        .replace("__FRONT__", _face(d, "front", areas["front"], sh.get("front_image")))
        .replace("__BACK__", _face(d, "back", areas["back"], sh.get("back_image")))
        .replace("__FIRST__", first)
        .replace("__SIZES__", "".join(f"<option{' selected' if s == 'M' else ''}>{s}</option>" for s in SIZES[product]))
        .replace("__SLUG__", json.dumps(slug))
        .replace("__PRICES__", json.dumps(areas["prices"]))
    )
    return HTMLResponse(page)
