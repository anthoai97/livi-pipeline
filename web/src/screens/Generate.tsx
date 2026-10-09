import { ArrowRight, ArrowsClockwise, PencilSimple, X } from "@phosphor-icons/react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ProductRing, type RingItem } from "../components/ProductRing";
import { furnishingsOf, RoomScene, type Callout } from "../components/RoomScene";
import { Button, Stepper, TaskRow, Wordmark, type Task } from "../components/ui";
import { clock, humanize, metres, money } from "../lib/format";
import { buildRequest, draftFromParams, roomLabel } from "../lib/room";
import { navigate, useLocation } from "../lib/router";
import { polygonCentroid } from "../lib/shapes";
import { currentStep, nodeData, STEPS, useRun } from "../lib/run";
import { catalogTask, designName, fitTask, readyTask, roomTasks, selectionTask } from "../lib/tasks";
import type { PipelineRequest } from "../lib/types";

const AUTO_OPEN_MS = 4500;

const COPY = [
  { title: "Reading your room", body: "Measuring walls, openings, and free floor." },
  { title: "Choosing pieces", body: "Three designs pick from the same shortlist, each in its own direction." },
  { title: "Checking the fit", body: "Each design places its pieces, then fixes overlaps and blocked walkways." },
  { title: "Placing furniture", body: "A design opens as soon as it passes the final check." },
];

const MIN_STEP_MS = 1800;
const ROOM_HOLD_MS = 2000;

/**
 * Steps advance one at a time and stay up at least MIN_STEP_MS, so a stage that finishes in
 * milliseconds (the room read) still gets its moment, as the legacy wizard paces its steps.
 */
function usePacedStep(step: number, holdUntil: Record<number, number> = {}) {
  const [shown, setShown] = useState(step);
  const since = useRef(Date.now());
  useEffect(() => {
    if (step === shown) return;
    if (step < shown) {
      since.current = Date.now();
      setShown(step);
      return;
    }
    const timer = setTimeout(
      () => {
        since.current = Date.now();
        setShown((current) => current + 1);
      },
      Math.max(0, since.current + MIN_STEP_MS - Date.now(), (holdUntil[shown] ?? 0) - Date.now()),
    );
    return () => clearTimeout(timer);
  }, [step, shown, holdUntil[shown]]); // eslint-disable-line react-hooks/exhaustive-deps
  return shown;
}

function useClock(running: boolean, startedAt: number) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  return (now - startedAt) / 1000;
}

function roomCallouts(request: PipelineRequest, room: { floor_area_sqm?: number } | undefined): Callout[] {
  if (!room) return [];
  const height = request.wall_height;
  const [fx, fy] = polygonCentroid(request.room_vertices);
  // The back-left corner of the outline, wherever a cut corner leaves it.
  const [kx, ky] = request.room_vertices.reduce((best, v) => (v[1] - v[0] > best[1] - best[0] ? v : best));
  return [
    { id: "floor", at: [fx, fy, 0.05], label: `Floor ${room.floor_area_sqm?.toFixed(1)} m²` },
    { id: "ceiling", at: [kx, ky, height + 0.12], label: `${metres(height)} ceiling` },
    ...request.room_doors.map((door): Callout => ({
      id: door.id,
      at: [door.center[0], door.center[1], door.height + 0.2],
      label: `Door ${metres(Math.max(door.width, door.depth))}`,
    })),
    ...request.room_windows.map((window): Callout => ({
      id: window.id,
      at: [window.center[0], window.center[1], (window.sill_height ?? 0.9) + window.height + 0.2],
      label: `Window ${metres(Math.max(window.width, window.depth))}`,
    })),
  ];
}

