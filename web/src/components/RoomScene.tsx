import { Edges, Html, Line, OrbitControls, useGLTF } from "@react-three/drei";
import { addAfterEffect, Canvas, useFrame, useThree } from "@react-three/fiber";
import { Component, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import * as THREE from "three";
import { insideSpans, pointInPolygon, polygonCentroid, viewCorner } from "../lib/shapes";
import { trackModels } from "../lib/model-loading";
import type { ManifestAsset, Opening, Placement, SceneTiming, Vec2 } from "../lib/types";

// Plan frame: metres, Z-up, origin at the room's min corner. three.js: Y-up, room centred on the origin.
// Conversion follows the legacy viewer (web-pipeline lib/scene3d/signatures.ts).

export interface RoomGeometry {
  room_area: Vec2;
  room_vertices: Vec2[];
  wall_height: number;
  room_doors: Opening[];
  room_windows: Opening[];
}

export interface Furnishing {
  asset: ManifestAsset;
  placement: Placement;
}

/**
 * Generation-time motion, each tied to what the pipeline is doing:
 * `rise` walls grow out of the floor; `scan` a beam sweeps the floor and draws the grid (room reading);
 * `clearance` door clearance zones glow (openings found); `walkway` a path flows in from the door (fit check);
 * `sway` the camera drifts so the wait never looks frozen.
 */
export interface SceneEffects {
  rise?: boolean;
  scan?: boolean;
  clearance?: boolean;
  walkway?: boolean;
  sway?: boolean;
}

export interface Callout {
  id: string;
  at: [number, number, number]; // plan x, y, z
  label: string;
}

const PALETTE = {
  accent: "#5b5ff0",
  wall: "#f4f4f2",
  floor: "#e7d6b8",
  door: "#5b3a26",
  frame: "#d9d9d6",
  glass: "#cfe0ee",
};

const S3_HOST = "https://livinit-storage-prod.s3.us-east-2.amazonaws.com";
// The bucket sends no CORS headers; the dev server proxies /s3 to it (vite.config.ts).
const proxied = (url: string) => (url.startsWith(S3_HOST) ? `/s3${url.slice(S3_HOST.length)}` : url);

const prefersReducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const easeOutCubic = (t: number) => 1 - Math.pow(1 - t, 3);
const clamp01 = (t: number) => Math.min(1, Math.max(0, t));

/** Seconds since the first frame this hook ran in; null before it. */
function useAge() {
  const born = useRef<number | null>(null);
  return (elapsed: number) => elapsed - (born.current ??= elapsed);
}

function centerOf(geometry: RoomGeometry): Vec2 {
  return [geometry.room_area[0] / 2, geometry.room_area[1] / 2];
}

function toWorld([x, y, z]: [number, number, number], [cx, cy]: Vec2): THREE.Vector3 {
  return new THREE.Vector3(x - cx, z, -(y - cy));
}

/** Legacy `frontViewToYaw`: which raw model axis faces the plan's front. */
function frontViewToYaw(frontView: number | null): number {
  switch ((frontView ?? 0) % 4) {
    case 1:
      return 0;
    case 2:
      return Math.PI / 2;
    case 3:
      return Math.PI;
    default:
      return -Math.PI / 2;
  }
}

interface Cut {
  kind: "door" | "window";
  u: number; // centre along the wall from the wall origin
  width: number;
  sill: number;
  height: number;
}

interface Wall {
  origin: THREE.Vector3;
  rotationY: number;
  length: number;
  cuts: Cut[];
  start: Vec2; // plan endpoints
  end: Vec2;
  normal: THREE.Vector3; // world inward normal
}

function distanceToSegment(p: Vec2, a: Vec2, b: Vec2): number {
  const [dx, dy] = [b[0] - a[0], b[1] - a[1]];
  const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy || 1)));
  return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
}

/**
 * One wall per polygon edge. Each wall's local +Z faces into the room, so a single-sided
 * material hides the walls between the camera and the room (a dollhouse cutaway at any angle).
 */
