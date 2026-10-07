const state = {
  track: null, points: [], transform: null, timer: null, dashboardTimer: null,
  replayTimer: null, pollMs: 2000, chartHistory: [], mode: "live",
  replayPoints: [], replayIndex: 0, playing: false, historyMapHour: null,
  frameRequestSequence: 0
};
const el = (id) => document.getElementById(id);

function setBadge(text, kind) { el("connectionBadge").textContent = text; el("connectionBadge").className = `badge ${kind}`; }
function stopTimers() { clearTimeout(state.timer); clearTimeout(state.dashboardTimer); clearTimeout(state.replayTimer); state.timer = null; state.dashboardTimer = null; state.replayTimer = null; }
function makeTransform(points) {
  const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const pad = 70, w = 1000, h = 700, sx = (w - 2*pad) / Math.max(maxX-minX,1e-9), sy = (h-2*pad) / Math.max(maxY-minY,1e-9), scale = Math.min(sx,sy);
  const ox = (w-(maxX-minX)*scale)/2, oy = (h-(maxY-minY)*scale)/2;
  return ([lon,lat]) => [ox+(lon-minX)*scale, h-(oy+(lat-minY)*scale)];
}
function pathFor(points) { return points.map((p,i) => { const [x,y]=state.transform(p); return `${i?'L':'M'}${x.toFixed(2)},${y.toFixed(2)}`; }).join(' '); }
function colorFor(value,min,max) { const t=Math.max(0,Math.min(1,(value-min)/Math.max(max-min,1e-9))); const hue=260-(210*t); return `hsl(${hue} 88% ${48+12*t}%)`; }

async function loadTrack(track) {
  stopTimers(); state.chartHistory=[]; stopReplay(); setBadge("Loading","waiting");
  const response=await fetch(`/api/track/${encodeURIComponent(track)}`); if(!response.ok) throw new Error((await response.json()).detail||"Track unavailable");
  const data=await response.json(); state.track=track; state.points=data.points; state.transform=makeTransform(data.points);
  el("irradianceSvg").setAttribute("viewBox","0 0 1000 700");el("racingSvg").setAttribute("viewBox","0 0 1000 700");
  const path=pathFor(data.points); el("irradianceShadow").setAttribute("d",path); el("racingTrackShadow").setAttribute("d",path); el("racingTrack").setAttribute("d",path);
  el("trackName").textContent=data.name; el("trackLength").textContent=`${(data.length_m/1000).toFixed(2)} km`;
  await refreshDashboard();
  if(state.mode === "live") await pollLive(); else setBadge("History", "waiting");
}

function placeCars(data) {
  for (const id of ["irradianceCar","racingCar"]) {
    const marker=el(id); if(data.valid&&data.on_track){ const [x,y]=state.transform([data.longitude,data.latitude]); marker.setAttribute("transform",`translate(${x},${y})`); marker.classList.remove("hidden"); } else marker.classList.add("hidden");
  }
  el("irradianceEmpty").classList.toggle("hidden",data.valid&&data.on_track);
}

function showPosition(data, historical=false) {
  el("latitude").textContent=data.latitude.toFixed(6); el("longitude").textContent=data.longitude.toFixed(6);
  el("distance").textContent=historical?"Replay":(data.distance_to_track_km==null?"—":`${data.distance_to_track_km.toFixed(2)} km`);
  el("sampleAge").textContent=historical?"Historical":`${data.age_seconds.toFixed(1)} s`;
  el("timestamp").textContent=new Date(data.timestamp).toLocaleString(); placeCars(data);
}

async function pollLive() {
  if(state.mode !== "live") return;
  try {
    const response=await fetch(`/api/live/${encodeURIComponent(state.track)}`,{cache:"no-store"}); if(!response.ok) throw new Error((await response.json()).detail||"Telemetry unavailable");
    const data=await response.json(); showPosition(data);
    if(data.stale){setBadge("Stale data","stale");el("statusMessage").textContent="InfluxDB responded, but the latest GPS sample is older than the configured live threshold.";}
    else if(!data.on_track){setBadge("Off track","stale");el("statusMessage").textContent="GPS is valid but not near the selected circuit.";}
    else{setBadge("Live","live");el("statusMessage").textContent="Receiving the latest GPS position from InfluxDB.";}
  } catch(error){setBadge("No telemetry","error");el("statusMessage").textContent=error.message; el("irradianceCar").classList.add("hidden");el("racingCar").classList.add("hidden");}
  finally { if(state.mode === "live") state.timer=setTimeout(pollLive,state.pollMs); }
}

