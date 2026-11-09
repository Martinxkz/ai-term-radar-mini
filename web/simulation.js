// This module mirrors encoder_core.py; it runs entirely in the browser.
export const RESOLUTION = 131072;
export const CIRCUMFERENCE = 0.2;
const TURNS = 1000;

function interpolate(history, target) {
  while (history.length > 2 && history[1][0] <= target) history.shift();
  if (history[0][0] > target) return null;
  const [t0, v0] = history[0];
  if (t0 === target) return v0;
  const [t1, v1] = history[1];
  return v0 + (v1 - v0) * (target - t0) / (t1 - t0);
}

export class MeasurementCalculator {
  constructor({ resolution = RESOLUTION, circumference = CIRCUMFERENCE,
                turns = TURNS, maxSpeed = 5, smoothingSeconds = 0.25 } = {}) {
    this.metresPerCount = circumference / resolution;
    this.modulus = turns * resolution;
    this.maxSpeed = maxSpeed;
    this.smoothingSeconds = smoothingSeconds;
    this.startTime = null;
    this.lastTime = null;
    this.lastCount = null;
    this.lastSpeed = null;
    this.lastSmoothSpeed = null;
    this.lastDt = null;
    this.distance = 0;
    this.travel = 0;
    this.distanceHistory = [];
    this.speedHistory = [];
  }

  add(timestamp, count) {
    if (!Number.isFinite(timestamp) || !Number.isInteger(count) ||
        count < 0 || count >= this.modulus) throw new Error("模拟计数或时间无效");
    if (this.startTime === null) {
      this.startTime = this.lastTime = timestamp;
      this.lastCount = count;
      this.distanceHistory.push([0, 0]);
      return { elapsed: 0, count, displacement: 0, travel: 0, speed: null,
        smoothSpeed: null, averageSpeed: null, acceleration: null,
        smoothAcceleration: null, averageAcceleration: null };
    }
    const dt = timestamp - this.lastTime;
    if (!(dt > 0)) throw new Error("模拟采样时间未递增");
    if (this.maxSpeed * dt >= this.modulus * this.metresPerCount / 2) {
      throw new Error("采样间隔过长，无法判断计数回绕。请重置模拟。");
    }
    const rawDelta = count - this.lastCount;
    const delta = ((rawDelta + this.modulus / 2) % this.modulus + this.modulus) %
      this.modulus - this.modulus / 2;
    const deltaDistance = delta * this.metresPerCount;
    const speed = deltaDistance / dt;
    if (Math.abs(speed) > this.maxSpeed) throw new Error("模拟速度超过合理上限");
    const elapsed = timestamp - this.startTime;
    this.distance += deltaDistance;
    this.travel += Math.abs(deltaDistance);
    const alpha = -Math.expm1(-dt / this.smoothingSeconds);
    const smoothSpeed = this.lastSmoothSpeed === null ? speed :
      this.lastSmoothSpeed + alpha * (speed - this.lastSmoothSpeed);
    const acceleration = this.lastSpeed === null ? null :
      (speed - this.lastSpeed) / ((dt + this.lastDt) / 2);
    const smoothAcceleration = this.lastSmoothSpeed === null ? null :
      (smoothSpeed - this.lastSmoothSpeed) / dt;
    this.distanceHistory.push([elapsed, this.distance]);
    this.speedHistory.push([elapsed, smoothSpeed]);
    const target = elapsed - 3;
    let averageSpeed = null;
    let averageAcceleration = null;
    if (target >= 0) {
      const oldDistance = interpolate(this.distanceHistory, target);
      if (oldDistance !== null) averageSpeed = (this.distance - oldDistance) / 3;
      const oldSpeed = interpolate(this.speedHistory, target);
      if (oldSpeed !== null) averageAcceleration = (smoothSpeed - oldSpeed) / 3;
    }
    this.lastTime = timestamp;
    this.lastCount = count;
    this.lastSpeed = speed;
    this.lastSmoothSpeed = smoothSpeed;
    this.lastDt = dt;
    return { elapsed, count, displacement: this.distance, travel: this.travel,
      speed, smoothSpeed, averageSpeed, acceleration, smoothAcceleration,
      averageAcceleration };
  }
}

export class EncoderSimulator {
  constructor() {
    this.calculator = new MeasurementCalculator();
    this.time = 0;
    this.motorSpeed = 0;
    this.positionCounts = TURNS * RESOLUTION / 2;
    this.targetSpeed = 0.6;
    this.accelerationLimit = 0.3;
    this.current = this.calculator.add(0, this.positionCounts);
  }

  advance(dt) {
    if (!Number.isFinite(dt) || dt <= 0) throw new Error("模拟时间间隔无效");
    const difference = this.targetSpeed - this.motorSpeed;
    const direction = Math.sign(difference);
    const rampTime = Math.min(dt, Math.abs(difference) / this.accelerationLimit);
    const endSpeed = this.motorSpeed + direction * this.accelerationLimit * rampTime;
    const distance = (this.motorSpeed + endSpeed) * rampTime / 2 +
      endSpeed * (dt - rampTime);
    this.positionCounts += distance / CIRCUMFERENCE * RESOLUTION;
    this.motorSpeed = endSpeed;
    this.time += dt;
    const modulus = TURNS * RESOLUTION;
    const count = ((Math.round(this.positionCounts) % modulus) + modulus) % modulus;
    this.current = this.calculator.add(this.time, count);
    return this.current;
  }
}