function buildWalls(geometry: RoomGeometry): Wall[] {
  const vertices = geometry.room_vertices;
  const center = centerOf(geometry);
  const signedArea = vertices.reduce((sum, [x1, y1], i) => {
    const [x2, y2] = vertices[(i + 1) % vertices.length];
    return sum + x1 * y2 - x2 * y1;
  }, 0);
  const edges = vertices.map((a, i) => [a, vertices[(i + 1) % vertices.length]] as const);
  const nearestEdge = (point: Vec2) =>
    edges.reduce((best, [a, b], i) => (distanceToSegment(point, a, b) < distanceToSegment(point, ...edges[best]) ? i : best), 0);

  const openings = [
    ...geometry.room_doors.map((opening) => ({ opening, kind: "door" as const })),
    ...geometry.room_windows.map((opening) => ({ opening, kind: "window" as const })),
  ];

  return edges.map(([a, b], index) => {
    const length = Math.hypot(b[0] - a[0], b[1] - a[1]);
    const [dx, dy] = [(b[0] - a[0]) / length, (b[1] - a[1]) / length];
    const [nx, ny] = signedArea > 0 ? [-dy, dx] : [dy, -dx]; // inward normal in plan
    const rotationY = Math.atan2(nx, -ny); // local +Z -> world (nx, 0, -ny)
    const localX = new THREE.Vector3(Math.cos(rotationY), 0, -Math.sin(rotationY));
    const start = toWorld([a[0], a[1], 0], center);
    const end = toWorld([b[0], b[1], 0], center);
    const origin = localX.dot(end.clone().sub(start)) > 0 ? start : end;
    const cuts = openings
      .filter(({ opening }) => nearestEdge(opening.center) === index)
      .map(({ opening, kind }): Cut => {
        const point = toWorld([opening.center[0], opening.center[1], 0], center);
        const width = Math.max(opening.width, opening.depth, 0.3);
        const sill = kind === "door" ? 0 : (opening.sill_height ?? 0.9);
        const height = Math.min(opening.height ?? (kind === "door" ? 2.1 : 1.2), geometry.wall_height - sill - 0.05);
        const u = Math.max(width / 2, Math.min(length - width / 2, localX.dot(point.sub(origin))));
        return { kind, u, width, sill, height };
      });
    return { origin, rotationY, length, cuts, start: a, end: b, normal: new THREE.Vector3(nx, 0, -ny) };
  });
}