export function Generate() {
  const { request, shared, variants, status, error, startedAt, start, cancel, select } = useRun();
  const reduce = useReducedMotion();
  // The room read takes milliseconds; hold step 1 so its clearance zones and brief chips register.
  const step = usePacedStep(currentStep(shared, variants), { 1: (shared.extract_room?.doneAt ?? 0) + ROOM_HOLD_MS });
  // The run belongs to the brief in the URL; `step` only mirrors progress, as in the legacy wizard.
  const { search } = useLocation();
  const key = useMemo(() => {
    const params = new URLSearchParams(search);
    params.delete("step");
    return params.toString();
  }, [search]);

  useEffect(() => {
    const draft = draftFromParams(new URLSearchParams(key));
    if (!draft) navigate("/", { replace: true });
    else if (useRun.getState().key !== key) start(buildRequest(draft), key);
  }, [key, start]);

  useEffect(() => {
    if (useRun.getState().key === key) navigate(`/generate?${key}&step=${step}`, { replace: true });
  }, [key, step]);

  const showDesigns = useCallback(
    (index: number) => {
      select(index);
      navigate("/select-variant", { replace: true });
    },
    [select],
  );
  const elapsed = useClock(status === "running", startedAt);
  const firstReady = variants.findIndex((variant) => variant.ready);
  const allSettled = variants.every((variant) => variant.ready || variant.failed);

  useEffect(() => {
    if (firstReady < 0) return;
    const timer = setTimeout(() => showDesigns(firstReady), AUTO_OPEN_MS);
    return () => clearTimeout(timer);
  }, [firstReady, showDesigns]);

  const tasks: Task[] = useMemo(() => {
    const preview = (index: number) =>
      variants[index].ready ? (
        <button
          onClick={() => showDesigns(index)}
          className="inline-flex items-center gap-1 text-[13.5px] font-medium text-accent hover:underline"
        >
          Open this design <ArrowRight size={14} weight="bold" />
        </button>
      ) : undefined;
    const perDesign = (build: typeof fitTask) => variants.map((variant, index) => ({ ...build(variant, index), action: preview(index) }));
    if (step === 1) return roomTasks(shared, request?.room_vertices.length ?? 4);
    if (step === 2) return [catalogTask(shared), ...perDesign(selectionTask)];
    if (step === 3) return perDesign(fitTask);
    return perDesign(readyTask);
  }, [step, shared, variants, showDesigns, request]);

  const ring = useMemo((): { items: RingItem[]; caption: string } | null => {
    if (step < 2 || firstReady >= 0) return null;
    const leader = variants.findIndex((variant) => nodeData(variant.nodes, "select_asset_intent")?.valid);
    if (leader >= 0) {
      const picked = nodeData(variants[leader].nodes, "select_asset_intent")!;
      return {
        caption: `${designName(leader)} picks, ${money(picked.total_cost ?? 0)}`,
        items: (picked.items ?? []).map((item, i) => ({ ...item, key: `${item.asset_id}-${i}`, label: humanize(item.category) })),
      };
    }
    const found = nodeData(shared, "rag_scope_assets");
    if (!found) return { caption: "Searching the catalog", items: [] };
    return {
      caption: `Best match for each need, from ${found.candidates} products`,
      items: (found.preview ?? []).map((item) => ({ ...item, key: item.asset_id + item.slot, label: item.slot })),
    };
  }, [step, firstReady, variants, shared]);

  if (!request) return null;
  const roomRead = shared.extract_room?.status === "done";
  const brief = nodeData(shared, "interpret");
  const understood = [...(brief?.requested_categories ?? []).map(humanize), ...(brief?.style_hints ?? [])];
  const ready = firstReady >= 0 ? variants[firstReady].ready : null;
  const copy = COPY[step - 1];
  const stopped = status === "error" || status === "cancelled";

  return (
    <div className="mx-auto flex min-h-[100dvh] max-w-[1400px] flex-col px-4 pb-4 md:px-8">
      <header className="flex flex-wrap items-center gap-x-8 gap-y-3 py-4">
        <Wordmark />
        <div className="order-3 w-full md:order-none md:w-auto md:flex-1">
          <div className="mx-auto max-w-2xl">
            <Stepper step={step} />
          </div>
        </div>
        <div className="ml-auto flex items-center gap-3">
          <span
            title="A recorded dining room run, fitted to your room"
            className="rounded-full bg-surface-2 px-3 py-1 text-[12.5px] text-muted"
          >
            Mock data
          </span>
          <span className="font-mono text-[13px] text-muted tabular-nums" aria-label="Elapsed time">
            {clock(elapsed)}
          </span>
          {status === "running" && (
            <Button variant="outline" className="px-4 py-2 text-[14px]" onClick={cancel}>
              <X size={15} weight="bold" /> Cancel
            </Button>
          )}
        </div>
      </header>

      <main className="grid flex-1 gap-4 lg:grid-cols-[minmax(0,1fr)_400px]">
        <section aria-label="Room" className="relative min-h-[420px] overflow-hidden rounded-[20px] bg-canvas md:min-h-[560px]">
          <RoomScene
            geometry={ready?.render_manifest ?? request}
            furnishings={ready ? furnishingsOf(ready.render_manifest) : []}
            callouts={step === 1 ? roomCallouts(request, nodeData(shared, "extract_room")) : []}
            grid={step === 1}
            zoom={ring ? 1.8 : 1}
            effects={{
              rise: true,
              scan: step === 1 && !roomRead,
              clearance: roomRead && !ready,
              walkway: step === 3 && !ready,
              sway: true,
            }}
          />
          {ring && <ProductRing items={ring.items} caption={ring.caption} />}
          {ready && (
            <p className="absolute top-4 left-1/2 -translate-x-1/2 rounded-full bg-surface/90 px-3.5 py-1.5 text-[13px] font-medium text-text shadow-sm backdrop-blur">
              {designName(firstReady)} is ready
            </p>
          )}
          <div
            className={`absolute bottom-4 left-4 hidden max-w-sm rounded-xl bg-surface/92 px-4 py-3 shadow-sm backdrop-blur ${ring ? "" : "md:block"}`}
          >
            <p
              className={`line-clamp-2 text-[13.5px] leading-snug text-text ${shared.interpret?.status === "working" ? "shimmer-text" : ""}`}
            >
              {request.user_intent}
            </p>
            {understood.length > 0 ? (
              <ul className="mt-2 flex flex-wrap gap-1.5" aria-label="What we understood">
                {understood.map((word, index) => (
                  <motion.li
                    key={word}
                    initial={reduce ? false : { opacity: 0, y: 6, scale: 0.9 }}
                    animate={{ opacity: 1, y: 0, scale: 1 }}
                    transition={{ type: "spring", stiffness: 160, damping: 16, delay: reduce ? 0 : index * 0.07 }}
                    className="rounded-full bg-accent-soft px-2.5 py-0.5 text-[12.5px] font-medium text-accent first-letter:uppercase"
                  >
                    {word}
                  </motion.li>
                ))}
              </ul>
            ) : (
              <p className="mt-1 text-[12.5px] text-muted">
                {roomLabel(request.room_type)}, budget {money(request.budget)}
              </p>
            )}
          </div>
        </section>

        <aside className="flex flex-col rounded-[20px] border border-line bg-surface p-5 md:p-6">
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={stopped ? status : step}
              initial={reduce ? false : { opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reduce ? { opacity: 0 } : { opacity: 0, y: -8 }}
              transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
            >
              {stopped ? (
                <Stopped
                  title={status === "cancelled" ? "Generation stopped" : "Something went wrong"}
                  body={status === "cancelled" ? "Nothing was saved. Start again or change the brief." : (error ?? "The pipeline stopped.")}
                  onRetry={() => start(request, key)}
                  onEdit={() => navigate("/", { replace: true })}
                />
              ) : (
                <>
                  <h2 className="text-[26px] leading-tight font-semibold tracking-tight text-text">{copy.title}</h2>
                  <p className="mt-1.5 text-[15px] leading-relaxed text-muted">{copy.body}</p>
                  <ul className="mt-5 grid gap-2">
                    {tasks.map((task) => (
                      <TaskRow key={task.id} task={task} />
                    ))}
                  </ul>
                </>
              )}
            </motion.div>
          </AnimatePresence>

          <div className="mt-auto pt-6">
            {firstReady >= 0 ? (
              <Button className="w-full" onClick={() => showDesigns(firstReady)}>
                {allSettled ? "Compare designs" : "Open the first design"} <ArrowRight size={18} weight="bold" />
              </Button>
            ) : (
              !stopped &&
              step < 4 && (
                <div className="border-t border-line pt-4">
                  <p className="text-[13px] font-medium text-muted">Up next</p>
                  <ul className="mt-2 grid gap-1.5">
                    {STEPS.slice(step).map((label) => (
                      <li key={label} className="text-[14.5px] text-faint">
                        {label}
                      </li>
                    ))}
                  </ul>
                </div>
              )
            )}
          </div>
        </aside>
      </main>
    </div>
  );
}

function Stopped({ title, body, onRetry, onEdit }: { title: string; body: string; onRetry: () => void; onEdit: () => void }) {
  return (
    <div role="alert">
      <h2 className="text-[26px] leading-tight font-semibold tracking-tight text-text">{title}</h2>
      <p className="mt-1.5 text-[15px] leading-relaxed text-muted">{body}</p>
      <div className="mt-5 flex flex-wrap gap-2">
        <Button onClick={onRetry}>
          <ArrowsClockwise size={17} weight="bold" /> Try again
        </Button>
        <Button variant="outline" onClick={onEdit}>
          <PencilSimple size={17} /> Edit brief
        </Button>
      </div>
    </div>
  );
}
