export interface Point {
  x: number;
  y: number;
}
export interface Camera extends Point {
  zoom: number;
}
export const CARD_WIDTH = 240;
export const CARD_HEIGHT = 320;
export function screenToWorld(point: Point, camera: Camera): Point {
  return {
    x: (point.x - camera.x) / camera.zoom,
    y: (point.y - camera.y) / camera.zoom,
  };
}
export function worldToScreen(point: Point, camera: Camera): Point {
  return {
    x: point.x * camera.zoom + camera.x,
    y: point.y * camera.zoom + camera.y,
  };
}
export function zoomAt(camera: Camera, point: Point, factor: number): Camera {
  const world = screenToWorld(point, camera);
  const zoom = Math.max(0.2, Math.min(2.5, camera.zoom * factor));
  return { x: point.x - world.x * zoom, y: point.y - world.y * zoom, zoom };
}
export interface CardTransform extends Point {
  rotation: number;
}

// Infinite repeating tiles: their centers are world points (24*n, 24*m).
// Keep fractional pixels so panning and cursor-anchored zoom stay continuous.
export const GRID_SPACING = 24;
export function gridStyle(camera: Camera) {
  const origin = worldToScreen({ x: 0, y: 0 }, camera);
  const spacing = GRID_SPACING * camera.zoom;
  const radius = camera.zoom;
  return {
    backgroundSize: `${spacing}px ${spacing}px`,
    backgroundPosition: `${origin.x - spacing / 2}px ${origin.y - spacing / 2}px`,
    backgroundImage: `radial-gradient(#b9c6b8 ${radius}px, transparent ${radius}px)`,
  };
}