function WallMesh({ wall, height, rise, delay }: { wall: Wall; height: number; rise: boolean; delay: number }) {
  // The wall face culls itself from behind; its door and window parts follow it.
  const openings = useRef<THREE.Group>(null);
  const grow = useRef<THREE.Group>(null);
  const age = useAge();
  const still = useMemo(() => !rise || prefersReducedMotion(), [rise]);
  useFrame(({ camera, clock }) => {
    if (openings.current) openings.current.visible = wall.normal.dot(camera.position.clone().sub(wall.origin)) > 0;
    if (grow.current) grow.current.scale.y = still ? 1 : 0.001 + 0.999 * easeOutCubic(clamp01((age(clock.elapsedTime) - delay) / 0.9));
  });
  const shape = useMemo(() => {
    const outline = new THREE.Shape([
      new THREE.Vector2(0, 0),
      new THREE.Vector2(wall.length, 0),
      new THREE.Vector2(wall.length, height),
      new THREE.Vector2(0, height),
    ]);
    for (const cut of wall.cuts) {
      const [x0, x1, y0, y1] = [cut.u - cut.width / 2, cut.u + cut.width / 2, cut.sill + 0.001, cut.sill + cut.height];
      outline.holes.push(
        new THREE.Path([new THREE.Vector2(x0, y0), new THREE.Vector2(x1, y0), new THREE.Vector2(x1, y1), new THREE.Vector2(x0, y1)]),
      );
    }
    return outline;
  }, [wall, height]);

  return (
    <group position={wall.origin} rotation={[0, wall.rotationY, 0]}>
      <group ref={grow} scale-y={still ? 1 : 0.001}>
        <mesh receiveShadow>
          <shapeGeometry args={[shape]} />
          <meshStandardMaterial color={PALETTE.wall} roughness={0.95} side={THREE.FrontSide} />
        </mesh>
        <group ref={openings}>
          {wall.cuts.map((cut) =>
            cut.kind === "door" ? (
              <mesh key={`${cut.kind}-${cut.u}`} position={[cut.u, cut.height / 2, 0.02]}>
                <boxGeometry args={[cut.width - 0.04, cut.height - 0.02, 0.04]} />
                <meshStandardMaterial color={PALETTE.door} roughness={0.6} />
              </mesh>
            ) : (
              <group key={`${cut.kind}-${cut.u}`} position={[cut.u, cut.sill + cut.height / 2, 0.01]}>
                <mesh>
                  <planeGeometry args={[cut.width, cut.height]} />
                  <meshStandardMaterial color={PALETTE.glass} transparent opacity={0.45} roughness={0.1} />
                </mesh>
                {[
                  [0, cut.height / 2, cut.width + 0.06, 0.04],
                  [0, -cut.height / 2, cut.width + 0.06, 0.04],
                  [-cut.width / 2, 0, 0.04, cut.height],
                  [cut.width / 2, 0, 0.04, cut.height],
                  [0, 0, 0.025, cut.height],
                ].map(([x, y, w, h]) => (
                  <mesh key={`${x}-${y}-${w}`} position={[x, y, 0.015]}>
                    <boxGeometry args={[w, h, 0.04]} />
                    <meshStandardMaterial color={PALETTE.frame} />
                  </mesh>
                ))}
              </group>
            ),
          )}
        </group>
      </group>
    </group>
  );
}

function Floor({ geometry, grid, scan }: { geometry: RoomGeometry; grid: boolean; scan: boolean }) {
  const [cx, cy] = centerOf(geometry);
  const shape = useMemo(
    () => new THREE.Shape(geometry.room_vertices.map(([x, y]) => new THREE.Vector2(x - cx, y - cy))),
    [geometry.room_vertices, cx, cy],
  );
  // The plinth follows the page theme; the room itself stays light.
  const plinth = useThemeToken("--line");
  return (
    <group>
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.02, 0]}>
        <circleGeometry args={[Math.hypot(...geometry.room_area) * 0.62, 96]} />
        <meshBasicMaterial color={plinth} />
      </mesh>
      <mesh rotation={[-Math.PI / 2, 0, 0]} receiveShadow>
        <shapeGeometry args={[shape]} />
        <meshStandardMaterial color={PALETTE.floor} roughness={0.8} />
      </mesh>
      {grid && <ScanGrid geometry={geometry} scan={scan} />}
    </group>
  );
}

function useThemeToken(name: string): string {
  const read = () => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#e1e3ea";
  const [value, setValue] = useState(read);
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const update = () => setValue(read());
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [name]); // eslint-disable-line react-hooks/exhaustive-deps
  return value;
}

const SCAN_PERIOD_S = 2.6;

/**
 * Half-metre grid over the room. While `scan` is on, a beam sweeps back and forth across the floor
 * and the grid appears behind its first pass, like a measurement being drawn.
 */
