import { ArrowLeft, ArrowRight, Check, CircleNotch } from "@phosphor-icons/react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useEffect } from "react";
import { furnishingsOf, RoomScene } from "../components/RoomScene";
import { Button, Wordmark } from "../components/ui";
import { grouped, humanize, money } from "../lib/format";
import { createDesign, studioHref } from "../lib/designs";
import { roomLabel } from "../lib/room";
import { navigate } from "../lib/router";
import { useRun, type VariantProgress } from "../lib/run";
import { designName, failureText, variantStage } from "../lib/tasks";

export function Choose() {
  const { variants, selected, select, reset, cancel, request, status } = useRun();
  const reduce = useReducedMotion();
  const hasOptions = variants.some((v) => v.ready);
  const variant = variants[selected];
  const ready = variant.ready;
  const pending = variants.filter((v) => !v.ready && !v.failed).length;
  const step = (delta: number) => select((selected + delta + variants.length) % variants.length);

  // Options live only in memory, so a reload or a direct visit goes back to the brief.
  useEffect(() => {
    if (!hasOptions) navigate("/", { replace: true });
  }, [hasOptions]);

  // Legacy guard: leaving the page discards the generated options.
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, []);

  function startOver() {
    reset();
    navigate("/", { replace: true });
  }

  /** Legacy finalize: create the design, stop any designs still running, open Studio. */
  function continueWith() {
    if (!ready || !request) return;
    const identity = createDesign(ready, request, `${roomLabel(request.room_type)}, ${designName(selected)}`);
    if (status === "running") cancel();
    navigate(studioHref(identity), { replace: true });
  }

  if (!hasOptions) return null;

  return (
    <div className="mx-auto flex min-h-[100dvh] max-w-[1400px] flex-col px-4 pb-4 md:px-8">
      <header className="flex h-16 items-center justify-between">
        <Wordmark />
        <Button variant="quiet" onClick={startOver}>
          Start over
        </Button>
      </header>

      <main className="grid flex-1 gap-4 lg:grid-cols-[minmax(0,1fr)_420px]">
        <section className="flex min-w-0 flex-col gap-3">
          <div className="relative min-h-[420px] flex-1 overflow-hidden rounded-[20px] bg-canvas md:min-h-[560px]">
            {ready ? (
              <RoomScene
                key={ready.variant_id}
                geometry={ready.render_manifest}
                furnishings={furnishingsOf(ready.render_manifest)}
                interactive
              />
            ) : (
              <Placeholder variant={variant} index={selected} />
            )}
            <div className="absolute inset-x-4 bottom-4 flex items-center justify-between">
              <button
                aria-label="Previous design"
                onClick={() => step(-1)}
                className="grid size-11 place-items-center rounded-full bg-surface/92 text-text shadow-sm backdrop-blur transition hover:bg-surface active:scale-[0.96]"
              >
                <ArrowLeft size={18} weight="bold" />
              </button>
              {ready && (
                <p className="rounded-full bg-surface/92 px-3.5 py-1.5 text-[13px] text-muted shadow-sm backdrop-blur">
                  Drag to look around
                </p>
              )}
              <button
                aria-label="Next design"
                onClick={() => step(1)}
                className="grid size-11 place-items-center rounded-full bg-surface/92 text-text shadow-sm backdrop-blur transition hover:bg-surface active:scale-[0.96]"
              >
                <ArrowRight size={18} weight="bold" />
              </button>
            </div>
          </div>

          <div role="tablist" aria-label="Designs" className="grid grid-cols-3 gap-2">
            {variants.map((option, index) => (
              <button
                key={index}
                role="tab"
                aria-selected={index === selected}
                onClick={() => select(index)}
                className={`rounded-xl border px-3.5 py-3 text-left transition active:scale-[0.99] ${
                  index === selected ? "border-accent bg-accent-soft/50 ring-1 ring-accent/30" : "border-line bg-surface hover:bg-surface-2"
                }`}
              >
                <p className="text-[14.5px] font-medium text-text">{designName(index)}</p>
                <p className={`mt-0.5 flex items-center gap-1.5 text-[13px] ${option.failed ? "text-bad" : "text-muted"}`}>
                  {!option.ready && !option.failed && <CircleNotch size={13} className={reduce ? "" : "animate-spin"} />}
                  {variantStage(option)}
                </p>
              </button>
            ))}
          </div>
        </section>

        <aside className="flex flex-col rounded-[20px] border border-line bg-surface p-5 md:p-6">
          <h1 className="text-[30px] leading-[1.1] font-semibold tracking-tight text-text">Choose your design.</h1>
          <p className="mt-2 text-[15px] leading-relaxed text-muted">Your pick opens in Studio, where you can keep editing.</p>
          {pending > 0 && (
            <p className="mt-4 flex items-center gap-2 rounded-xl bg-accent-soft/60 px-3.5 py-2.5 text-[14px] text-accent">
              <CircleNotch size={16} weight="bold" className={reduce ? "" : "animate-spin"} />
              {pending === 1 ? "One more design is on its way." : `${pending} more designs are on their way.`}
            </p>
          )}

          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={selected}
              initial={reduce ? false : { opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reduce ? { opacity: 0 } : { opacity: 0, y: -8 }}
              transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
              className="mt-6 flex min-h-0 flex-1 flex-col"
            >
              {ready ? (
                <>
                  <dl className="grid grid-cols-2 gap-4 border-b border-line pb-5">
                    <div>
                      <dt className="text-[13px] text-muted">Pieces</dt>
                      <dd className="mt-1 text-[28px] font-semibold tracking-tight text-text tabular-nums">
                        {ready.selected_assets.length}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-[13px] text-muted">Shoppable total</dt>
                      <dd className="mt-1 text-[28px] font-semibold tracking-tight text-text tabular-nums">{money(ready.total_cost)}</dd>
                    </div>
                    {request && (
                      <p className="col-span-2 text-[13.5px] text-muted">
                        {ready.total_cost <= request.budget
                          ? `${money(request.budget - ready.total_cost)} under your ${money(request.budget)} budget.`
                          : `${money(ready.total_cost - request.budget)} over your ${money(request.budget)} budget.`}{" "}
                        Decor without a price is not counted.
                      </p>
                    )}
                  </dl>
                  <ul className="mt-4 grid max-h-[340px] grid-cols-2 gap-2 overflow-y-auto pr-1">
                    {grouped(ready.selected_assets).map((asset) => (
                      <li key={asset.instance_key} className="flex items-center gap-2.5 rounded-xl bg-surface-2/70 p-2">
                        <span className="grid size-12 shrink-0 place-items-center overflow-hidden rounded-lg bg-white">
                          {asset.image_url && <img src={asset.image_url} alt="" loading="lazy" className="size-full object-contain p-1" />}
                        </span>
                        <span className="min-w-0">
                          <span className="block truncate text-[13px] font-medium text-text">{asset.name}</span>
                          <span className="block truncate text-[12.5px] text-muted">
                            {asset.price != null && !asset.is_decor_item ? money(asset.price) : "Decor"}
                            {asset.count > 1 ? ` x${asset.count}` : ""}, {humanize(asset.category)}
                          </span>
                        </span>
                      </li>
                    ))}
                  </ul>
                </>
              ) : (
                <p className={`text-[15px] ${variant.failed ? "text-bad" : "text-muted"}`}>
                  {variant.failed
                    ? failureText(variant.failed.reason)
                    : "This design is still being built. You can look at the others meanwhile."}
                </p>
              )}
            </motion.div>
          </AnimatePresence>

          <div className="mt-6 grid gap-2">
            <Button disabled={!ready} onClick={continueWith}>
              <Check size={18} weight="bold" /> Continue with this design
            </Button>
          </div>
        </aside>
      </main>
    </div>
  );
}

function Placeholder({ variant, index }: { variant: VariantProgress; index: number }) {
  return (
    <div className="grid size-full min-h-[420px] place-items-center p-8 text-center">
      <div>
        <p className="text-[18px] font-medium text-text">{designName(index)}</p>
        <p className={`mt-1 text-[14.5px] ${variant.failed ? "text-bad" : "text-muted"}`}>
          {variant.failed ? failureText(variant.failed.reason) : `${variantStage(variant)}. It shows here when it is ready.`}
        </p>
      </div>
    </div>
  );
}
