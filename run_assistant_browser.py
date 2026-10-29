"""Local Gemini Live client using Chrome's microphone echo cancellation.

Install extra dependency: python -m pip install aiohttp==3.14.4
Run next to instructions.txt. The server binds only to 127.0.0.1.
"""
import argparse
import asyncio
import json
import secrets
import sys
import webbrowser
from pathlib import Path

from aiohttp import web, WSMsgType
from google import genai
from google.genai import types

MODEL = "gemini-3.8-live"
VOICE = "Zephyr"
SESSION_SECONDS = 180
ROOT = Path(__file__).resolve().parent

WORKLET = r"""
class MicCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.phase = 0; this.sum = 0; this.count = 0;
    this.frame = new Int16Array(512); this.offset = 0;
  }
  process(inputs, outputs) {
    for (const group of outputs) for (const channel of group) channel.fill(0);
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    for (const sample of input) {
      this.sum += sample; this.count += 1; this.phase += 16000;
      if (this.phase >= sampleRate) {
        this.phase -= sampleRate;
        const value = Math.max(-1, Math.min(1, this.sum / this.count));
        this.sum = 0; this.count = 0;
        this.frame[this.offset++] = value < 0 ? value * 32768 : value * 32767;
        if (this.offset === this.frame.length) {
          this.port.postMessage(this.frame.buffer, [this.frame.buffer]);
          this.frame = new Int16Array(512); this.offset = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('mic-capture', MicCapture);
"""