function ScanGrid({ geometry: room, scan }: { geometry: RoomGeometry; scan: boolean }) {
  const [width, length] = room.room_area;
  // Grid lines are cut to the floor outline, so L, T, and angled rooms get no grid in open air.
  const geometry = useMemo(() => {
    const points: number[] = [];
    const [cx, cy] = [width / 2, length / 2];
    for (let x = 0.5; x < width; x += 0.5)
      for (const [y0, y1] of insideSpans(room.room_vertices, 0, x)) points.push(x - cx, 0.004, -(y0 - cy), x - cx, 0.004, -(y1 - cy));
    for (let y = 0.5; y < length; y += 0.5)
      for (const [x0, x1] of insideSpans(room.room_vertices, 1, y)) points.push(x0 - cx, 0.004, -(y - cy), x1 - cx, 0.004, -(y - cy));
    return new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(points, 3));
  }, [room.room_vertices, width, length]);
  const still = useMemo(prefersReducedMotion, []);
  const clip = useMemo(() => new THREE.Plane(new THREE.Vector3(-1, 0, 0), still ? 1e3 : -width / 2), [width, still]);
  const beam = useRef<THREE.Group>(null);
  const glow = useRef<THREE.MeshBasicMaterial>(null);
  const age = useAge();

  useFrame(({ clock }) => {
    if (still || !beam.current || !glow.current) return;
    const t = age(clock.elapsedTime) / SCAN_PERIOD_S;
    const pass = t % 2 < 1 ? t % 1 : 1 - (t % 1); // ping-pong 0..1
    const eased = pass < 0.5 ? 2 * pass * pass : 1 - Math.pow(-2 * pass + 2, 2) / 2;
    beam.current.position.x = -width / 2 + eased * width;
    clip.constant = t >= 1 ? 1e3 : beam.current.position.x; // first pass reveals the grid
    const target = scan ? 1 : 0;
    glow.current.opacity += (target * 0.22 - glow.current.opacity) * 0.08;
    beam.current.visible = glow.current.opacity > 0.01;
  });

  return (
    <group>
      <lineSegments geometry={geometry}>
        <lineBasicMaterial color="#7fa3bd" transparent opacity={0.55} clippingPlanes={[clip]} />
      </lineSegments>
      {!still && (
        <group ref={beam}>
          <mesh position={[0, 0.6, 0]} rotation={[0, Math.PI / 2, 0]}>
            <planeGeometry args={[length, 1.2]} />
            <meshBasicMaterial
              ref={glow}
              color={PALETTE.accent}
              transparent
              opacity={0}
              side={THREE.DoubleSide}
              depthWrite={false}
              blending={THREE.AdditiveBlending}
            />
          </mesh>
          <mesh position={[0, 0.006, 0]}>
            <boxGeometry args={[0.025, 0.01, length]} />
            <meshBasicMaterial color={PALETTE.accent} />
          </mesh>
        </group>
      )}
    </group>
  );
}

/** A glowing half-disc in front of each door: the clearance the room stage keeps free. */
function Clearance({ geometry, walls }: { geometry: RoomGeometry; walls: Wall[] }) {
  const center = centerOf(geometry);
  const fill = useRef<THREE.MeshBasicMaterial[]>([]);
  const still = useMemo(prefersReducedMotion, []);
  const age = useAge();
  useFrame(({ clock }) => {
    const t = age(clock.elapsedTime);
    const opacity = clamp01(t / 0.6) * (still ? 0.3 : 0.26 + 0.1 * Math.sin(t * 2.4));
    for (const material of fill.current) if (material) material.opacity = opacity;
  });
  return (
    <>
      {geometry.room_doors.map((door, index) => {
        const wall = nearestWall(walls, [door.center[0], door.center[1], 0]);
        if (!wall) return null;
        const radius = Math.max(door.width, door.depth, 0.6);
        const facing = Math.atan2(-wall.normal.x, -wall.normal.z); // turns the half-disc into the room
        return (
          <group key={door.id} position={toWorld([door.center[0], door.center[1], 0.008], center)} rotation={[0, facing, 0]}>
            <mesh rotation={[-Math.PI / 2, 0, 0]}>
              <circleGeometry args={[radius, 48, 0, Math.PI]} />
              <meshBasicMaterial
                ref={(m) => void (fill.current[index] = m!)}
                color={PALETTE.accent}
                transparent
                opacity={0}
                depthWrite={false}
              />
            </mesh>
            <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.001, 0]}>
              <ringGeometry args={[radius - 0.025, radius, 48, 1, 0, Math.PI]} />
              <meshBasicMaterial color={PALETTE.accent} transparent opacity={0.9} />
            </mesh>
          </group>
        );
      })}
    </>
  );
}