function drawIrradiance(data) {
  const group=el("irradianceSegments"); group.replaceChildren(); const values=data.irradiance, min=data.irradiance_min_wm2??0, max=data.irradiance_max_wm2??1;
  for(let i=1;i<values.length;i++){const [x1,y1]=state.transform(values[i-1]),[x2,y2]=state.transform(values[i]);const line=document.createElementNS("http://www.w3.org/2000/svg","line");line.setAttribute("x1",x1);line.setAttribute("y1",y1);line.setAttribute("x2",x2);line.setAttribute("y2",y2);line.setAttribute("stroke",colorFor(values[i][2]??0,min,max));line.setAttribute("class","irr-segment");group.appendChild(line);}
  el("irradianceRange").textContent=`${min.toFixed(0)}–${max.toFixed(0)} W/m²`;
}
function drawRacing(data) { if(data.racing_line_available){el("racingLine").setAttribute("d",pathFor(data.racing_line));el("racingLine").classList.remove("hidden");el("racingStatus").textContent="Generated ideal line";}else{el("racingLine").classList.add("hidden");el("racingStatus").textContent="Racing line not generated for this circuit";} }
function drawSectors(sectors){const root=el("sectorChart");root.replaceChildren();const max=Math.max(...sectors.map(s=>s.avg_irradiance_wm2||0),1);for(const s of sectors){const row=document.createElement("div");row.className="bar-row";row.innerHTML=`<span>${s.name}</span><div class="bar-track"><i style="width:${100*(s.avg_irradiance_wm2||0)/max}%"></i></div><strong>${(s.avg_irradiance_wm2||0).toFixed(1)} W/m²</strong>`;root.appendChild(row);} }
function drawPower(){const values=state.chartHistory,w=Math.max(900,values.length*38),h=380,pad=45,max=Math.max(...values.flatMap(v=>[v.calc||0,v.mppt||0]),1),svg=el("powerChart"),wrap=svg.closest(".chart-wrap"),wasAtEnd=wrap.scrollWidth-wrap.scrollLeft-wrap.clientWidth<35;svg.setAttribute("viewBox",`0 0 ${w} ${h}`);svg.style.width=`${w}px`;const mk=key=>values.map((v,i)=>`${i?'L':'M'}${pad+i*(w-2*pad)/Math.max(values.length-1,1)},${h-pad-(v[key]||0)*(h-2*pad)/max}`).join(' ');el("calculatedLine").setAttribute("d",mk("calc"));el("measuredLine").setAttribute("d",mk("mppt"));if(wasAtEnd)requestAnimationFrame(()=>{wrap.scrollLeft=wrap.scrollWidth;});}

async function refreshDashboard(){
  try{const response=await fetch(`/api/dashboard/${encodeURIComponent(state.track)}`,{cache:"no-store"});if(!response.ok)throw new Error((await response.json()).detail||"Dashboard data unavailable");const data=await response.json();drawIrradiance(data);drawRacing(data);drawSectors(data.sectors);el("weatherSource").textContent=data.weather_source;el("calculatedPower").textContent=data.comparison_available?`${data.calculated_solar_power_w.toFixed(1)} W`:data.comparison_status;el("measuredPower").textContent=data.measured_mppt_power_w==null?"No sample":`${data.measured_mppt_power_w.toFixed(1)} W`;el("mpptCount").textContent=`${data.active_mppt_count} active`;if(data.comparison_available){state.chartHistory.push({time:data.timestamp,calc:data.calculated_solar_power_w,mppt:data.measured_mppt_power_w});state.chartHistory=state.chartHistory.slice(-300);drawPower();}}
  catch(error){el("weatherSource").textContent="Unavailable";el("racingStatus").textContent=error.message;}
  finally{if(state.mode === "live") state.dashboardTimer=setTimeout(refreshDashboard,30000);}
}

