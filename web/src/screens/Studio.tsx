import { Plus } from "@phosphor-icons/react";
import { useMemo, useRef } from "react";
import { furnishingsOf, RoomScene } from "../components/RoomScene";
import { Button, Wordmark } from "../components/ui";
import { loadDesign } from "../lib/designs";
import { grouped, humanize, money } from "../lib/format";
import { navigate, useLocation } from "../lib/router";
import { useRun } from "../lib/run";

/** /studio?v=2&workspace&room&design: opens the design named by the URL, as the legacy Studio does. */
export function Studio() {
  const { search } = useLocation();
  const saved = useMemo(() => loadDesign(new URLSearchParams(search)), [search]);
  const confirm = useRef<HTMLDialogElement>(null);
  const { reset, runId, recordTiming } = useRun();

  function startNew() {
    confirm.current?.close();
    reset();
    navigate("/");
  }

  const pieces = saved ? grouped(saved.variant.selected_assets) : [];

  return (
    <div className="mx-auto flex min-h-[100dvh] max-w-[1400px] flex-col px-4 pb-4 md:px-8">
      <header className="flex h-16 items-center gap-4">
        <button aria-label="Livinit home" onClick={() => (saved ? confirm.current?.showModal() : navigate("/"))}>
          <Wordmark />
        </button>
        {saved && <p className="min-w-0 truncate text-[15px] font-medium text-text">{saved.name}</p>}
        {saved && (
          <Button variant="outline" className="ml-auto px-4 py-2 text-[14px]" onClick={() => confirm.current?.showModal()}>
            <Plus size={15} weight="bold" /> New design
          </Button>
        )}
      </header>

      {saved ? (
        <main className="grid flex-1 gap-4 lg:grid-cols-[minmax(0,1fr)_380px]">
          <section aria-label="3D view" className="relative min-h-[420px] overflow-hidden rounded-[20px] bg-canvas md:min-h-[600px]">
            <RoomScene
              key={saved.variant.variant_id}
              geometry={saved.variant.render_manifest}
              furnishings={furnishingsOf(saved.variant.render_manifest)}
              onDisplayed={(timing) => recordTiming(runId, saved.variant.variant_id, "studio", timing)}
              interactive
            />
          </section>

          <aside className="flex min-h-0 flex-col rounded-[20px] border border-line bg-surface p-5 md:p-6">
            <h1 className="text-[22px] font-semibold tracking-tight text-text">Products in this room</h1>
            <p className="mt-1 text-[14px] text-muted">
              {pieces.length} unique products, {saved.variant.selected_assets.length} items
            </p>
            <ul className="mt-4 grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)] content-start gap-2 overflow-y-auto pr-1">
              {pieces.map((asset) => (
                <li key={asset.instance_key} className="flex items-center gap-3 rounded-xl bg-surface-2/70 p-2">
                  <span className="grid size-14 shrink-0 place-items-center overflow-hidden rounded-lg bg-white">
                    {asset.image_url && <img src={asset.image_url} alt="" loading="lazy" className="size-full object-contain p-1" />}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[14px] font-medium text-text">{asset.name}</span>
                    <span className="block truncate text-[13px] text-muted first-letter:uppercase">{humanize(asset.category)}</span>
                  </span>
                  <span className="shrink-0 text-right text-[13.5px] text-text tabular-nums">
                    {asset.price && !asset.is_decor_item ? money(asset.price) : "Decor"}
                    {asset.count > 1 && <span className="block text-[12.5px] text-muted">x{asset.count}</span>}
                  </span>
                </li>
              ))}
            </ul>
            <dl className="mt-4 flex items-baseline justify-between border-t border-line pt-4">
              <dt className="text-[14px] text-muted">Product total</dt>
              <dd className="text-[22px] font-semibold tracking-tight text-text tabular-nums">{money(saved.variant.total_cost)}</dd>
            </dl>
          </aside>
        </main>
      ) : (
        <main className="grid flex-1 place-items-center rounded-[20px] bg-canvas p-8 text-center">
          <div>
            <h1 className="text-[22px] font-semibold tracking-tight text-text">No saved design found</h1>
            <p className="mt-2 text-[15px] text-muted">Enter your room details to generate a design.</p>
            <Button className="mt-5" onClick={() => navigate("/")}>
              Start a design
            </Button>
          </div>
        </main>
      )}

      <dialog
        ref={confirm}
        aria-labelledby="new-design-title"
        className="m-auto w-[min(420px,calc(100vw-32px))] rounded-[20px] border border-line bg-surface p-6 text-text shadow-2xl backdrop:bg-black/40"
      >
        <h2 id="new-design-title" className="text-[20px] font-semibold tracking-tight">
          Start a new design?
        </h2>
        <p className="mt-2 text-[15px] text-muted">This design stays saved in this browser. Use its link to reopen it.</p>
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="outline" onClick={() => confirm.current?.close()}>
            Stay here
          </Button>
          <Button onClick={startNew}>Start new</Button>
        </div>
      </dialog>
    </div>
  );
}