/** A dashed path that flows from the door across the room while layouts are checked for walkways. */
function Walkway({ geometry, walls }: { geometry: RoomGeometry; walls: Wall[] }) {
  const center = centerOf(geometry);
  const line = useRef<{ material: { dashOffset: number } } | null>(null);
  const still = useMemo(prefersReducedMotion, []);
  const door = geometry.room_doors[0];
  const points = useMemo(() => {
    if (!door) return null;
    const wall = nearestWall(walls, [door.center[0], door.center[1], 0]);
    if (!wall) return null;
    const start = toWorld([door.center[0], door.center[1], 0.012], center).addScaledVector(wall.normal, 0.25);
    // Through the floor's centroid, which stays on the floor in L and T rooms, and on past it.
    const middle = toWorld([...polygonCentroid(geometry.room_vertices), 0.012], center);
    const side = new THREE.Vector3(-wall.normal.z, 0, wall.normal.x);
    const onFloor = (p: THREE.Vector3) => {
      for (let i = 0; i < 10 && !pointInPolygon([p.x + center[0], center[1] - p.z], geometry.room_vertices); i++) p.lerp(middle, 0.3);
      return p;
    };
    const bend = onFloor(start.clone().lerp(middle, 0.5).addScaledVector(side, 0.5));
    const end = onFloor(middle.clone().addScaledVector(middle.clone().sub(start), 0.7).addScaledVector(side, -0.4));
    return new THREE.CatmullRomCurve3([start, bend, middle, end]).getPoints(64);
  }, [door, walls, center, geometry.room_vertices]);
  useFrame((_, delta) => {
    if (!still && line.current) line.current.material.dashOffset -= delta * 0.8;
  });
  if (!points) return null;
  return (
    <Line
      ref={line as never}
      points={points}
      color={PALETTE.accent}
      lineWidth={4}
      dashed
      dashSize={0.16}
      gapSize={0.1}
      transparent
      opacity={0.9}
    />
  );
}

/** Scale a GLB to the catalog size, centre it on X/Z, and set it on the floor (legacy `prepareAssetModel`). */
function Model({ url, asset, onSettled }: { url: string; asset: ManifestAsset; onSettled: (failed: boolean) => void }) {
  const gltf = useGLTF(url, true);
  const object = useMemo(() => {
    const inner = new THREE.Group();
    inner.add(gltf.scene.clone(true));
    const bounds = new THREE.Box3().setFromObject(inner);
    if (bounds.isEmpty()) throw new Error("The model has no geometry.");
    const size = bounds.getSize(new THREE.Vector3());
    inner.scale.set(asset.width / (size.x || 1), asset.height / (size.y || 1), asset.depth / (size.z || 1));
    inner.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(inner);
    const center = box.getCenter(new THREE.Vector3());
    inner.position.set(-center.x, -box.min.y, -center.z);
    inner.traverse((child) => {
      if (child instanceof THREE.Mesh) child.castShadow = child.receiveShadow = true;
    });
    return inner;
  }, [gltf, asset.width, asset.height, asset.depth]);
  useLayoutEffect(() => onSettled(false), [onSettled]);
  return <primitive object={object} />;
}

/** Stands in while a model streams in: an outlined volume of the catalog size that breathes. */
function Placeholder({ asset }: { asset: ManifestAsset }) {
  const fill = useRef<THREE.MeshBasicMaterial>(null);
  const still = useMemo(prefersReducedMotion, []);
  useFrame(({ clock }) => {
    if (fill.current && !still) fill.current.opacity = 0.08 + 0.06 * Math.sin(clock.elapsedTime * 3);
  });
  return (
    <mesh position={[0, asset.height / 2, 0]}>
      <boxGeometry args={[asset.width, asset.height, asset.depth]} />
      <meshBasicMaterial ref={fill} color={PALETTE.accent} transparent opacity={0.1} depthWrite={false} />
      <Edges color={PALETTE.accent} />
    </mesh>
  );
}