HTML = r"""<!doctype html>
<html lang="ar" dir="rtl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>مساعد محمد الصوتي</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#0c1320;color:#e9eff7;font:17px/1.7 system-ui,sans-serif}
main{max-width:760px;margin:6vh auto;padding:28px}section{padding:28px;background:#142137;border:1px solid #2d405d;border-radius:22px}
h1{font-size:28px;margin:0 0 8px}p{color:#b4c5dc}label{display:block;margin:20px 0 6px}
input{width:100%;padding:13px;border:1px solid #4c6382;border-radius:10px;background:#0c1729;color:white;font:inherit;direction:ltr}
button{padding:12px 24px;border:0;border-radius:10px;background:#71dfbf;color:#10211b;font:700 17px system-ui;cursor:pointer;margin:18px 0 8px}
button:disabled{opacity:.5;cursor:default}#stop{background:#425573;color:white;margin-inline-start:10px}
#status{padding:15px;border-radius:10px;background:#0c1729;min-height:60px;margin-top:16px}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin-top:18px}.stats span{background:#0c1729;padding:8px 12px;border-radius:8px;font-size:14px}
progress{width:100%;height:12px;accent-color:#71dfbf}small{color:#b4c5dc}#log{font:13px/1.7 monospace;direction:ltr;text-align:left;white-space:pre-wrap;max-height:160px;overflow:auto;color:#b4c5dc}
</style>
<main><section>
<h1>مساعد محمد الصوتي</h1>
<p>تحدث بصورة طبيعية. الميكروفون يبقى مفتوحًا أثناء الرد، ويمكنك المقاطعة بالكلام.</p>
<label for="key">مفتاح Gemini API</label>
<input id="key" type="password" autocomplete="off" placeholder="الصق مفتاح مشروعك هنا" spellcheck="false">
<button id="start">بدء المحادثة</button><button id="stop" disabled>إنهاء الجلسة</button>
<div id="status" role="status">جاهز. افتح الصفحة في Chrome واسمح باستخدام الميكروفون.</div>
<div class="stats"><span id="aec">إلغاء الصدى: —</span><span id="count">المقاطعات: 0</span><span id="clock">المدة: 0 ثانية</span></div>
<p style="margin-bottom:4px">مستوى الميكروفون</p><progress id="level" max="1" value="0"></progress>
<p><small>صفحة محلية على جهازك، والصوت يُرسل إلى Google عبر الإنترنت. المفتاح لا يُحفظ في ملفات أو تخزين المتصفح بواسطة هذا التطبيق. تنتهي التجربة بعد 3 دقائق.</small></p>
<details><summary>تفاصيل الاتصال</summary><div id="log"></div></details>
</section></main>
<script>
const sessionToken = '__SESSION_TOKEN__';
const $ = id => document.getElementById(id);
let ws = null, context = null, media = null, capture = null, source = null;
let active = false, ready = false, playing = new Set(), playAt = 0, interruptions = 0;
let clock = null, startTime = 0, dropped = 0, generation = 0;
function status(text) { $('status').textContent = text; }
function log(text) { const box=$('log'); box.textContent += text+'\n'; box.scrollTop=box.scrollHeight; }
function clearPlayback() {
  for (const node of playing) { try { node.stop(); } catch (_) {} }
  playing.clear(); playAt = context ? context.currentTime : 0;
}
function playPCM(buffer) {
  if (!active || !context || !buffer.byteLength) return;
  const pcm = new Int16Array(buffer);
  const audio = context.createBuffer(1, pcm.length, 24000);
  const channel = audio.getChannelData(0);
  for (let i=0;i<pcm.length;i++) channel[i]=pcm[i]/32768;
  const node=context.createBufferSource(); node.buffer=audio; node.connect(context.destination);
  playAt=Math.max(playAt,context.currentTime+0.06);
  node.start(playAt); playAt+=audio.duration; playing.add(node);
  node.onended=()=>{ playing.delete(node); node.disconnect(); };
}
async function stopSession(finalMessage) {
  generation++; active=false; ready=false;
  if (clock) clearInterval(clock); clock=null;
  clearPlayback();
  const socket=ws; ws=null; if(socket && socket.readyState<2) socket.close();
  if(capture) capture.disconnect(); if(source) source.disconnect();
  capture=null;source=null;
  if(media) for(const track of media.getTracks()) track.stop(); media=null;
  const ctx=context; context=null; if(ctx) await ctx.close().catch(()=>{});
  $('start').disabled=false; $('stop').disabled=true; $('level').value=0;
  if(finalMessage) status(finalMessage);
}
async function startSession() {
  const key=$('key').value.trim();
  if(!key) { status('الصق مفتاح Gemini API أولًا.'); return; }
  $('start').disabled=true; $('stop').disabled=false; $('log').textContent='';
  active=true; interruptions=0; dropped=0; const mine=++generation;
  $('count').textContent='المقاطعات: 0'; status('جاري تجهيز الميكروفون والاتصال…');
  try {
    context=new AudioContext({latencyHint:'interactive'});
    await context.resume();
    const stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:{ideal:true},noiseSuppression:{ideal:true},autoGainControl:{ideal:true},channelCount:{ideal:1}},video:false});
    if(!active || mine!==generation) { stream.getTracks().forEach(t=>t.stop()); return; }
    media=stream;
    const settings=media.getAudioTracks()[0].getSettings();
    $('aec').textContent='إلغاء الصدى: '+(settings.echoCancellation===true?'مفعّل':settings.echoCancellation===false?'غير مفعّل':'غير مؤكد');
    log('Microphone settings: '+JSON.stringify({echoCancellation:settings.echoCancellation,noiseSuppression:settings.noiseSuppression,autoGainControl:settings.autoGainControl,inputRate:settings.sampleRate,playbackRate:context.sampleRate}));
    await context.audioWorklet.addModule('/capture.js');
    if(!active || mine!==generation) return;
    capture=new AudioWorkletNode(context,'mic-capture');
    source=context.createMediaStreamSource(media); source.connect(capture); capture.connect(context.destination);
    capture.port.onmessage=event=>{
      if(!active) return;
      const samples=new Int16Array(event.data); let square=0;
      for(const x of samples) square+=(x/32768)**2;
      $('level').value=Math.min(1,Math.sqrt(square/samples.length)*5);
      // Always send processed microphone audio, including during model playback.
      if(ready && ws && ws.readyState===WebSocket.OPEN) {
        if(ws.bufferedAmount<16384) ws.send(event.data);
        else { dropped++; if(dropped===1 || dropped%50===0) log('Network input backlog: dropped chunks='+dropped); }
      }
    };
    const socket=new WebSocket('ws://'+location.host+'/ws'); ws=socket;
    socket.binaryType='arraybuffer';
    socket.onopen=()=>{if(mine===generation) {socket.send(JSON.stringify({key,token:sessionToken}));$('key').value='';}};
    socket.onmessage=async event=>{
      if(mine!==generation || !active) return;
      if(event.data instanceof ArrayBuffer) {playPCM(event.data);return;}
      const message=JSON.parse(event.data);
      if(message.type==='ready') {
        ready=true;startTime=Date.now(); status('متصل. تحدث بالعربية؛ يمكنك مقاطعة المساعد بالكلام.');
        clock=setInterval(()=>$('clock').textContent='المدة: '+Math.floor((Date.now()-startTime)/1000)+' ثانية',1000);
        log('Gemini connected. Continuous microphone input enabled.');
      } else if(message.type==='interrupted') {
        clearPlayback();interruptions++;$('count').textContent='المقاطعات: '+interruptions;
        status('تمت المقاطعة؛ أسمعك، تفضل.'); log('Google interruption received: '+interruptions);
      } else if(message.type==='error' || message.type==='ended') {
        log(message.message); await stopSession(message.message);
      }
    };
    socket.onerror=()=>{if(mine===generation) log('WebSocket connection error');};
    socket.onclose=()=>{if(active && mine===generation) stopSession('انتهى الاتصال. يمكنك بدء جلسة جديدة.');};
  } catch(error) {
    const text=String(error).replaceAll(key,'[REDACTED]');log(text);
    await stopSession('تعذر البدء: '+text);
  }
}
$('start').onclick=startSession;
$('stop').onclick=()=>stopSession('تم إنهاء الجلسة.');
window.addEventListener('beforeunload',()=>{if(ws)ws.close();if(media)media.getTracks().forEach(t=>t.stop());});
</script></html>
"""


