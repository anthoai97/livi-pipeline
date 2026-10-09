import { ArrowRight } from "@phosphor-icons/react";
import { motion, useReducedMotion } from "motion/react";
import { useId, useMemo, useState, type ChangeEvent, type FormEvent, type ReactNode } from "react";
import { RoomScene } from "../components/RoomScene";
import { Button, Wordmark } from "../components/ui";
import { metres } from "../lib/format";
import {
  buildRequest,
  DEFAULT_DRAFT,
  DOOR_WIDTH,
  draftEdges,
  draftFromParams,
  draftToParams,
  draftVertices,
  openingsProblem,
  ROOM_TYPES,
  WINDOW_WIDTH,
  type RoomDraft,
} from "../lib/room";
import { navigate } from "../lib/router";
import { useRun } from "../lib/run";
import {
  CORNERS,
  defaultWall,
  polygonArea,
  polygonEdges,
  SHAPES,
  shapeVertices,
  viewCorner,
  type CutCorner,
  type ShapeId,
} from "../lib/shapes";

type Errors = Partial<Record<keyof RoomDraft, string>>;

function check(draft: RoomDraft): Errors {
  const errors: Errors = {};
  if (draft.prompt.trim().length < 8) errors.prompt = "Describe the room in a sentence or two.";
  if (!(draft.budget >= 500)) errors.budget = "Use a budget of at least $500.";
  if (!(draft.width >= 2 && draft.width <= 12)) errors.width = "2 to 12 m";
  if (!(draft.length >= 2 && draft.length <= 12)) errors.length = "2 to 12 m";
  if (!(draft.height >= 2.2 && draft.height <= 4.5)) errors.height = "2.2 to 4.5 m";
  const openings = openingsProblem(draft);
  if (openings) errors.doorWall = openings;
  return errors;
}

/** The shape's real floor outline, drawn from the same vertices the room is built from. */
function ShapeOutline({ shape, corner }: { shape: ShapeId; corner: CutCorner }) {
  const points = shapeVertices(shape, corner, 1, 1)
    .map(([x, y]) => `${3 + x * 26},${29 - y * 26}`)
    .join(" ");
  return (
    <svg viewBox="0 0 32 32" className="size-8" aria-hidden>
      <polygon points={points} fill="currentColor" fillOpacity={0.14} stroke="currentColor" strokeWidth={1.6} strokeLinejoin="round" />
    </svg>
  );
}

function Field({ label, error, children, hint }: { label: string; error?: string; hint?: string; children: (id: string) => ReactNode }) {
  const id = useId();
  return (
    <div className="grid gap-2">
      <label htmlFor={id} className="text-[14px] font-medium text-text">
        {label}
      </label>
      {children(id)}
      {error ? <p className="text-[13px] text-bad">{error}</p> : hint ? <p className="text-[13px] text-faint">{hint}</p> : null}
    </div>
  );
}

const input =
  "w-full rounded-xl border border-line bg-surface px-3.5 py-2.5 text-[15px] text-text placeholder:text-faint outline-none transition focus:border-accent focus:ring-3 focus:ring-accent/20";