class ModelBoundary extends Component<{ fallback: ReactNode; children: ReactNode; onFailed: () => void }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch() {
    this.props.onFailed();
  }
  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

/**
 * One placed piece. It settles into place after `delay` seconds, so pieces arrive one by one.
 * A wall piece hides while its wall is cut away, so the camera never sees its back.
 */
const DROP_S = 0.55;
const DROP_HEIGHT = 1.3;

function Piece({ item, center, delay, wall, tracker, onProgress }: {
  item: Furnishing; center: Vec2; delay: number; wall?: Wall;
  tracker: ReturnType<typeof trackModels>; onProgress: () => void;
}) {
  const { asset, placement } = item;
  const root = useRef<THREE.Group>(null);
  const lift = useRef<THREE.Group>(null);
  const ripple = useRef<THREE.Mesh>(null);
  const age = useAge();
  const still = useMemo(prefersReducedMotion, []);
  const grounded = asset.placement_mode === "floor";
  const onSettled = useCallback((failed: boolean) => {
    tracker.settle(placement.instance_key, failed);
    onProgress();
  }, [tracker, placement.instance_key, onProgress]);
  useLayoutEffect(() => {
    if (!asset.glb_url) onSettled(true);
  }, [asset.glb_url, onSettled]);

  // Falls with gravity, squashes a little on landing, and sends a ripple across the floor.
  useFrame(({ clock, camera }) => {
    if (!lift.current || !root.current) return;
    const t = still ? 10 : age(clock.elapsedTime) - delay;
    root.current.visible = t > 0 && (!wall || wall.normal.dot(camera.position.clone().sub(wall.origin)) > 0);
    const fall = clamp01(t / DROP_S);
    lift.current.position.y = (1 - fall * fall) * DROP_HEIGHT;
    const landed = t - DROP_S;
    const squash = landed > 0 && landed < 0.3 ? Math.sin((landed / 0.3) * Math.PI) * 0.07 : 0;
    lift.current.scale.set(1 + squash * 0.5, 1 - squash, 1 + squash * 0.5);
    if (ripple.current) {
      const r = clamp01(landed / 0.7);
      ripple.current.visible = grounded && landed > 0 && r < 1;
      ripple.current.scale.setScalar(0.6 + r * 0.9);
      (ripple.current.material as THREE.MeshBasicMaterial).opacity = 0.55 * (1 - r);
    }
  });
  const footprint = Math.max(asset.width, asset.depth) / 2;

  const position = toWorld(placement.position, center);
  const rotationY = (placement.rotation[2] ?? 0) - frontViewToYaw(asset.frontView);
  return (
    <group ref={root} position={position} rotation={[0, rotationY, 0]} visible={false}>
      <mesh ref={ripple} rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.01, 0]} visible={false}>
        <ringGeometry args={[footprint, footprint + 0.05, 48]} />
        <meshBasicMaterial color={PALETTE.accent} transparent opacity={0} depthWrite={false} />
      </mesh>
      <group ref={lift}>
        <ModelBoundary onFailed={() => onSettled(true)} fallback={<Placeholder asset={asset} />}>
          <Suspense fallback={<Placeholder asset={asset} />}>
            {asset.glb_url ? <Model url={proxied(asset.glb_url)} asset={asset} onSettled={onSettled} /> : <Placeholder asset={asset} />}
          </Suspense>
        </ModelBoundary>
      </group>
    </group>
  );
}

function nearestWall(walls: Wall[], [x, y]: [number, number, number]): Wall | undefined {
  return walls.reduce<Wall | undefined>(
    (best, wall) =>
      !best || distanceToSegment([x, y], wall.start, wall.end) < distanceToSegment([x, y], best.start, best.end) ? wall : best,
    undefined,
  );
}