function stopReplay() { clearTimeout(state.replayTimer); state.replayTimer=null; state.playing=false; if(el("playPause")) el("playPause").textContent="Play"; }
function showReplayPoint(index) {
  if(!state.replayPoints.length) return;
  state.replayIndex=Math.max(0,Math.min(index,state.replayPoints.length-1)); const point=state.replayPoints[state.replayIndex];
  el("replaySlider").value=state.replayIndex; el("replayCounter").textContent=`${state.replayIndex+1} / ${state.replayPoints.length} · ${new Date(point.timestamp).toLocaleString()}`;
  showPosition({...point,valid:true,on_track:true},true); setBadge("Replay","live"); el("statusMessage").textContent="Showing historical GPS telemetry from InfluxDB.";
  refreshHistoryFrame(point, state.replayIndex);
}
async function refreshHistoryFrame(point, index) {
  const hour=new Date(point.timestamp);hour.setUTCMinutes(0,0,0);const hourKey=hour.toISOString();
  const includeMap=state.historyMapHour!==hourKey;const sequence=++state.frameRequestSequence;
  const params=new URLSearchParams({timestamp:point.timestamp,latitude:String(point.latitude),longitude:String(point.longitude),include_map:String(includeMap)});
  try{const response=await fetch(`/api/history-frame/${encodeURIComponent(state.track)}?${params}`,{cache:"no-store"});if(!response.ok)throw new Error((await response.json()).detail||"Historical solar data unavailable");const data=await response.json();if(sequence!==state.frameRequestSequence||index!==state.replayIndex)return;if(data.irradiance){drawIrradiance(data);state.historyMapHour=hourKey;}el("weatherSource").textContent=data.weather_source;el("calculatedPower").textContent=`${data.calculated_solar_power_w.toFixed(1)} W`;el("measuredPower").textContent=data.measured_mppt_power_w==null?"No MPPT sample":`${data.measured_mppt_power_w.toFixed(1)} W`;el("mpptCount").textContent=`${data.active_mppt_count} active`;state.chartHistory.push({time:data.timestamp,calc:data.calculated_solar_power_w,mppt:data.measured_mppt_power_w});state.chartHistory=state.chartHistory.slice(-300);drawPower();el("historyError").classList.add("hidden");}
  catch(error){if(sequence===state.frameRequestSequence&&index===state.replayIndex){el("historyError").textContent=error.message;el("historyError").classList.remove("hidden");}}
}
function scheduleNextReplay() {
  if(!state.playing) return;
  if(state.replayIndex >= state.replayPoints.length-1){stopReplay();return;}
  const current=new Date(state.replayPoints[state.replayIndex].timestamp).getTime(),next=new Date(state.replayPoints[state.replayIndex+1].timestamp).getTime();const historicalSeconds=Math.max((next-current)/1000,0);const speed=Number(el("replaySpeed").value)||60;const delay=Math.max(80,Math.min(historicalSeconds*1000/speed,10000));state.replayTimer=setTimeout(()=>{showReplayPoint(state.replayIndex+1);scheduleNextReplay();},delay);
}
function toggleReplay() { if(!state.replayPoints.length)return; state.playing=!state.playing;el("playPause").textContent=state.playing?"Pause":"Play";clearTimeout(state.replayTimer);if(state.playing){if(state.replayIndex>=state.replayPoints.length-1)showReplayPoint(0);scheduleNextReplay();} }

async function loadHistory() {
  stopReplay(); const startValue=el("historyStart").value, stopValue=el("historyStop").value;
  if(!startValue||!stopValue){showFatal(new Error("Choose both start and stop date/time."));return;}
  const start=new Date(startValue), stop=new Date(stopValue); if(stop<=start){showFatal(new Error("The end must be after the start."));return;}
  el("historyError").classList.add("hidden"); setBadge("Loading history","waiting"); el("loadHistory").disabled=true;
  try{const url=`/api/history/${encodeURIComponent(state.track)}?start=${encodeURIComponent(start.toISOString())}&stop=${encodeURIComponent(stop.toISOString())}`;const response=await fetch(url,{cache:"no-store"});if(!response.ok)throw new Error((await response.json()).detail||"History unavailable");const data=await response.json();state.replayPoints=data.points;state.replayIndex=0;state.historyMapHour=null;state.chartHistory=[];el("replaySlider").max=Math.max(data.points.length-1,0);el("replaySlider").disabled=!data.points.length;el("playPause").disabled=!data.points.length;if(!data.points.length){setBadge("No samples","stale");el("replayCounter").textContent="No GPS samples near this circuit";el("statusMessage").textContent="InfluxDB returned no positions for the selected interval and circuit.";}else showReplayPoint(0);}
  catch(error){showFatal(error);}finally{el("loadHistory").disabled=false;}
}