export function Brief() {
  // Coming back from a run reopens its brief.
  const [draft, setDraft] = useState<RoomDraft>(() => draftFromParams(new URLSearchParams(useRun.getState().key ?? "")) ?? DEFAULT_DRAFT);
  const [submitted, setSubmitted] = useState(false);
  const reduce = useReducedMotion();
  const errors = submitted ? check(draft) : {};
  // The preview keeps the last sensible size while a field is mid-edit or out of range.
  const preview = useMemo(() => {
    const sizes = check(draft);
    return buildRequest({
      ...draft,
      width: sizes.width ? DEFAULT_DRAFT.width : draft.width,
      length: sizes.length ? DEFAULT_DRAFT.length : draft.length,
      height: sizes.height ? DEFAULT_DRAFT.height : draft.height,
    });
  }, [draft]);

  const set = <K extends keyof RoomDraft>(key: K, value: RoomDraft[K]) => setDraft((current) => ({ ...current, [key]: value }));
  // A new outline has new walls, so the door and window move to the longest wall the view shows.
  const reshape = (shape: ShapeId, corner: CutCorner) =>
    setDraft((current) => {
      const vertices = shapeVertices(shape, corner, current.width, current.length);
      const wall = defaultWall(polygonEdges(vertices), viewCorner(vertices, [current.width, current.length]));
      return { ...current, shape, corner, doorWall: wall, windowWall: wall };
    });
  const edges = draftEdges(draft);
  const cuts = SHAPES.find((s) => s.value === draft.shape)?.cuts;
  const number = (key: "budget" | "width" | "length" | "height") => (event: ChangeEvent<HTMLInputElement>) =>
    set(key, event.target.valueAsNumber);

  function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitted(true);
    if (Object.keys(check(draft)).length === 0) navigate(`/generate?${draftToParams(draft)}`);
  }

  return (
    <div className="mx-auto flex min-h-[100dvh] max-w-[1400px] flex-col px-4 pb-6 md:px-8">
      <header className="flex h-16 items-center">
        <Wordmark />
      </header>

      <main className="grid flex-1 gap-6 lg:grid-cols-[minmax(0,460px)_minmax(0,1fr)] lg:gap-10">
        <motion.form
          onSubmit={submit}
          noValidate
          initial={reduce ? false : { opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }}
          className="flex flex-col gap-6 pt-4 lg:pt-10"
        >
          <div>
            <h1 className="text-4xl leading-[1.05] font-semibold tracking-tighter text-text md:text-[44px]">
              Describe a room. Get three layouts.
            </h1>
            <p className="mt-3 max-w-[46ch] text-[16px] leading-relaxed text-muted">
              Set the size and budget. We pick real products and place them so the room still works.
            </p>
          </div>

          <Field label="What should the room feel like?" error={errors.prompt}>
            {(id) => (
              <textarea
                id={id}
                rows={4}
                value={draft.prompt}
                onChange={(event) => set("prompt", event.target.value)}
                className={`${input} resize-none leading-relaxed`}
              />
            )}
          </Field>

          <fieldset className="grid gap-2">
            <legend className="mb-2 text-[14px] font-medium text-text">Room type</legend>
            <div className="flex flex-wrap gap-2">
              {ROOM_TYPES.map((room) => (
                <button
                  key={room.value}
                  type="button"
                  aria-pressed={draft.roomType === room.value}
                  onClick={() => set("roomType", room.value)}
                  className={`rounded-full border px-4 py-2 text-[14px] transition active:scale-[0.98] ${
                    draft.roomType === room.value
                      ? "border-accent bg-accent-soft font-medium text-accent"
                      : "border-line bg-surface text-muted hover:text-text"
                  }`}
                >
                  {room.label}
                </button>
              ))}
            </div>
          </fieldset>

          <fieldset className="grid gap-2">
            <legend className="mb-2 text-[14px] font-medium text-text">Room shape</legend>
            <div className="grid grid-cols-4 gap-2">
              {SHAPES.map((shape) => (
                <button
                  key={shape.value}
                  type="button"
                  aria-pressed={draft.shape === shape.value}
                  onClick={() => reshape(shape.value, draft.corner)}
                  className={`flex flex-col items-center gap-1 rounded-xl border px-2 py-2.5 text-[13px] transition active:scale-[0.98] ${
                    draft.shape === shape.value
                      ? "border-accent bg-accent-soft font-medium text-accent"
                      : "border-line bg-surface text-muted hover:text-text"
                  }`}
                >
                  <ShapeOutline shape={shape.value} corner={draft.corner} />
                  {shape.label}
                </button>
              ))}
            </div>
            {cuts && (
              <div className="mt-1 flex flex-wrap items-center gap-2">
                <span className="text-[13px] text-muted">Cut corner</span>
                {CORNERS.map((corner) => (
                  <button
                    key={corner.value}
                    type="button"
                    aria-pressed={draft.corner === corner.value}
                    onClick={() => reshape(draft.shape, corner.value)}
                    className={`rounded-full border px-3 py-1 text-[13px] transition active:scale-[0.98] ${
                      draft.corner === corner.value
                        ? "border-accent bg-accent-soft font-medium text-accent"
                        : "border-line bg-surface text-muted hover:text-text"
                    }`}
                  >
                    {corner.label}
                  </button>
                ))}
              </div>
            )}
          </fieldset>

          <div className="grid grid-cols-3 gap-3">
            <Field label="Width" error={errors.width}>
              {(id) => <input id={id} type="number" step={0.1} value={draft.width} onChange={number("width")} className={input} />}
            </Field>
            <Field label="Length" error={errors.length}>
              {(id) => <input id={id} type="number" step={0.1} value={draft.length} onChange={number("length")} className={input} />}
            </Field>
            <Field label="Ceiling" error={errors.height}>
              {(id) => <input id={id} type="number" step={0.1} value={draft.height} onChange={number("height")} className={input} />}
            </Field>
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {(["doorWall", "windowWall"] as const).map((key) => (
              <Field key={key} label={key === "doorWall" ? "Door" : "Window"}>
                {(id) => (
                  <select id={id} value={draft[key]} onChange={(event) => set(key, Number(event.target.value))} className={input}>
                    {edges.map((edge) => (
                      <option
                        key={edge.index}
                        value={edge.index}
                        disabled={edge.length < (key === "doorWall" ? DOOR_WIDTH : WINDOW_WIDTH) + 0.2}
                      >
                        {edge.label}, {metres(edge.length)}
                      </option>
                    ))}
                  </select>
                )}
              </Field>
            ))}
          </div>
          {openingsProblem(draft) && <p className="-mt-3 text-[13px] text-bad">{openingsProblem(draft)}</p>}

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label="Budget (USD)" error={errors.budget}>
              {(id) => <input id={id} type="number" step={500} value={draft.budget} onChange={number("budget")} className={input} />}
            </Field>
          </div>

          <div className="flex flex-wrap items-center gap-2 pt-1">
            <Button type="submit">
              Design my room <ArrowRight size={18} weight="bold" />
            </Button>
            <p className="text-[13px] text-faint">Mock mode: plays a recorded dining room run, fitted to your room.</p>
          </div>
        </motion.form>

        <section aria-label="Room preview" className="relative min-h-[360px] overflow-hidden rounded-[20px] bg-canvas lg:sticky lg:top-6 lg:my-4 lg:h-[calc(100dvh-7rem)] lg:self-start">
          <RoomScene geometry={preview} interactive />
          <p className="pointer-events-none absolute bottom-4 left-4 rounded-full bg-surface/90 px-3.5 py-1.5 text-[13px] text-muted shadow-sm backdrop-blur">
            {SHAPES.find((s) => s.value === draft.shape)?.label}, {polygonArea(draftVertices(draft)).toFixed(1)} m² floor,{" "}
            {metres(draft.height || 0)} ceiling. Drag to turn.
          </p>
        </section>
      </main>
    </div>
  );
}
