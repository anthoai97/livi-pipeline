import { ArrowRight, Check, CircleNotch } from "@phosphor-icons/react";
import { useReducedMotion } from "motion/react";
import { useEffect } from "react";
import { furnishingsOf, RoomScene } from "../components/RoomScene";
import { Button, Wordmark } from "../components/ui";
import { grouped, humanize, money } from "../lib/format";
import { createDesign, studioHref } from "../lib/designs";
import { roomLabel } from "../lib/room";
import { navigate } from "../lib/router";
import { useRun, type VariantProgress } from "../lib/run";
import { designName, variantStage } from "../lib/tasks";

export function Choose() {
  const { variants, selected, select, reset, cancel, request, status, error, runId, recordTiming } = useRun();
  const reduce = useReducedMotion();
  const hasOptions = variants.some((v) => v.ready);
  const variant = variants[selected];
  const ready = variant.ready;
  const pending = status === "running" ? variants.filter((v) => !v.ready && !v.failed).length : 0;

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
    <div className="flex min-h-[100dvh] flex-col px-4 pb-4 md:px-6">
      <header className="flex h-14 shrink-0 items-center justify-between">
        <Wordmark />
        <Button variant="quiet" onClick={startOver}>
          Start over
        </Button>
      </header>

      <main aria-label="Choose your design">
        <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_280px]">
          <section aria-label="Design preview" className="min-w-0">
            <div className="relative h-[max(320px,60svh)] overflow-hidden rounded-[20px] bg-canvas lg:h-[max(400px,calc(100dvh-160px))]">
              {ready ? (
                <RoomScene
                  key={`${runId}:${ready.variant_id}`}
                  onDisplayed={(timing) => recordTiming(runId, ready.variant_id, "chooser", timing)}
                  geometry={ready.render_manifest}
                  furnishings={furnishingsOf(ready.render_manifest)}
                  zoom={0.85}
                  interactive
                />
              ) : (
                <Placeholder variant={variant} index={selected} />
              )}
            </div>

            <div role="group" aria-label="Designs" className="mt-3 grid grid-cols-3 gap-2">
              {variants.map((option, index) => (
                <button
                  key={index}
                  aria-pressed={index === selected}
                  onClick={() => select(index)}
                  className={`min-w-0 rounded-xl border px-3 py-3 text-left transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent sm:px-4 ${
                    index === selected ? "border-accent bg-accent-soft/50" : "border-line hover:bg-surface-2"
                  }`}
                >
                  <span className="flex items-center justify-between gap-1 text-[13px] font-medium text-text sm:text-[14px]">
                    {designName(index)}
                    {index === selected && <Check size={14} weight="bold" className="shrink-0 text-accent" />}
                  </span>
                  <span className={`mt-1 flex items-center gap-1.5 text-[12px] tabular-nums sm:text-[13px] ${option.failed ? "text-bad" : "text-muted"}`}>
                    {!option.ready && !option.failed && <CircleNotch size={13} className={`shrink-0 ${reduce ? "" : "animate-spin"}`} />}
                    {variantStage(option)}
                  </span>
                </button>
              ))}
            </div>
          </section>

          <aside className="min-w-0 border-t border-line pt-5 lg:border-t-0 lg:pt-2">
            <h2 className="text-[22px] font-medium tracking-tight text-text">{designName(selected)}</h2>
            {error && <p role="alert" className="mt-3 text-[14px] text-bad">{error}</p>}
            {ready ? (
              <>
                <p className="mt-1 text-[14px] text-muted">{ready.selected_assets.length} items{request ? ` for your ${roomLabel(request.room_type).toLowerCase()}` : ""}</p>
                <dl className="mt-6">
                  <dt className="text-[13px] text-muted">Product total</dt>
                  <dd className="mt-1 text-[36px] font-medium tracking-tight text-text tabular-nums">{money(ready.total_cost)}</dd>
                </dl>
                {request && (
                  <p className={`mt-1 text-[13px] ${ready.total_cost > request.budget ? "text-warn" : "text-muted"}`}>
                    {ready.total_cost <= request.budget
                      ? `${money(request.budget - ready.total_cost)} under budget`
                      : `${money(ready.total_cost - request.budget)} over budget`}
                  </p>
                )}
                <p className="mt-3 text-[12px] leading-relaxed text-muted">Excludes decor with no listed price.</p>
              </>
            ) : (
              <p role="status" className={`mt-3 text-[14px] leading-relaxed ${variant.failed ? "text-bad" : "text-muted"}`}>
                {variant.failed ? variant.failed.message : "Still generating. You can view another design while you wait."}
              </p>
            )}

            <Button className="mt-6 w-full" disabled={!ready} onClick={continueWith}>
              Continue with this design <ArrowRight size={17} />
            </Button>
            {pending > 0 && (
              <p role="status" className="mt-4 flex items-center gap-2 text-[13px] text-muted">
                <CircleNotch size={14} className={`shrink-0 ${reduce ? "" : "animate-spin"}`} />
                {pending === 1 ? "1 design still generating" : `${pending} designs still generating`}
              </p>
            )}

            {ready && (
              <section aria-label="Products" className="mt-6 border-t border-line pt-4">
                <h3 className="text-[14px] font-medium text-text">
                  Products ({grouped(ready.selected_assets).length})
                </h3>
                <ul className="mt-4 grid max-h-[360px] gap-3 overflow-y-auto pr-1">
                  {grouped(ready.selected_assets).map((asset) => (
                    <li key={asset.instance_key} className="flex items-center gap-3">
                      <span className="grid size-12 shrink-0 place-items-center overflow-hidden rounded-lg bg-white">
                        {asset.image_url && <img src={asset.image_url} alt="" loading="lazy" className="size-full object-contain p-1" />}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[13px] font-medium text-text" title={asset.name}>{asset.name}</span>
                        <span className="block truncate text-[12px] text-muted first-letter:uppercase">{humanize(asset.category)}{asset.count > 1 ? ` × ${asset.count}` : ""}</span>
                        <span className="mt-1 block text-[13px] font-medium text-text tabular-nums">{asset.price != null && !asset.is_decor_item ? money(asset.price) : "Decor"}</span>
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </aside>
        </div>
      </main>
    </div>
  );
}

function Placeholder({ variant, index }: { variant: VariantProgress; index: number }) {
  return (
    <div className="grid size-full place-items-center p-8 text-center">
      <div className="max-w-sm">
        <p className="text-[18px] font-medium text-text">{designName(index)}</p>
        <p className={`mt-2 text-[14px] leading-relaxed ${variant.failed ? "text-bad" : "text-muted"}`}>
          {variant.failed ? variant.failed.message : `${variantStage(variant)}. The room will appear here when ready.`}
        </p>
      </div>
    </div>
  );
}