function setDefaultHistoryRange(){const stop=new Date(),start=new Date(stop.getTime()-60*60*1000);const local=v=>new Date(v.getTime()-v.getTimezoneOffset()*60000).toISOString().slice(0,16);el("historyStart").value=local(start);el("historyStop").value=local(stop);}
function changeMode(mode){stopTimers();stopReplay();state.frameRequestSequence++;state.mode=mode;el("historyControls").classList.toggle("hidden",mode!=="history");el("mapModeLabel").textContent=mode==="live"?"LIVE IRRADIANCE MAP":"HISTORICAL TRACK MAP";el("telemetryModeLabel").textContent=mode==="live"?"LIVE TELEMETRY":"HISTORICAL TELEMETRY";if(mode==="live"){setBadge("Connecting","waiting");refreshDashboard();pollLive();}else{setBadge("History","waiting");el("statusMessage").textContent="Choose an interval, then load GPS history from InfluxDB.";}}

function switchView(view) {
  el("mapView").classList.toggle("hidden", view !== "map");
  el("racingView").classList.toggle("hidden", view !== "racing");
  document.querySelectorAll(".view-tab").forEach(button => {
    button.classList.toggle("active", button.dataset.view === view);
    button.setAttribute("aria-current", button.dataset.view === view ? "page" : "false");
  });
}

function setupMapZoom() {
  document.querySelectorAll(".map-zoom").forEach(controls => {
    const svg=el(controls.dataset.zoomTarget);const original=[0,0,1000,700];
    const zoom=factor=>{const box=svg.getAttribute("viewBox").split(/\s+/).map(Number);const newWidth=Math.max(180,Math.min(1000,box[2]/factor));const newHeight=newWidth*.7;const centerX=box[0]+box[2]/2,centerY=box[1]+box[3]/2;let x=Math.max(0,Math.min(1000-newWidth,centerX-newWidth/2));let y=Math.max(0,Math.min(700-newHeight,centerY-newHeight/2));svg.setAttribute("viewBox",`${x} ${y} ${newWidth} ${newHeight}`);};
    controls.addEventListener("click",event=>{const action=event.target.dataset.zoom;if(action==="in")zoom(1.35);else if(action==="out")zoom(1/1.35);else if(action==="reset")svg.setAttribute("viewBox",original.join(" "));});
    svg.addEventListener("wheel",event=>{event.preventDefault();zoom(event.deltaY<0?1.18:1/1.18);},{passive:false});
  });
}

function setupChartPan(){const wrap=el("powerChart").closest(".chart-wrap");let dragging=false,startX=0,startScroll=0;wrap.addEventListener("pointerdown",event=>{dragging=true;startX=event.clientX;startScroll=wrap.scrollLeft;wrap.setPointerCapture(event.pointerId);wrap.classList.add("dragging");});wrap.addEventListener("pointermove",event=>{if(dragging)wrap.scrollLeft=startScroll-(event.clientX-startX);});const stop=()=>{dragging=false;wrap.classList.remove("dragging");};wrap.addEventListener("pointerup",stop);wrap.addEventListener("pointercancel",stop);wrap.addEventListener("wheel",event=>{if(Math.abs(event.deltaY)>Math.abs(event.deltaX)){event.preventDefault();wrap.scrollLeft+=event.deltaY;}},{passive:false});}

async function start(){try{const response=await fetch("/api/config");if(!response.ok)throw new Error("CSI configuration unavailable");const config=await response.json();state.pollMs=Math.max(config.poll_seconds*1000,500);const select=el("trackSelect");config.tracks.forEach(({key,name})=>select.add(new Option(name,key)));select.value=config.default_track;select.addEventListener("change",()=>loadTrack(select.value).catch(showFatal));document.querySelectorAll(".view-tab").forEach(button=>button.addEventListener("click",()=>switchView(button.dataset.view)));el("modeSelect").addEventListener("change",event=>changeMode(event.target.value));el("loadHistory").addEventListener("click",loadHistory);el("playPause").addEventListener("click",toggleReplay);el("replaySpeed").addEventListener("change",()=>{if(state.playing){clearTimeout(state.replayTimer);scheduleNextReplay();}});el("replaySlider").addEventListener("input",event=>{stopReplay();showReplayPoint(Number(event.target.value));});setupMapZoom();setupChartPan();setDefaultHistoryRange();switchView("map");await loadTrack(select.value);}catch(error){showFatal(error);}}
function showFatal(error){setBadge("Error","error");el("statusMessage").textContent=error.message;if(state.mode==="history"){el("historyError").textContent=error.message;el("historyError").classList.remove("hidden");}}
start();