const LOOK_AT = new THREE.Vector3(0, 0.45, 0);
const FOV = 30;

/** Camera distance that fits a sphere of `radius` in both the canvas height and width. */
function fitDistance(radius: number, aspect: number): number {
  const vertical = (FOV * Math.PI) / 180;
  const horizontal = 2 * Math.atan(Math.tan(vertical / 2) * aspect);
  return radius / Math.sin(Math.min(vertical, horizontal) / 2);
}

const cameraPosition = ([sx, sy]: [number, number], distance: number) =>
  new THREE.Vector3(0.7 * sx, 0.66, -0.9 * sy).normalize().multiplyScalar(distance);

/** Interactive views start at a fixed corner and hand over to OrbitControls; others ease to `zoom` (> 1 pulls back). */
function CameraRig({
  corner,
  radius,
  span,
  zoom,
  interactive,
  sway,
}: {
  corner: [number, number];
  radius: number; // the plinth's: the whole room and its base stay in frame
  span: number;
  zoom: number;
  interactive: boolean;
  sway: boolean;
}) {
  const { camera, size } = useThree();
  const [sx, sy] = corner;
  const distance = fitDistance(radius, size.width / Math.max(1, size.height));
  const base = useMemo(() => cameraPosition([sx, sy], distance * zoom), [sx, sy, distance, zoom]);
  const target = useMemo(() => new THREE.Vector3(), []);
  const up = useMemo(() => new THREE.Vector3(0, 1, 0), []);
  useEffect(() => {
    if (!interactive) return;
    camera.position.copy(cameraPosition([sx, sy], distance));
    camera.lookAt(LOOK_AT);
  }, [camera, sx, sy, distance, interactive]);
  useFrame(({ clock }) => {
    if (interactive) return;
    const still = prefersReducedMotion();
    // A slow drift around the room, about 25 degrees each way over 20 seconds.
    target.copy(base).applyAxisAngle(up, sway && !still ? 0.22 * Math.sin((clock.elapsedTime / 20) * Math.PI * 2) : 0);
    camera.position.lerp(target, still ? 1 : 0.05);
    camera.lookAt(LOOK_AT);
  });
  return interactive ? (
    <OrbitControls
      target={LOOK_AT}
      enablePan={false}
      enableDamping
      minDistance={span * 1.2}
      maxDistance={span * 4.5}
      maxPolarAngle={1.38}
    />
  ) : null;
}

