import test from "node:test";
import assert from "node:assert/strict";
import {
  gridStyle,
  GRID_SPACING,
  screenToWorld,
  worldToScreen,
  zoomAt,
} from "../src/board/geometry.ts";
test("drop coordinates round-trip through a panned and zoomed camera", () => {
  const camera = { x: -120, y: 340, zoom: 0.6 };
  const p = { x: 420, y: 166 };
  assert.deepEqual(worldToScreen(screenToWorld(p, camera), camera), p);
});
test("zoom preserves world point under pointer and stays bounded", () => {
  const c = { x: 90, y: 50, zoom: 1 };
  const p = { x: 270, y: 310 };
  assert.deepEqual(screenToWorld(p, zoomAt(c, p, 2)), screenToWorld(p, c));
  assert.equal(zoomAt(c, p, 100).zoom, 2.5);
  assert.equal(zoomAt(c, p, 0.001).zoom, 0.2);
});
test("drop coordinates preserve negative world positions", () => {
  assert.deepEqual(screenToWorld({ x: 0, y: 0 }, { x: 100, y: 200, zoom: 2 }), {
    x: -50,
    y: -100,
  });
});

test("grid dots use worldToScreen across negative, fractional and distant cameras", () => {
  for (const camera of [
    { x: 300, y: 150, zoom: 1 },
    { x: -124.375, y: 67.125, zoom: 0.2 },
    { x: 100000.125, y: -99999.75, zoom: 2.5 },
    zoomAt({ x: -40, y: 80, zoom: 1 }, { x: 431, y: 297 }, 1.37),
  ]) {
    const style = gridStyle(camera);
    const [sx, sy] = style.backgroundSize.split(" ").map(parseFloat);
    const [x, y] = style.backgroundPosition.split(" ").map(parseFloat);
    assert.equal(sx, GRID_SPACING * camera.zoom);
    assert.equal(sy, sx);
    assert.ok(style.backgroundImage.includes(`${camera.zoom}px`));
    for (const n of [-100, -1, 0, 1, 100]) {
      const dot = worldToScreen(
        { x: n * GRID_SPACING, y: -n * GRID_SPACING },
        camera,
      );
      assert.ok(Math.abs(x + sx / 2 + n * sx - dot.x) < 1e-8);
      assert.ok(Math.abs(y + sy / 2 - n * sy - dot.y) < 1e-8);
    }
  }
});
test("grid follows fractional pan without rounding or tile-boundary jumps", () => {
  const before = gridStyle({ x: -0.125, y: 23.875, zoom: 1 });
  const after = gridStyle({ x: 0.125, y: 24.125, zoom: 1 });
  const a = before.backgroundPosition.split(" ").map(parseFloat);
  const b = after.backgroundPosition.split(" ").map(parseFloat);
  assert.equal(b[0] - a[0], 0.25);
  assert.equal(b[1] - a[1], 0.25);
});
