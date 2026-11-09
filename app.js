import { EncoderSimulator } from "./simulation.js";

const $ = id => document.getElementById(id);
const target = $("target");
const ramp = $("ramp");
const pauseButton = $("pause");
let simulator;
let history;
let recording;
let running = true;
let lastTick = performance.now();
let lastDraw = 0;
const colors = { raw: "#e9b65a", smooth: "#5ae0a8", average: "#67b9ff" };

function reset() {
  simulator = new EncoderSimulator();
  simulator.targetSpeed = Number(target.value);
  simulator.accelerationLimit = Number(ramp.value);
  history = [simulator.current];
  recording = [simulator.current];
  lastTick = performance.now();
  running = true;
  pauseButton.textContent = "暂停";
  status("模拟运行中");
  render();
}

function status(text, kind = "") {
  $("status-text").textContent = text;
  $("status").className = `status ${kind}`;
}

function updateControls() {
  simulator.targetSpeed = Number(target.value);
  simulator.accelerationLimit = Number(ramp.value);
  $("target-value").textContent = `${simulator.targetSpeed.toFixed(2)} m/s`;
  $("ramp-value").textContent = `${simulator.accelerationLimit.toFixed(2)} m/s²`;
}
target.addEventListener("input", updateControls);
ramp.addEventListener("input", updateControls);
$("reset").addEventListener("click", reset);
$("export").addEventListener("click", () => {
  const columns = [
    ["elapsed_s", "elapsed"], ["count", "count"], ["displacement_m", "displacement"],
    ["travel_m", "travel"], ["speed_m_s", "speed"], ["smooth_speed_m_s", "smoothSpeed"],
    ["average_speed_3s_m_s", "averageSpeed"], ["acceleration_m_s2", "acceleration"],
    ["smooth_acceleration_m_s2", "smoothAcceleration"],
    ["average_acceleration_3s_m_s2", "averageAcceleration"],
  ];
  const lines = [columns.map(([label]) => label).join(",")];
  for (const point of recording) {
    lines.push(columns.map(([, key]) => point[key] ?? "").join(","));
  }
  const url = URL.createObjectURL(new Blob([lines.join("\r\n") + "\r\n"], { type: "text/csv" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `encoder_simulation_${new Date().toISOString().replaceAll(":", "-")}.csv`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
pauseButton.addEventListener("click", () => {
  running = !running;
  lastTick = performance.now();
  pauseButton.textContent = running ? "暂停" : "继续";
  status(running ? "模拟运行中" : "模拟已暂停", running ? "" : "paused");
});

function format(value, digits = 3) {
  return value === null ? "--" : value.toFixed(digits);
}

function drawChart(canvas, series) {
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!width || !height) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  const left = 52, right = 10, top = 14, bottom = 25;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const end = Math.max(30, simulator.current.elapsed);
  const start = end - 30;
  const visible = history.filter(point => point.elapsed >= start);
  const values = visible.flatMap(point => series.map(([key]) => point[key]))
    .filter(value => value !== null && Number.isFinite(value));
  let low = Math.min(0, ...values), high = Math.max(0, ...values);
  const padding = Math.max((high - low) * 0.12, 0.05);
  low -= padding;
  high += padding;
  const x = t => left + (t - start) / 30 * plotWidth;
  const y = value => top + (high - value) / (high - low) * plotHeight;
  ctx.font = "10px sans-serif";
  ctx.fillStyle = "#78909b";
  ctx.strokeStyle = "#273b45";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const py = top + plotHeight * i / 4;
    const value = high - (high - low) * i / 4;
    ctx.beginPath(); ctx.moveTo(left, py); ctx.lineTo(width - right, py); ctx.stroke();
    ctx.textAlign = "right"; ctx.fillText(value.toFixed(2), left - 8, py + 3);
  }
  ctx.textAlign = "center";
  for (let i = 0; i <= 3; i++) {
    const t = start + i * 10;
    ctx.fillText(`${t.toFixed(0)}s`, x(t), height - 5);
  }
  for (const [key, color] of series) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.7;
    ctx.beginPath();
    let active = false;
    for (const point of visible) {
      const value = point[key];
      if (value === null || !Number.isFinite(value)) { active = false; continue; }
      if (!active) ctx.moveTo(x(point.elapsed), y(value));
      else ctx.lineTo(x(point.elapsed), y(value));
      active = true;
    }
    ctx.stroke();
  }
}

function render() {
  const m = simulator.current;
  $("speed").textContent = format(m.speed);
  $("smooth-speed").textContent = format(m.smoothSpeed);
  $("average-speed").textContent = format(m.averageSpeed);
  $("smooth-acceleration").textContent = format(m.smoothAcceleration);
  $("acceleration").textContent = `${format(m.acceleration)} m/s²`;
  $("average-acceleration").textContent = `${format(m.averageAcceleration)} m/s²`;
  $("displacement").textContent = `${format(m.displacement)} m`;
  $("travel").textContent = `${format(m.travel)} m`;
  $("count").textContent = m.count.toLocaleString("zh-CN");
  $("elapsed").textContent = `${m.elapsed.toFixed(1)} s`;
  drawChart($("speed-chart"), [["speed", colors.raw], ["smoothSpeed", colors.smooth], ["averageSpeed", colors.average]]);
  drawChart($("acceleration-chart"), [["acceleration", colors.raw], ["smoothAcceleration", colors.smooth], ["averageAcceleration", colors.average]]);
  drawChart($("distance-chart"), [["displacement", colors.average], ["travel", colors.raw]]);
}

function tick() {
  const now = performance.now();
  if (!running) return;
  const dt = (now - lastTick) / 1000;
  lastTick = now;
  if (dt <= 0) return;
  try {
    const point = simulator.advance(dt);
    history.push(point);
    recording.push(point);
    if (recording.length > 72000) recording.shift();
    while (history.length > 1 && history[1].elapsed < simulator.current.elapsed - 30) history.shift();
    if (now - lastDraw >= 150) { render(); lastDraw = now; }
  } catch (error) {
    running = false;
    pauseButton.disabled = true;
    status(error.message, "error");
  }
}

window.addEventListener("resize", render);
$("reset").addEventListener("click", () => { pauseButton.disabled = false; });
reset();
setInterval(tick, 50);