export function RoomScene({
  geometry,
  furnishings = [],
  callouts = [],
  grid = false,
  zoom = 1,
  interactive = false,
  effects = {},
  onDisplayed,
}: {
  geometry: RoomGeometry;
  furnishings?: Furnishing[];
  callouts?: Callout[];
  grid?: boolean;
  zoom?: number;
  interactive?: boolean;
  effects?: SceneEffects;
  onDisplayed?: (timing: SceneTiming) => void;
}) {
  const [tracker] = useState(() => trackModels(furnishings.map((item) => item.placement.instance_key)));
  const [progress, setProgress] = useState(() => tracker.snapshot());
  const onProgress = useCallback(() => setProgress(tracker.snapshot()), [tracker]);
  const report = useCallback((timing: SceneTiming) => onDisplayed?.(timing), [onDisplayed]);
  const walls = useMemo(() => buildWalls(geometry), [geometry]);
  const center = centerOf(geometry);
  const span = Math.max(...geometry.room_area);
  const corner = viewCorner(geometry.room_vertices, geometry.room_area);
  const radius = Math.hypot(...geometry.room_area) * 0.62;

  return (
    <>
      <Canvas
        shadows
        dpr={[1, 2]}
        camera={{ fov: FOV, position: cameraPosition(corner, radius * 4 * zoom) }}
        gl={{ antialias: true }}
        onCreated={({ gl }) => void (gl.localClippingEnabled = true)}
      >
        <hemisphereLight args={["#ffffff", "#d8d4cc", 1.6]} />
        <directionalLight
          position={[span, span * 1.6, span * 0.6]}
          intensity={1.4}
          castShadow
          shadow-mapSize={[2048, 2048]}
          shadow-camera-left={-span}
          shadow-camera-right={span}
          shadow-camera-top={span}
          shadow-camera-bottom={-span}
          shadow-bias={-0.0004}
        />
        <CameraRig corner={corner} radius={radius} span={span} zoom={zoom} interactive={interactive} sway={!!effects.sway} />
        <Floor geometry={geometry} grid={grid} scan={!!effects.scan} />
        {walls.map((wall, index) => (
          <WallMesh key={index} wall={wall} height={geometry.wall_height} rise={!!effects.rise} delay={index * 0.12} />
        ))}
        {effects.clearance && <Clearance geometry={geometry} walls={walls} />}
        {effects.walkway && <Walkway geometry={geometry} walls={walls} />}
        {furnishings.map((item, index) => (
          <Piece
            key={item.placement.instance_key}
            item={item}
            tracker={tracker}
            onProgress={onProgress}
            center={center}
            delay={0.2 + index * 0.22}
            wall={item.asset.placement_mode === "wall_mounted" ? nearestWall(walls, item.placement.position) : undefined}
          />
        ))}
        <FirstFrame tracker={tracker} onDisplayed={report} />
        {callouts.map((callout) => (
          <Html key={callout.id} position={toWorld(callout.at, center)} center zIndexRange={[20, 0]}>
            <span className="callout">{callout.label}</span>
          </Html>
        ))}
      </Canvas>
      {progress.settled < progress.models && (
        <p role="status" className="absolute top-4 right-4 rounded-full bg-surface/90 px-3.5 py-1.5 text-[13px] text-muted shadow-sm backdrop-blur">
          Loading 3D pieces, {progress.settled} of {progress.models}
        </p>
      )}
      {progress.settled === progress.models && progress.failed_models > 0 && (
        <p role="status" className="absolute top-4 right-4 rounded-full bg-surface/90 px-3.5 py-1.5 text-[13px] text-muted shadow-sm backdrop-blur">
          {progress.failed_models} 3D {progress.failed_models === 1 ? "piece unavailable" : "pieces unavailable"}
        </p>
      )}
    </>
  );
}

/** Observe this canvas's draw, then report after three.js renders it. */
function FirstFrame({ tracker, onDisplayed }: {
  tracker: ReturnType<typeof trackModels>; onDisplayed: (timing: SceneTiming) => void;
}) {
  const drew = useRef(false);
  const reported = useRef(false);
  const callback = useRef(onDisplayed);
  useLayoutEffect(() => { callback.current = onDisplayed; }, [onDisplayed]);
  useFrame(() => {
    const state = tracker.snapshot();
    drew.current = state.loadedAt !== null;
  });
  useLayoutEffect(() => addAfterEffect(() => {
    if (!drew.current || reported.current) return;
    const state = tracker.snapshot();
    if (state.loadedAt === null) return;
    reported.current = true;
    callback.current({ loadedAt: state.loadedAt, displayedAt: performance.now(), models: state.models, failed_models: state.failed_models });
  }), [tracker]);
  return null;
}

export function furnishingsOf(manifest: { layout: Record<string, Placement>; assets: Record<string, ManifestAsset> }): Furnishing[] {
  // Floor pieces first, so supported and wall items arrive after what they rest on or near.
  const order = (mode: string) => ["floor", "wall_mounted", "tabletop", "ceiling_mounted"].indexOf(mode);
  return Object.values(manifest.layout)
    .filter((placement) => manifest.assets[placement.instance_key])
    .map((placement) => ({ placement, asset: manifest.assets[placement.instance_key] }))
    .sort((a, b) => order(a.asset.placement_mode) - order(b.asset.placement_mode));
}
