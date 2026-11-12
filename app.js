const $ = s => document.querySelector(s);
const canvas = $('#previewCanvas'), ctx = canvas.getContext('2d');
const stage = $('#dropZone'), input = $('#mediaInput'), empty = $('#emptyState');
const fpsInput = $('#fpsInput');
let fps = 60, duration = 1199/60, template = null, media = null, mediaType = null;
let playing = false, startAt = 0, pausedAt = 0, raf = 0, zoom = 1;
let keyframes = {};
const xmlUrl = 'templates/imortal-car-slam.xml';

function toast(message){const el=$('#toast');el.textContent=message;el.classList.add('show');clearTimeout(toast.t);toast.t=setTimeout(()=>el.classList.remove('show'),2600)}
function parseTemplate(xmlText){
  const doc=new DOMParser().parseFromString(xmlText,'application/xml');
  if(doc.querySelector('parsererror')) throw new Error('The template XML could not be read.');
  const scene=doc.querySelector('scene');
  const total=Number(scene?.getAttribute('totalTime')||1199);
  const sourceFps=Number(scene?.getAttribute('fps')||60);
  duration=total/sourceFps;
  const groups=['location','scale','rotation'];
  groups.forEach(group=>{
    keyframes[group]=[...doc.querySelectorAll(`transform ${group} kf`)].map(k=>{
      const vals=k.getAttribute('v').split(',').map(Number);
      const bez=(k.getAttribute('e')||'').match(/cubicBezier\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)/);
      return {t:Number(k.getAttribute('t')),v:vals,e:bez?bez.slice(1).map(Number):null};
    }).sort((a,b)=>a.t-b.t);
  });
  $('#durationLabel').textContent=formatTime(duration);
  buildRulers();
}
function cubic(x1,y1,x2,y2,t){
  const bez=(a,b,c,d,u)=>3*(1-u)*(1-u)*u*b+3*(1-u)*u*u*c+u*u*u*d;
  const derivative=(a,b,c,d,u)=>3*(1-u)*(1-u)*(b-a)+6*(1-u)*u*(c-b)+3*u*u*(d-c);
  let u=t;
  for(let i=0;i<6;i++){const dx=bez(0,x1,x2,1,u)-t, der=derivative(0,x1,x2,1,u);if(Math.abs(der)<1e-5)break;u=Math.max(0,Math.min(1,u-dx/der))}
  return bez(0,y1,y2,1,u);
}
function valueAt(frames,t,dimensions=1){
  if(!frames?.length)return Array(dimensions).fill(0);
  if(t<=frames[0].t)return frames[0].v;
  if(t>=frames[frames.length-1].t)return frames[frames.length-1].v;
  let i=0;while(i<frames.length-1&&frames[i+1].t<t)i++;
  const a=frames[i],b=frames[i+1],span=b.t-a.t,raw=span?((t-a.t)/span):1;
  const eased=a.e?cubic(...a.e,raw):raw;
  return a.v.map((v,j)=>v+(b.v[j]-v)*eased);
}
function formatTime(seconds){const cs=Math.floor(seconds*100)%100,whole=Math.floor(seconds);return `${String(Math.floor(whole/60)).padStart(2,'0')}:${String(whole%60).padStart(2,'0')}:${String(cs).padStart(2,'0')}`}
function buildRulers(){
  const width=Math.max(900,Math.ceil(duration*55*zoom)+20);
  $('#timelineInner').style.width=(width+118)+'px';
  const ms=$('#rulerMs'),fr=$('#rulerFrames');ms.innerHTML='';fr.innerHTML='';
  const pxPerSec=55*zoom;
  for(let s=0;s<=Math.ceil(duration);s++){
    const tick=document.createElement('span');tick.className='tick';tick.style.left=(s*pxPerSec)+'px';tick.textContent=(s*1000)+' ms';ms.append(tick);
  }
  const frameStep=Math.max(1,Math.round(fps/10));
  for(let f=0;f<=Math.ceil(duration*fps);f+=frameStep){
    const tick=document.createElement('span');tick.className='tick';tick.style.left=(f/fps*pxPerSec)+'px';tick.textContent='F'+f;fr.append(tick);
  }
  $('#effectStrip')?.remove();
  const strip=document.querySelector('.effect-strip');if(strip)strip.style.width=(duration*pxPerSec)+'px';
  const clip=$('#clipBlock');clip.style.width=(duration*pxPerSec)+'px';
  updatePlayhead(pausedAt);
}
function updatePlayhead(time){
  const px=118+time*55*zoom;$('#playhead').style.left=px+'px';
  const frame=Math.floor(time*fps);$('#frameReadout').textContent=`Frame ${frame}`;
  $('#timeReadout').textContent=formatTime(time);
}
function loadMedia(file){
  if(!file)return;
  const isVideo=file.type.startsWith('video/')||/\.(mp4|mov)$/i.test(file.name);
  const isImage=file.type.startsWith('image/')||/\.(png|jpe?g|webp)$/i.test(file.name);
  if(!isVideo&&!isImage){toast('Please choose an MP4, MOV or image file.');return}
  if(media){if(media.tagName==='VIDEO'){media.pause();media.removeAttribute('src')}else URL.revokeObjectURL(media.src)}
  const url=URL.createObjectURL(file);
  if(isVideo){
    media=document.createElement('video');media.src=url;media.muted=true;media.loop=true;media.playsInline=true;media.preload='auto';mediaType='video';
    media.addEventListener('loadedmetadata',()=>{if(media.duration&&Number.isFinite(media.duration)){duration=Math.min(media.duration,600);$('#durationLabel').textContent=formatTime(duration);buildRulers()}drawFrame(pausedAt)},{once:true});
  }else{
    media=new Image();media.src=url;mediaType='image';media.onload=()=>drawFrame(0);
  }
  media.onload=media.onload||null;
  media.addEventListener?.('error',()=>toast('Could not load this media file.'));
  empty.hidden=true;canvas.hidden=false;$('#previewHud').hidden=false;
  $('#mediaInfo').textContent=file.name;$('#clipName').textContent=file.name;$('#clipBlock').hidden=false;$('.track-placeholder').hidden=true;
  $('#playBtn').disabled=false;$('#stepBtn').disabled=false;$('#exportBtn').disabled=false;
  $('#playState').textContent=isImage?'IMAGE':'VIDEO';
  if(isVideo)media.addEventListener('loadeddata',()=>drawFrame(0),{once:true});
  toast(isImage?'Image imported. Transparent PNG works best for Slam.':'Video imported.');
}
function drawFrame(time){
  if(!media||!template)return;
  const loc=valueAt(keyframes.location,time/duration,3),scale=valueAt(keyframes.scale,time/duration,2),rot=valueAt(keyframes.rotation,time/duration,1);
  ctx.clearRect(0,0,1080,1080);
  const w=media.videoWidth||media.naturalWidth||1080,h=media.videoHeight||media.naturalHeight||1080;
  if(!w||!h)return;
  const sx=720/w,sy=720/h;
  const baseW=w*sx,baseH=h*sy;
  ctx.save();ctx.translate(loc[0]||540,loc[1]||540);ctx.rotate((rot[0]||0)*Math.PI/180);
  ctx.scale(scale[0]||1,scale[1]||1);
  ctx.drawImage(media,-baseW/2,-baseH/2,baseW,baseH);ctx.restore();
  updatePlayhead(time);
}
function tick(now){
  if(!playing)return;
  const t=((now-startAt)/1000+pausedAt)%duration;
  if(mediaType==='video'&&media.readyState>=2){const target=t;if(Math.abs(media.currentTime-target)>.08)media.currentTime=target;}
  drawFrame(t);raf=requestAnimationFrame(tick);
}
function play(){
  if(!media)return;
  if(playing){playing=false;cancelAnimationFrame(raf);pausedAt=(performance.now()-startAt)/1000+pausedAt;pausedAt%=duration;$('#playBtn').textContent='▶';$('#playState').textContent='PAUSED';if(mediaType==='video')media.pause();return}
  playing=true;startAt=performance.now();$('#playBtn').textContent='Ⅱ';$('#playState').textContent='PLAYING';
  if(mediaType==='video')media.play().catch(()=>{});raf=requestAnimationFrame(tick);
}
async function exportVideo(){
  if(!media){toast('Import media before exporting.');return}
  if(!canvas.captureStream||!window.MediaRecorder){toast('Video export is not supported by this browser. Try a recent version of Chrome or Edge.');return}
  const wasPlaying=playing;if(wasPlaying)play();
  const stream=canvas.captureStream(fps);
  if(mediaType==='video'&&media.captureStream){try{media.muted=false;const audio=media.captureStream();audio.getAudioTracks().forEach(t=>stream.addTrack(t))}catch(e){}}
  const mime=['video/webm;codecs=vp9','video/webm;codecs=vp8','video/webm'].find(t=>MediaRecorder.isTypeSupported(t))||'';
  if(!mime){toast('No supported browser recording format found.');return}
  const chunks=[];let recorder;
  try{recorder=new MediaRecorder(stream,{mimeType:mime,videoBitsPerSecond:12000000});}catch(e){toast('Could not start export on this browser.');return}
  recorder.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};
  recorder.onstop=()=>{const blob=new Blob(chunks,{type:mime}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='impact-motion-export.webm';a.click();setTimeout(()=>URL.revokeObjectURL(url),3000);stream.getTracks().forEach(t=>t.stop());toast('Export complete. Saved as WebM.');};
  const start=pausedAt;recorder.start();const end=performance.now()+Math.min(duration,600)*1000;
  function exportTick(now){const t=(now-(end-Math.min(duration,600)*1000))/1000+start;if(t>=duration){drawFrame(duration-.001);recorder.stop();return}if(mediaType==='video'&&media.readyState>=2){try{media.currentTime=t}catch(e){}}drawFrame(t);requestAnimationFrame(exportTick)}
  requestAnimationFrame(exportTick);
}
fetch(xmlUrl).then(r=>{if(!r.ok)throw new Error('Template file missing');return r.text()}).then(parseTemplate).then(()=>{template=true;drawFrame(0)}).catch(e=>{console.error(e);toast('Could not load Slam template XML.')});
input.addEventListener('change',e=>loadMedia(e.target.files[0]));
stage.addEventListener('click',e=>{if(e.target.closest('.drop-overlay'))return;if(!media)input.click()});
['dragenter','dragover'].forEach(ev=>stage.addEventListener(ev,e=>{e.preventDefault();stage.classList.add('dragging')}));
['dragleave','drop'].forEach(ev=>stage.addEventListener(ev,e=>{e.preventDefault();if(ev==='dragleave'&&!stage.contains(e.relatedTarget))stage.classList.remove('dragging');if(ev==='drop'){stage.classList.remove('dragging');loadMedia(e.dataTransfer.files[0])}}));
$('#playBtn').addEventListener('click',play);
$('#restartBtn').addEventListener('click',()=>{pausedAt=0;if(mediaType==='video')media.currentTime=0;drawFrame(0);if(playing){playing=false;cancelAnimationFrame(raf);$('#playBtn').textContent='▶'}});
$('#stepBtn').addEventListener('click',()=>{pausedAt=Math.min(duration-.001,pausedAt+1/fps);if(mediaType==='video')media.currentTime=pausedAt;drawFrame(pausedAt)});
$('#exportBtn').addEventListener('click',exportVideo);
$('#settingsBtn').addEventListener('click',()=>{$('#settingsModal').hidden=false;fpsInput.value=fps});
$('#closeSettings').addEventListener('click',()=>$('#settingsModal').hidden=true);
$('#cancelSettings').addEventListener('click',()=>$('#settingsModal').hidden=true);
$('#saveSettings').addEventListener('click',()=>{const n=Number(fpsInput.value);if(!Number.isInteger(n)||n<30||n>240){toast('Frame rate must be a whole number from 30 to 240.');return}fps=n;$('#fpsLabel').textContent=fps+' FPS';$('#settingsModal').hidden=true;buildRulers();toast('Project frame rate updated.')});
$('#zoomIn').addEventListener('click',()=>{zoom=Math.min(3,zoom*1.25);buildRulers()});
$('#zoomOut').addEventListener('click',()=>{zoom=Math.max(.5,zoom/1.25);buildRulers()});
$('#fitBtn').addEventListener('click',()=>{if(media)drawFrame(pausedAt);});
document.querySelectorAll('.side-tab').forEach(btn=>btn.addEventListener('click',()=>{document.querySelectorAll('.side-tab').forEach(b=>b.classList.remove('active'));btn.classList.add('active');$('#libraryTitle').textContent=btn.dataset.tab[0].toUpperCase()+btn.dataset.tab.slice(1);if(btn.dataset.tab!=='slam')toast(`${btn.dataset.tab[0].toUpperCase()+btn.dataset.tab.slice(1)} templates are coming soon.`)}));
$('#slamTemplate').addEventListener('click',()=>{document.querySelectorAll('.side-tab').forEach(b=>b.classList.toggle('active',b.dataset.tab==='slam'));$('#libraryTitle').textContent='Slam';toast('Imortal Car Slam selected.')});
