import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { EncoderSimulator, MeasurementCalculator } from "../web/simulation.js";

test("web calculations match the desktop Python calculations", () => {
  const samples = [[0, 100000], [0.07, 100300], [1.4, 105000], [3.3, 110000],
    [4.1, 107000], [7.6, 100000], [8, 99000]];
  const python = spawnSync("python", ["-c", `
import json, sys
from dataclasses import asdict
from encoder_core import MeasurementCalculator
c = MeasurementCalculator(hardware_turns=1000)
print(json.dumps([asdict(c.add(*sample)) for sample in json.load(sys.stdin)]))
`], { input: JSON.stringify(samples), encoding: "utf8" });
  assert.equal(python.status, 0, python.stderr);
  const expected = JSON.parse(python.stdout);
  const calculator = new MeasurementCalculator();
  const keys = { smooth_speed: "smoothSpeed", average_speed: "averageSpeed",
    smooth_acceleration: "smoothAcceleration", average_acceleration: "averageAcceleration" };
  samples.forEach(([time, count], index) => {
    const actual = calculator.add(time, count);
    for (const [key, value] of Object.entries(expected[index])) {
      const observed = actual[keys[key] || key];
      if (value === null) assert.equal(observed, null, key);
      else assert.ok(Math.abs(value - observed) < 1e-10, `${key}: ${value} != ${observed}`);
    }
  });
});

test("simulator accelerates, reverses and respects pause-free simulation time", () => {
  const sim = new EncoderSimulator();
  sim.targetSpeed = 1;
  sim.accelerationLimit = 0.5;
  sim.advance(1);
  assert.equal(sim.motorSpeed, 0.5);
  assert.ok(Math.abs(sim.current.displacement - 0.25) < 0.000002);
  sim.targetSpeed = -1;
  sim.advance(1);
  assert.equal(sim.motorSpeed, 0);
  sim.advance(1);
  assert.equal(sim.motorSpeed, -0.5);
  assert.ok(sim.current.speed < 0);
  assert.ok(sim.current.travel > sim.current.displacement);
});

test("calculator unwraps both directions and rejects ambiguous gaps", () => {
  const calc = new MeasurementCalculator({ resolution: 10, circumference: 1, turns: 2, maxSpeed: 0.5 });
  calc.add(0, 19);
  assert.ok(Math.abs(calc.add(1, 1).speed - 0.2) < 1e-12);
  assert.ok(Math.abs(calc.add(2, 19).speed + 0.2) < 1e-12);
  assert.throws(() => calc.add(4, 1), /采样间隔过长/);
});