def make_config():
    instructions = (ROOT / "instructions.txt").read_text(encoding="utf-8-sig")
    instructions += "\nرحّب مرة واحدة فقط عند بداية الجلسة. بعد ذلك أجب عن كلام المستخدم دون إعادة الترحيب."
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=instructions,
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE))),
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=16000, sliding_window=types.SlidingWindow(target_tokens=8000)),
    )


async def receive_model(session, socket):
    while True:
        async for response in session.receive():
            content = response.server_content
            if content and content.interrupted:
                await socket.send_json({"type": "interrupted"})
                continue
            if content and content.model_turn:
                for part in content.model_turn.parts or []:
                    blob = part.inline_data
                    if blob and blob.data and (blob.mime_type or "").startswith("audio/"):
                        await socket.send_bytes(blob.data)


async def receive_browser(session, socket):
    async for message in socket:
        if message.type == WSMsgType.BINARY:
            if len(message.data) > 16384 or len(message.data) % 2:
                raise ValueError("Unexpected microphone packet size")
            await session.send_realtime_input(
                audio=types.Blob(data=message.data, mime_type="audio/pcm;rate=16000"))
        elif message.type == WSMsgType.ERROR:
            raise ConnectionError("Browser connection closed")


class LocalService:
    def __init__(self, port):
        self.port = port
        self.token = secrets.token_urlsafe(32)
        self.active = False
        self.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    async def page(self, request):
        return web.Response(text=HTML.replace("__SESSION_TOKEN__", self.token),
                            content_type="text/html", headers={"Cache-Control": "no-store"})

    async def worklet(self, request):
        return web.Response(text=WORKLET, content_type="application/javascript")

    async def websocket(self, request):
        if request.headers.get("Origin") not in self.origins:
            raise web.HTTPForbidden()
        socket = web.WebSocketResponse(heartbeat=20, max_msg_size=32768, compress=False)
        await socket.prepare(request)
        client = None
        key = ""
        jobs = []
        owns_session = False
        try:
            first = await asyncio.wait_for(socket.receive(), timeout=20)
            if first.type != WSMsgType.TEXT:
                raise ValueError("Missing session setup")
            setup = json.loads(first.data)
            if not secrets.compare_digest(str(setup.get("token", "")), self.token):
                raise ValueError("Refresh this local page and try again")
            key = str(setup.get("key", "")).strip()
            if not key:
                raise ValueError("A Gemini API key is required")
            if self.active:
                raise ValueError("A session is already running. Close the other tab first.")
            self.active = True
            owns_session = True
            config = make_config()
            client = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
            async with client.aio.live.connect(model=MODEL, config=config) as session:
                print("Gemini connected. Browser microphone remains active during playback.")
                await socket.send_json({"type": "ready"})
                timer = asyncio.create_task(asyncio.sleep(SESSION_SECONDS))
                jobs = [asyncio.create_task(receive_browser(session, socket)),
                        asyncio.create_task(receive_model(session, socket)), timer]
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part.from_text(
                        text="ابدأ المحادثة بالترحيب المحدد في تعليماتك.")]), turn_complete=True)
                done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                for job in done:
                    job.result()
                if timer in done and not socket.closed:
                    await socket.send_json({"type": "ended", "message": "انتهت تجربة الثلاث دقائق. يمكنك بدء جلسة جديدة."})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            text = str(error).replace(key, "[REDACTED]") if key else str(error)
            print("Session error:", text)
            if not socket.closed:
                await socket.send_json({"type": "error", "message": text})
        finally:
            for job in jobs:
                job.cancel()
            if jobs:
                await asyncio.gather(*jobs, return_exceptions=True)
            if client is not None:
                await client.aio.aclose()
            if owns_session:
                self.active = False
            await socket.close()
        return socket

    def create_app(self):
        @web.middleware
        async def local_hosts_only(request, handler):
            if request.host not in {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}:
                raise web.HTTPForbidden()
            return await handler(request)
        app = web.Application(middlewares=[local_hosts_only])
        app.router.add_get("/", self.page)
        app.router.add_get("/capture.js", self.worklet)
        app.router.add_get("/ws", self.websocket)
        return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    url = f"http://127.0.0.1:{args.port}"
    print("Mohammad Voice Assistant | Browser echo-cancellation version")
    print("Keep this terminal open. Open in Chrome:", url)
    print("Use your existing non-billed Gemini project. Stop server: Ctrl+C.")
    service = LocalService(args.port)
    async def open_browser(app):
        if not args.no_browser:
            asyncio.get_running_loop().call_later(1, lambda: webbrowser.open(url))
    app = service.create_app()
    app.on_startup.append(open_browser)
    web.run_app(app, host="127.0.0.1", port=args.port, access_log=None)


if __name__ == "__main__":
    main()
